#!/usr/bin/env python3
"""
payer_reputation.py -- reputation for the PAYERS, propagated from trusted anchors.

`payer_graph.py` scores payees and flags `captive_sybil` (a payee whose payers each
pay only it). But a Sybil *ring* defeats that: N sockpuppet payers that pay N
sockpuppet payees each get breadth >= 2, so none look "captive" -- yet the whole
cluster is fake. Counting breadth isn't enough; you have to ask whether a payer's
breadth touches anything REAL.

This layer answers that by propagating trust from an ANCHOR set:

  1. anchors -- payees with many distinct on-chain payers (>= ANCHOR_MIN_DISTINCT).
     Funding that many independent, USDC-holding wallets is a real, ongoing cost, so
     a high-diversity payee is the closest thing to ground-truth "real" we have.
  2. payer reputation r(payer) in [0,1] -- saturating on the number of DISTINCT
     anchors a payer pays. A wallet that pays several independently-established
     services is a proven real agent; a ring member that only pays its own cluster
     scores ~0 (the ring holds no anchors).
  3. payee corroboration -- `reputable_payers` (payers with r >= REPUTABLE_PAYER_MIN)
     and a `sybil_ring` flag: clears the distinct gate, yet NOT ONE payer is reputable
     -> the payers are a closed low-trust cluster. Catches the ring the breadth-only
     `captive_sybil` misses.

`PayerReputationSource.cross_signal(payee)` is a SUPERSET of `PayerGraphSource`'s --
it adds the reputation fields + `sybil_ring` -- so it drops straight into
`forecast(graph_source=...)`. The verdict fold stays conservative: `sybil_ring`, like
`captive_sybil`, only escalates GO->HOLD, never STOP, and is fail-open.

Pure + stdlib; edges injected.

Honesty / known limits (audit F1/F2/F4):
  * anchors are just "payees with >= ANCHOR_MIN_DISTINCT distinct payers" -- they are
    Sybil-MINTABLE (fund ~20 wallets to pay two attacker payees -> both become
    anchors). This RAISES the cost of a Sybil, it does not make it impossible.
  * `sybil_ring` fires only when ZERO payers are reputable, so a ring clears it by
    making ONE sockpuppet pay two anchors. That's cheap -- which is exactly why
    `sybil_ring` is ADVISORY (surfaced, not gated) in the verdict; `captive_sybil`
    (stricter, established==0) is the gate.
  * everything is relative to INGESTED coverage, so precision rises with ingestion.
The layer is ADDITIVE Sybil recall for small captive/ring clusters, not a complete
defense. Both signals only ever tighten (HOLD), never STOP.
"""
from __future__ import annotations

import payer_graph as PG

ANCHOR_MIN_DISTINCT = 20          # distinct on-chain payers to qualify as a trust anchor
PAYER_REP_SATURATION = 3          # paying this many distinct anchors saturates r -> 1.0
REPUTABLE_PAYER_MIN = 0.5         # r >= this == a "reputable" payer (>= 2 anchors, here)
# sybil_ring's ceiling, kept equal to payer_graph.CAPTIVE_SYBIL_MAX_DISTINCT so the
# two flags share one ceiling. Past it a zero-reputable set is more likely an
# ingestion-coverage gap (or a large consumer merchant) than an affordable ring.
RING_MAX_DISTINCT = PG.CAPTIVE_SYBIL_MAX_DISTINCT


def anchor_payees(graph, *, min_distinct=ANCHOR_MIN_DISTINCT):
    """Trusted seed: payees with >= `min_distinct` distinct payers (hard to fake)."""
    return {payee for payee, payers in graph["payee_to_payers"].items()
            if len(payers) >= min_distinct}


def payer_reputation(graph, anchors, payer, *, saturation=PAYER_REP_SATURATION):
    """(reputation in [0,1], anchors_paid) for one payer. Saturating on the number
    of DISTINCT anchor payees the payer pays -- a payer that spends across several
    independently-established services is a proven real agent."""
    paid = graph["payer_to_payees"].get(PG._norm(payer), set())
    anchors_paid = len(paid & anchors)
    return round(min(1.0, anchors_paid / max(1, saturation)), 3), anchors_paid


def payer_scores(graph, *, anchors=None, saturation=PAYER_REP_SATURATION):
    """Reputation for every payer in the graph -> {payer: r}. Anchors derived from
    the graph unless supplied."""
    anchors = anchor_payees(graph) if anchors is None else anchors
    return ({p: payer_reputation(graph, anchors, p, saturation=saturation)[0]
             for p in graph["payer_to_payees"]}, anchors)


