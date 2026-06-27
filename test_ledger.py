"""
Tests for the verdict->outcome ledger and the moat flywheel.

Run: python -m unittest test_ledger.py -v

aggregate_counterparties is the pure core; the flywheel test proves the loop:
a recorded verdict + observed outcome becomes the reputation that drives the
NEXT verdict.
"""
import os
import tempfile
import unittest

import blackwall as bw
import ledger as L


class TestAggregate(unittest.TestCase):
    """
    aggregate_counterparties: event stream -> per-counterparty records.

    Mutation notes:
      - dispute_rate = 0 when no outcomes -> test_unobserved_dispute_is_none FAILS.
      - Don't join outcomes by receipt_id -> test_outcome_attributed FAILS.
      - Count bad outcomes as settlements -> test_dispute_rate FAILS.
    """

    def test_unobserved_dispute_is_none(self):
        # A verdict with no outcome yet: dispute_rate UNKNOWN, not 0.
        events = [{"kind": "verdict", "receipt_id": "bw_1",
                   "counterparty": "0xA", "amount": "0.09", "ts": "t1"}]
        recs = L.aggregate_counterparties(events)
        self.assertIsNone(recs["0xA"]["dispute_rate"])
        self.assertFalse(recs["0xA"]["_meta"]["known"])

    def test_outcome_attributed_via_receipt(self):
        events = [
            {"kind": "verdict", "receipt_id": "bw_1", "counterparty": "0xA",
             "amount": "0.09", "ts": "t1"},
            {"kind": "outcome", "receipt_id": "bw_1", "outcome": "delivered"},
        ]
        recs = L.aggregate_counterparties(events)
        self.assertEqual(recs["0xA"]["settlement_count"], 1)
        self.assertEqual(recs["0xA"]["price_history"], ["0.09"])
        self.assertTrue(recs["0xA"]["_meta"]["known"])

    def test_dispute_rate(self):
        events = [
            {"kind": "verdict", "receipt_id": "r%d" % i, "counterparty": "0xA",
             "amount": "0.09", "ts": "t"} for i in range(5)
        ]
        events += [{"kind": "outcome", "receipt_id": "r%d" % i,
                    "outcome": "delivered"} for i in range(4)]
        events += [{"kind": "outcome", "receipt_id": "r4",
                    "outcome": "disputed"}]
        recs = L.aggregate_counterparties(events)
        # settlement_count is total SETTLED (good + disputed) = 5, so that
        # reputation_score's Beta split by dispute_rate is exact.
        self.assertEqual(recs["0xA"]["settlement_count"], 5)
        self.assertAlmostEqual(recs["0xA"]["dispute_rate"], 0.2)  # 1 of 5

    def test_duplicate_outcome_counts_once(self):
        # Replays / retries must be idempotent (last write wins).
        events = [
            {"kind": "verdict", "receipt_id": "r1", "counterparty": "0xA",
             "amount": "0.09", "ts": "t"},
            {"kind": "outcome", "receipt_id": "r1", "outcome": "delivered"},
            {"kind": "outcome", "receipt_id": "r1", "outcome": "delivered"},
        ]
        recs = L.aggregate_counterparties(events)
        self.assertEqual(recs["0xA"]["settlement_count"], 1)
        self.assertEqual(recs["0xA"]["price_history"], ["0.09"])

    def test_outcome_status_update_last_wins(self):
        # delivered then later disputed -> final state is disputed.
        events = [
            {"kind": "verdict", "receipt_id": "r1", "counterparty": "0xA",
             "amount": "0.09", "ts": "t"},
            {"kind": "outcome", "receipt_id": "r1", "outcome": "delivered"},
            {"kind": "outcome", "receipt_id": "r1", "outcome": "disputed"},
        ]
        recs = L.aggregate_counterparties(events)
        self.assertEqual(recs["0xA"]["settlement_count"], 1)  # still 1 settled
        self.assertAlmostEqual(recs["0xA"]["dispute_rate"], 1.0)

    def test_orphan_outcome_ignored(self):
        events = [{"kind": "outcome", "receipt_id": "ghost",
                   "outcome": "delivered"}]
        self.assertEqual(L.aggregate_counterparties(events), {})


class TestEventLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "led.jsonl")
        self.led = L.EventLedger(self.path)

    def test_roundtrip_and_aggregate(self):
        self.led.record_verdict("bw_x", "0xA", "0.09", "GO", score=0.99)
        self.led.record_outcome("bw_x", "delivered")
        recs = self.led.aggregate()
        self.assertEqual(recs["0xA"]["settlement_count"], 1)

    def test_invalid_outcome_rejected(self):
        with self.assertRaises(ValueError):
            self.led.record_outcome("bw_x", "exploded")

    def test_corrupt_line_skipped(self):
        with open(self.path, "a") as f:
            f.write("not json\n")
        self.led.record_verdict("bw_y", "0xB", "1.0", "HOLD")
        # read survives the corrupt line
        self.assertIn("0xB", self.led.aggregate())


class TestChainedSource(unittest.TestCase):
    def test_ledger_leads_then_falls_back(self):
        # Ledger knows 0xA; mock provides the cold-start default for others.
        led = L.EventLedger(os.path.join(tempfile.mkdtemp(), "l.jsonl"))
        for i in range(3):
            led.record_verdict("r%d" % i, "0xA", "0.09", "GO")
            led.record_outcome("r%d" % i, "delivered")
        chained = L.ChainedReputationSource(
            [L.LedgerReputationSource(led), bw.MockReputationSource()])
        # Known in ledger -> ledger record wins.
        self.assertEqual(chained.lookup("0xA")["_meta"]["source"],
                         "blackwall-ledger")
        # Unknown to ledger but seeded in mock -> falls through to mock.
        rec = chained.lookup("0xSANCTIONED00000000000000000000000000003")
        self.assertTrue(rec["sanctioned"])


class TestFlywheel(unittest.TestCase):
    """The loop: verdict -> outcome -> drives the NEXT verdict's reputation."""

    def test_self_reports_do_NOT_graduate_only_chain_confirmed_do(self):
        # Security property: the verdict's thin-history gate counts only
        # CHAIN-CONFIRMED settlements, so unauthenticated self-reports cannot
        # graduate a counterparty to GO -- only the settlement watcher can.
        path = os.path.join(tempfile.mkdtemp(), "fly.jsonl")
        led = L.EventLedger(path)
        src = L.LedgerReputationSource(led)
        cp = "0xFRESH"
        payload = {"counterparty": cp, "amount": "0.09",
                   "asset": "USDC", "chain": "base"}

        # First contact -> thin -> HOLD.
        resp1, err = bw.forecast(payload, src, ledger=led)
        self.assertIsNone(err)
        self.assertEqual(resp1["verdict"], "HOLD")

        # 25 SELF-REPORTED deliveries -> settlement_count rises, but confirmed
        # stays 0 -> still HOLD (self-reports can't graduate).
        for i in range(25):
            r, _ = bw.forecast(dict(payload), src, ledger=led)
            led.record_outcome(r["receipt_id"], "delivered")  # source=self-report
        resp2, _ = bw.forecast(payload, src, ledger=led)
        self.assertEqual(resp2["verdict"], "HOLD")
        self.assertEqual(src.lookup(cp)["settlement_count"], 25)
        self.assertEqual(src.lookup(cp)["confirmed_settlement_count"], 0)

        # Now 25 CHAIN-CONFIRMED settlements -> graduates to GO.
        for i in range(25):
            r, _ = bw.forecast(dict(payload), src, ledger=led)
            led.record_outcome(r["receipt_id"], "settled", source="chain-watch")
        resp3, _ = bw.forecast(payload, src, ledger=led)
        self.assertEqual(resp3["verdict"], "GO")


if __name__ == "__main__":
    unittest.main()
