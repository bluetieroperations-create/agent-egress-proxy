#!/usr/bin/env python3
"""
ledger.py -- Blackwall's verdict -> outcome ledger (the moat's write path).

The data-source spike (docs/DATA_SOURCE_SPIKE.md) found that the *durable* moat
is NOT data Blackwall can read from a public indexer -- it is data Blackwall
ACCUMULATES itself: every verdict it issued, and what that payment actually did
(settled / delivered / underdelivered / disputed / refunded). On-chain shows
volume; only this ledger shows DISPUTE rate, which is the signal a stateless
facilitator can't reconstruct.

The flywheel this closes:

    forecast() --writes--> VERDICT event
                              │
            (settlement watch / agent report / inference)
                              ▼
                           OUTCOME event  (joined by receipt_id)
                              │
                    aggregate_counterparties()
                              ▼
                 LedgerReputationSource.lookup()  --feeds--> next forecast()

Append-only JSONL (same ethos as the egress proxy's egress.log), stdlib only.
`aggregate_counterparties` is pure and unit-tested offline.
"""
from __future__ import annotations

import json
import os
import threading
import time
from decimal import Decimal

# Outcome taxonomy. The two axes Blackwall cares about: did it SETTLE (money
# moved) and did it DELIVER (the agent got what it paid for).
GOOD_OUTCOMES = {"settled", "delivered"}        # counts toward settlement_count
BAD_OUTCOMES = {"underdelivered", "disputed", "refunded"}  # counts toward disputes
NEUTRAL_OUTCOMES = {"abandoned"}                # GO issued but agent never paid

