#!/usr/bin/env python3
"""
payer_graph.py -- the cross-counterparty payer graph as a reputation signal.

Per-payee `distinct_payers` counts HOW MANY payers a payee has, but not WHO they
are. A wash-trader funds N throwaway wallets that each pay only its one payee -> N
distinct payers, all CAPTIVE. A legitimate payee is paid by wallets that ALSO pay
other services -- real agents spending across the ecosystem. This module builds the
bipartite payer<->payee graph from ingested settlements and derives, per payee:

  * established_payers -- payers proven to pay >= K ingested payees. ROBUST: it can
    only be understated (never overstated), grows monotonically with ingestion, and
    is hard to fake -- you'd have to actually pay other real services.
  * captive_payers / captive_ratio -- payers that (as far as we've ingested) pay ONLY
    this payee. Sybil-shaped, but RELATIVE to ingestion (see the honesty note).
  * cross_score -- 0..1 corroboration = share of a payee's payers that are established.
  * captive_sybil -- a CONSERVATIVE flag: the payee clears the naive distinct gate,
    yet not one of its payers is seen paying anyone else -> block an automatic GO
    (HOLD), never STOP.

Pure + stdlib; edges are injected so the logic is fully unit-tested. Folds into the
verdict CONSERVATIVELY -- tighten-only (HOLD, not STOP), fail-open when no graph.

Honesty: "captive" is measured over INGESTED payees only. A payer that pays services
we haven't ingested looks captive here, so `captive_ratio` is a FLOOR on Sybil-risk,
not a proof -- which is exactly why it only ever escalates to HOLD (the safe
direction), and its precision RISES as ingestion covers more of the ecosystem.
`established_payers` is the robust half: seeing a payer at two ingested payees is
real regardless of sample size.
"""
from __future__ import annotations

ESTABLISHED_MIN_BREADTH = 2       # pays >= this many distinct payees == "established"
MIN_DISTINCT_PAYERS = 3           # mirror blackwall's naive Sybil gate
# captive_sybil only fires up to this many distinct payers: past it, a fully-captive
# set is more likely an ingestion-coverage artifact than an affordable sockpuppet
# farm, and the raw distinct gate + amount/anomaly gates already apply. Bounded to
# keep false HOLDs off big real payees on a partial sample.
CAPTIVE_SYBIL_MAX_DISTINCT = 8


def _norm(a):
    return a.lower() if isinstance(a, str) else None


def build_index(edges):
    """edges: iterable of (payer, payee). Returns
    {'payer_to_payees': {payer: set(payee)}, 'payee_to_payers': {payee: set(payer)}}.
    Addresses lowercased; blank/None on either side drops the edge. Idempotent
    (sets), so a repeated edge doesn't inflate breadth."""
    p2e, e2p = {}, {}
    for payer, payee in edges or ():
        payer, payee = _norm(payer), _norm(payee)
        if not payer or not payee:
            continue
        p2e.setdefault(payer, set()).add(payee)
        e2p.setdefault(payee, set()).add(payer)
    return {"payer_to_payees": p2e, "payee_to_payers": e2p}


def payer_breadth(graph, payer):
    """Number of DISTINCT payees a payer has paid (its ecosystem breadth)."""
    return len(graph["payer_to_payees"].get(_norm(payer), ()))


def cross_signal(graph, payee, *, established_min_breadth=ESTABLISHED_MIN_BREADTH,
                 min_distinct=MIN_DISTINCT_PAYERS,
                 captive_max_distinct=CAPTIVE_SYBIL_MAX_DISTINCT):
    """Cross-counterparty signal for one payee. Pure. Returns None when the payee is
    absent from the graph (no ingested payers -> no signal, fail-open)."""
    payers = graph["payee_to_payers"].get(_norm(payee))
    if not payers:
        return None
    p2e = graph["payer_to_payees"]
    distinct = len(payers)
    established = sum(1 for p in payers if len(p2e.get(p, ())) >= established_min_breadth)
    captive = distinct - established          # breadth==1 <=> captive (>=1 always)
    captive_ratio = round(captive / distinct, 3)
    cross_score = round(established / distinct, 3)
    # Suspicious pattern: enough distinct payers to clear the naive gate, yet NOT ONE
    # pays any other ingested payee. Bounded by captive_max_distinct.
    captive_sybil = (established == 0
                     and min_distinct <= distinct <= captive_max_distinct)
    return {
        "distinct_payers": distinct,
        "established_payers": established,
        "captive_payers": captive,
        "captive_ratio": captive_ratio,
        "cross_score": cross_score,
        "captive_sybil": captive_sybil,
    }


class PayerGraphSource:
    """Wraps ingested edges into a queryable, cached cross-counterparty source. Built
    once from a store (whole-graph derivation), then answers per-payee off a cache --
    it is NOT rebuilt per lookup."""

    def __init__(self, edges, **signal_kwargs):
        self.graph = build_index(edges)
        self._kw = signal_kwargs
        self._cache = {}

    @classmethod
    def from_store(cls, store, **signal_kwargs):
        return cls(store.iter_settlement_edges(), **signal_kwargs)

    def cross_signal(self, payee):
        key = _norm(payee)
        if key not in self._cache:
            self._cache[key] = cross_signal(self.graph, key, **self._kw)
        return self._cache[key]

    def breadth(self, payer):
        return payer_breadth(self.graph, payer)
