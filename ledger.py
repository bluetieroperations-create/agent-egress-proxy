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
    # Two passes (verdicts, then outcomes) -> materialize once so a one-shot
    # generator (EventLedger.read_events) isn't exhausted by the first pass.
    events = list(events)
    by_receipt = {}      # receipt_id -> (counterparty, amount, observed_amount)
    agg = {}             # counterparty -> mutable accumulator

    def acc(cp):
        if cp not in agg:
            agg[cp] = {"good": 0, "bad": 0, "amounts": [], "chain_confirmed": 0,
                       "first_ts": None, "last_ts": None, "verdicts": 0}
        return agg[cp]

    # First pass: index verdicts (so outcomes can attribute regardless of order).
    for e in events:
        if e.get("kind") == "verdict":
            rid = e.get("receipt_id")
            cp = e.get("counterparty")
            if not cp or not rid:
                continue
            by_receipt[rid] = [cp, e.get("amount"), None]
            a = acc(cp)
            a["verdicts"] += 1
            ts = e.get("ts")
            if ts:
                a["first_ts"] = min(a["first_ts"], ts) if a["first_ts"] else ts
                a["last_ts"] = max(a["last_ts"], ts) if a["last_ts"] else ts

    # Second pass: collapse outcomes to ONE per receipt (last write wins). This
    # makes replays / retries / status updates idempotent -- a receipt counts
    # exactly once no matter how many times its outcome is reported.
    final_outcome = {}   # receipt_id -> (outcome, source)
    for e in events:
        if e.get("kind") != "outcome":
            continue
        rid = e.get("receipt_id")
        if rid not in by_receipt:
            continue  # outcome for an unknown receipt -> cannot attribute
        final_outcome[rid] = (e.get("outcome"), e.get("source", "self-report"))
        if e.get("observed_amount") is not None:
            by_receipt[rid][2] = e.get("observed_amount")

    for rid, (outcome, source) in final_outcome.items():
        cp, amount, observed = by_receipt[rid]
        a = acc(cp)
        settled = outcome in GOOD_OUTCOMES or outcome in BAD_OUTCOMES
        if outcome in GOOD_OUTCOMES:
            a["good"] += 1
        elif outcome in BAD_OUTCOMES:
            a["bad"] += 1
        # Settlement counts as CHAIN-CONFIRMED only when a trustless watcher
        # wrote it -- this is the un-fakeable backbone the self-report can't be.
        if settled and source == "chain-watch":
            a["chain_confirmed"] += 1
        # Price history is "what this counterparty charged on settled payments"
        # -- include disputed ones too (the charge is real regardless of
        # delivery); exclude NEUTRAL (abandoned, never settled).
        if settled:
            amt = observed if observed is not None else amount
            if amt is not None:
                a["amounts"].append(str(amt))

    records = {}
    for cp, a in agg.items():
        good, bad = a["good"], a["bad"]
        decided = good + bad     # total SETTLED payments (good + disputed)
        dispute_rate = (bad / decided) if decided else None
        records[cp] = {
            # settlement_count is the total settled population that
            # reputation_score splits by dispute_rate -- so it is good+bad, not
            # good-only, which makes the Beta posterior exact.
            "settlement_count": decided,
            "dispute_rate": dispute_rate,   # None until an outcome is observed
            "price_history": a["amounts"],
            "first_seen": a["first_ts"],
            "last_seen": a["last_ts"],
            "_meta": {
                "source": "blackwall-ledger",
                "known": decided > 0,
                "verdicts_seen": a["verdicts"],
                "outcomes_seen": decided,
                "dispute_rate_is_observed": decided > 0,
                # How many of the settlements are trustless (chain-confirmed) vs
                # merely self-reported. A production GO policy should gate on the
                # confirmed count, not the raw one.
                "chain_confirmed_settlements": a["chain_confirmed"],
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
                       asset=None, chain=None, ts=None):
        self._append({
            "kind": "verdict",
            "ts": ts or _now(),
            "receipt_id": receipt_id,
            "agent_id": agent_id,
            "counterparty": counterparty,
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
        last = None
        for src in self.sources:
            rec = src.lookup(counterparty)
            last = rec
            known = (rec.get("_meta", {}) or {}).get("known")
            if known or rec.get("sanctioned") or rec.get("known_bad") \
                    or (rec.get("settlement_count") or 0) > 0:
                return rec
        return last


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