VALID_OUTCOMES = GOOD_OUTCOMES | BAD_OUTCOMES | NEUTRAL_OUTCOMES
THIN_HISTORY_SETTLEMENTS = 20  # mirrors blackwall.THIN_HISTORY_SETTLEMENTS


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ===========================================================================
# Pure aggregation (unit-tested offline)
# ===========================================================================
def aggregate_counterparties(events):
    """
    PURE: fold an event stream into per-counterparty reputation records.

    `events` is an iterable of dicts. A `verdict` event carries
    {kind:"verdict", receipt_id, counterparty, amount, ts, ...}; an `outcome`
    event carries {kind:"outcome", receipt_id, outcome, ts, observed_amount?}.
    Outcomes are joined back to their counterparty via receipt_id.

    Returns {counterparty: record} where record has exactly the keys
    blackwall.decide_payment reads -- settlement_count, dispute_rate,
    price_history -- plus age/velocity/_meta. This is what makes the ledger a
    drop-in reputation source.
    """
    # Materialize once (read_events is a one-shot generator).
    #
    # TRUST MODEL: every verdict-affecting signal (settlement_count, dispute_rate,
    # price_history) is anchored to CHAIN-CONFIRMED settlements only -- a receipt
    # that the settlement watcher confirmed on-chain (source="chain-watch"). A
    # self-report on a receipt that never settled on-chain contributes NOTHING to
    # the verdict (it is advisory metadata). This closes the self-report poisoning
    # channels: you cannot dispute, price-anomaly-poison, or inflate a counterparty
    # you never actually paid. Among confirmed receipts, the latest delivery
    # outcome (which the payer may self-report) sets quality (good vs disputed) --
    # you can only dispute a payment that really happened.
    events = list(events)
    by_receipt = {}      # receipt_id -> {cp, amount}
    agg = {}

    def acc(cp):
        if cp not in agg:
            agg[cp] = {"good": 0, "bad": 0, "amounts": [], "confirmed_txs": set(),
                       "first_ts": None, "last_ts": None, "verdicts": 0,
                       "self_reports": 0}
        return agg[cp]

    for e in events:
        if e.get("kind") == "verdict":
            rid = e.get("receipt_id")
            cp = e.get("counterparty")
            if not cp or not rid:
                continue
            by_receipt[rid] = {"cp": cp, "amount": e.get("amount")}
            a = acc(cp)
            a["verdicts"] += 1
            ts = e.get("ts")
            if ts:
                a["first_ts"] = min(a["first_ts"], ts) if a["first_ts"] else ts
                a["last_ts"] = max(a["last_ts"], ts) if a["last_ts"] else ts

    # Per receipt: the STICKY chain-watch confirmation (first one wins -- a later
    # self-report can't erase it) and the LATEST delivery outcome (any source).
    chain_tx = {}        # rid -> settlement_tx of the chain-watch confirmation
    chain_amount = {}    # rid -> on-chain observed amount from that confirmation
    latest_quality = {}  # rid -> latest outcome (delivery quality)
    self_reported = set()
    for e in events:
        if e.get("kind") != "outcome":
            continue
        rid = e.get("receipt_id")
        if rid not in by_receipt:
            continue  # outcome for an unknown receipt -> cannot attribute
        outcome = e.get("outcome")
        source = e.get("source", "self-report")
        latest_quality[rid] = outcome
        if source != "chain-watch":
            self_reported.add(rid)
        elif outcome in GOOD_OUTCOMES and rid not in chain_tx:
            chain_tx[rid] = e.get("settlement_tx")   # sticky
            if e.get("observed_amount") is not None:
                chain_amount[rid] = e.get("observed_amount")

    # Only CHAIN-CONFIRMED receipts move the signals; dedup by settlement_tx so
    # one on-chain payment confirms at most one settlement for a counterparty.
    for rid, tx in chain_tx.items():
        a = acc(by_receipt[rid]["cp"])
        if tx is not None:
            if tx in a["confirmed_txs"]:
                continue
            a["confirmed_txs"].add(tx)
        if latest_quality.get(rid) in BAD_OUTCOMES:
            a["bad"] += 1     # payer disputed a payment that really settled
        else:
            a["good"] += 1
        amt = chain_amount.get(rid)
        if amt is None:
            amt = by_receipt[rid]["amount"]
        if amt is not None:
            a["amounts"].append(str(amt))

    # Advisory only: self-reports on receipts with no chain confirmation.
    for rid in self_reported:
        if rid not in chain_tx:
            acc(by_receipt[rid]["cp"])["self_reports"] += 1

    records = {}
    for cp, a in agg.items():
        good, bad = a["good"], a["bad"]
        confirmed = good + bad   # all chain-confirmed (deduped by tx)
        dispute_rate = (bad / confirmed) if confirmed else None
        records[cp] = {
            # settlement_count == confirmed: the ledger's reputation is built ONLY
            # from chain-confirmed settlements, so the count cannot be self-report
            # inflated. reputation_score's Beta splits it by dispute_rate.
            "settlement_count": confirmed,
            "confirmed_settlement_count": confirmed,
            "dispute_rate": dispute_rate,   # over confirmed settlements only
            "price_history": a["amounts"],  # on-chain amounts of confirmed settlements
            "first_seen": a["first_ts"],
            "last_seen": a["last_ts"],
            "_meta": {
                "source": "blackwall-ledger",
                "known": confirmed > 0,
                "verdicts_seen": a["verdicts"],
                "outcomes_seen": confirmed,
                "dispute_rate_is_observed": confirmed > 0,
                "chain_confirmed_settlements": confirmed,
                # self-reports that never reached chain confirmation (ignored
                # for the verdict; surfaced for transparency only).
                "advisory_self_reports": a["self_reports"],
            },
        }
    return records