def payee_corroboration(graph, payer_rep, payee, *, min_reputation=REPUTABLE_PAYER_MIN,
                        min_distinct=PG.MIN_DISTINCT_PAYERS,
                        ring_max_distinct=RING_MAX_DISTINCT):
    """Reputation-weighted view of a payee's payers. None when the payee is absent."""
    payers = graph["payee_to_payers"].get(PG._norm(payee))
    if not payers:
        return None
    distinct = len(payers)
    reps = [payer_rep.get(p, 0.0) for p in payers]
    reputable = sum(1 for r in reps if r >= min_reputation)
    reputable_ratio = round(reputable / distinct, 3)
    avg_payer_rep = round(sum(reps) / distinct, 3)
    # A payer's reputation is > 0 iff it pays at least ONE trusted anchor (rep saturates
    # on distinct anchors paid), so `anchor_connected` counts payers with ANY anchor link.
    anchor_connected = sum(1 for r in reps if r > 0.0)
    # Sybil ring: enough distinct payers to clear the naive gate, yet a CLOSED cluster --
    # NOT ONE payer touches any anchor. (Stage-3.1 refinement, docs/DATA_COMPLETENESS.md:
    # tightened from `reputable == 0`, which also flagged legit-but-UNDER-SATURATED payees
    # whose payers each pay a SINGLE anchor -- anchor-connected, not closed. On the shipped
    # corpus this cleared 6 such payees, 19 -> 13 flagged, all 13 truly anchor-isolated.)
    sybil_ring = (anchor_connected == 0 and min_distinct <= distinct <= ring_max_distinct)
    return {
        "reputable_payers": reputable,
        "reputable_ratio": reputable_ratio,
        "avg_payer_reputation": avg_payer_rep,
        "anchor_connected_payers": anchor_connected,
        "sybil_ring": sybil_ring,
    }


_TIER_SUMMARY = {
    "established": "proven cross-ecosystem agent -- pays %d trusted anchor(s); strong "
                   "positive signal",
    "emerging": "some cross-ecosystem activity (%d anchor(s), %d payee(s)) -- limited "
                "history",
    "unknown": "no cross-ecosystem history -- NEUTRAL (cold start), not a negative "
               "signal",
}


def payer_profile(graph, anchors, payer_rep, payer, *,
                  min_reputation=REPUTABLE_PAYER_MIN,
                  established_min_breadth=PG.ESTABLISHED_MIN_BREADTH):
    """Screen a PAYER (not a payee): what does the ecosystem know about this wallet?
    Pure. A facilitator/wallet screening an inbound payment uses this to fast-track a
    proven agent -- NOT to block unknowns (most real end-users are unknown; that's
    cold-start, a neutral signal, never a negative one)."""
    key = PG._norm(payer)
    paid = graph["payer_to_payees"].get(key, set())
    breadth = len(paid)
    anchors_paid = len(paid & anchors)
    rep = payer_rep.get(key, 0.0)
    reputable = rep >= min_reputation
    if reputable:
        tier = "established"
    elif anchors_paid >= 1 or breadth >= established_min_breadth:
        tier = "emerging"
    else:
        tier = "unknown"
    summary = _TIER_SUMMARY[tier] % (
        (anchors_paid,) if tier == "established"
        else (anchors_paid, breadth) if tier == "emerging" else ())
    return {
        "payer": key,
        "reputation": rep,
        "tier": tier,
        "reputable": reputable,
        "anchors_paid": anchors_paid,
        "payees_paid": breadth,
        "summary": summary,
    }


class PayerReputationSource:
    """Composes the payer graph with propagated payer reputation. `cross_signal` is a
    SUPERSET of PayerGraphSource's (graph fields + reputation fields + sybil_ring), so
    it's a drop-in `graph_source` for forecast. Built once, cached per payee."""

    def __init__(self, edges, *, anchor_min_distinct=ANCHOR_MIN_DISTINCT,
                 saturation=PAYER_REP_SATURATION, min_reputation=REPUTABLE_PAYER_MIN,
                 **graph_kwargs):
        self.graph = PG.build_index(edges)
        self.anchors = anchor_payees(self.graph, min_distinct=anchor_min_distinct)
        self.payer_rep, _ = payer_scores(self.graph, anchors=self.anchors,
                                         saturation=saturation)
        self._min_rep = min_reputation
        self._gkw = graph_kwargs
        self._cache = {}

    @classmethod
    def from_store(cls, store, **kwargs):
        return cls(store.iter_settlement_edges(), **kwargs)

    def reputation(self, payer):
        """The payer's own reputation score in [0,1]."""
        return self.payer_rep.get(PG._norm(payer), 0.0)

    def screen(self, payer):
        """Full reputation profile for a PAYER wallet (the queryable output: a
        facilitator/wallet screening WHO is paying, before it settles)."""
        return payer_profile(self.graph, self.anchors, self.payer_rep, payer,
                             min_reputation=self._min_rep)

    def cross_signal(self, payee):
        key = PG._norm(payee)
        if key not in self._cache:
            base = PG.cross_signal(self.graph, key, **self._gkw)
            if base is None:
                self._cache[key] = None
            else:
                # keep the two flags' shared thresholds in step (audit F6): a custom
                # min_distinct passed for the graph flag also drives the ring flag.
                corr = payee_corroboration(
                    self.graph, self.payer_rep, key, min_reputation=self._min_rep,
                    min_distinct=self._gkw.get("min_distinct", PG.MIN_DISTINCT_PAYERS))
                self._cache[key] = dict(base, **corr)
        return self._cache[key]