# ===========================================================================
# Append-only event store
# ===========================================================================
class EventLedger:
    """Thread-safe append-only JSONL ledger of verdict + outcome events."""

    def __init__(self, path="blackwall_ledger.jsonl"):
        self.path = path
        self._lock = threading.Lock()

    def _append(self, rec):
        line = json.dumps(rec, separators=(",", ":"))
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def record_verdict(self, receipt_id, counterparty, amount, verdict,
                       score=None, agent_id=None, resource=None,
                       asset=None, chain=None, ts=None, payer=None):
        self._append({
            "kind": "verdict",
            "ts": ts or _now(),
            "receipt_id": receipt_id,
            "agent_id": agent_id,
            "counterparty": counterparty,
            # payer = the agent's on-chain wallet; binds settlement confirmation
            # to THIS agent so a third party's payment can't confirm this receipt.
            "payer": payer,
            "resource": resource,
            "amount": str(amount),
            "asset": asset,
            "chain": chain,
            "verdict": verdict,
            "score": score,
        })

    def record_outcome(self, receipt_id, outcome, observed_amount=None,
                       settlement_tx=None, ts=None, source="self-report"):
        if outcome not in VALID_OUTCOMES:
            raise ValueError("unknown outcome %r (valid: %s)"
                             % (outcome, sorted(VALID_OUTCOMES)))
        self._append({
            "kind": "outcome",
            "ts": ts or _now(),
            "receipt_id": receipt_id,
            "outcome": outcome,
            "observed_amount": None if observed_amount is None else str(observed_amount),
            "settlement_tx": settlement_tx,
            # "chain-watch" = trustless on-chain confirmation; "self-report" =
            # claimed by a caller (unauthenticated -- weight accordingly).
            "source": source,
        })

    def read_events(self):
        """Yield every event in append order. Missing file -> empty."""
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except ValueError:
                    continue  # skip a corrupt line rather than fail the read

    def aggregate(self):
        return aggregate_counterparties(self.read_events())


# ===========================================================================
# Ledger as a reputation source (drop-in for MockReputationSource)
# ===========================================================================
class LedgerReputationSource:
    """
    Blackwall's OWN accumulated reputation, behind the same lookup() seam.

    Re-aggregates on lookup (fine for the scaffold; a real deployment keeps a
    rolling in-memory aggregate updated on append). Unknown counterparty -> a
    thin record with _meta.known=False, so a chained source can enrich it.
    """

    def __init__(self, ledger):
        self.ledger = ledger

    def lookup(self, counterparty):
        records = self.ledger.aggregate()
        rec = records.get(counterparty)
        if rec is not None:
            rec.setdefault("sanctioned", False)
            rec.setdefault("known_bad", False)
            return rec
        return {
            "settlement_count": 0,
            "confirmed_settlement_count": 0,
            "dispute_rate": None,
            "price_history": [],
            "sanctioned": False,
            "known_bad": False,
            "_meta": {"source": "blackwall-ledger", "known": False},
        }


class ChainedReputationSource:
    """
    Try sources in order; return the first that reports a KNOWN counterparty
    (_meta.known truthy, or sanctioned/known_bad set). Else the last source's
    record (the thin/unknown default). Lets the ledger lead and an on-chain /
    bootstrap source fill the cold-start gap.
    """

    def __init__(self, sources):
        if not sources:
            raise ValueError("need at least one source")
        self.sources = list(sources)

    def lookup(self, counterparty):
        # sanctioned / known_bad are safety hard-STOPs: they must be OR-ed across
        # ALL sources, never masked by an earlier source's absence/False. So we
        # consult every source (no short-circuit) for those flags, while still
        # taking the FIRST known source's record as the reputation data. (A
        # leading ledger record with a self-report used to mask a downstream
        # sanctions source -> a sanctions bypass; this closes it.)
        primary = None
        last = None
        sanctioned = False
        known_bad = False
        for src in self.sources:
            rec = src.lookup(counterparty)
            last = rec
            sanctioned = sanctioned or bool(rec.get("sanctioned"))
            known_bad = known_bad or bool(rec.get("known_bad"))
            if primary is None:
                known = (rec.get("_meta", {}) or {}).get("known") \
                    or (rec.get("settlement_count") or 0) > 0 \
                    or rec.get("sanctioned") or rec.get("known_bad")
                if known:
                    primary = rec
        result = dict(primary if primary is not None else last)
        result["sanctioned"] = bool(result.get("sanctioned")) or sanctioned
        result["known_bad"] = bool(result.get("known_bad")) or known_bad
        return result


if __name__ == "__main__":
    # Tiny CLI: dump per-counterparty stats from a ledger file.
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "blackwall_ledger.jsonl"
    led = EventLedger(path)
    for cp, rec in led.aggregate().items():
        dr = rec["dispute_rate"]
        print("%s  settlements=%d dispute_rate=%s verdicts=%d"
              % (cp, rec["settlement_count"],
                 "n/a" if dr is None else "%.1f%%" % (dr * 100),
                 rec["_meta"]["verdicts_seen"]))
