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


def verdict(rid, cp, amount="0.09", ts="t"):
    return {"kind": "verdict", "receipt_id": rid, "counterparty": cp,
            "amount": amount, "ts": ts}


def chain_settled(rid, tx, observed_amount=None):
    # A CHAIN-WATCH confirmation -- the only kind that counts toward reputation.
    e = {"kind": "outcome", "receipt_id": rid, "outcome": "settled",
         "source": "chain-watch", "settlement_tx": tx}
    if observed_amount is not None:
        e["observed_amount"] = observed_amount
    return e


def self_report(rid, outcome):
    return {"kind": "outcome", "receipt_id": rid, "outcome": outcome}


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

    def test_chain_confirmed_counts_self_report_does_not(self):
        # Only a chain-watch confirmation counts; a self-report is advisory.
        recs = L.aggregate_counterparties([
            verdict("bw_1", "0xA"), chain_settled("bw_1", "0xtx1")])
        self.assertEqual(recs["0xA"]["settlement_count"], 1)
        self.assertEqual(recs["0xA"]["price_history"], ["0.09"])
        self.assertTrue(recs["0xA"]["_meta"]["known"])
        # a self-report alone -> NOT counted, NOT known.
        recs2 = L.aggregate_counterparties([
            verdict("bw_2", "0xB"), self_report("bw_2", "delivered")])
        self.assertEqual(recs2["0xB"]["settlement_count"], 0)
        self.assertFalse(recs2["0xB"]["_meta"]["known"])
        self.assertEqual(recs2["0xB"]["_meta"]["advisory_self_reports"], 1)

    def test_dispute_rate_over_confirmed(self):
        # 5 chain-confirmed settlements; the payer disputes one of them.
        events = [verdict("r%d" % i, "0xA") for i in range(5)]
        events += [chain_settled("r%d" % i, "0xtx%d" % i) for i in range(5)]
        events += [self_report("r4", "disputed")]  # quality of a REAL settlement
        recs = L.aggregate_counterparties(events)
        self.assertEqual(recs["0xA"]["settlement_count"], 5)
        self.assertAlmostEqual(recs["0xA"]["dispute_rate"], 0.2)  # 1 of 5

    def test_self_reported_dispute_on_unsettled_is_ignored(self):
        # Dispute on a receipt that never chain-settled -> no effect (anti-poison).
        recs = L.aggregate_counterparties([
            verdict("r1", "0xA"), self_report("r1", "disputed")])
        self.assertEqual(recs["0xA"]["settlement_count"], 0)
        self.assertIsNone(recs["0xA"]["dispute_rate"])

    def test_one_tx_confirms_once(self):
        # The SAME settlement_tx across two receipts confirms at most one.
        recs = L.aggregate_counterparties([
            verdict("r1", "0xA"), verdict("r2", "0xA"),
            chain_settled("r1", "0xSAME"), chain_settled("r2", "0xSAME")])
        self.assertEqual(recs["0xA"]["settlement_count"], 1)

    def test_chain_confirmation_is_sticky(self):
        # A later self-report must NOT erase a real chain confirmation.
        recs = L.aggregate_counterparties([
            verdict("r1", "0xA"), chain_settled("r1", "0xtx"),
            self_report("r1", "disputed")])
        self.assertEqual(recs["0xA"]["settlement_count"], 1)   # still confirmed
        self.assertAlmostEqual(recs["0xA"]["dispute_rate"], 1.0)  # quality bad

    def test_price_history_only_from_confirmed_observed_amount(self):
        # Self-reported observed_amount must NOT poison price_history.
        recs = L.aggregate_counterparties([
            verdict("r1", "0xA", amount="0.09"),
            {"kind": "outcome", "receipt_id": "r1", "outcome": "settled",
             "observed_amount": "5.00"},  # self-report (no chain-watch source)
        ])
        self.assertEqual(recs["0xA"]["price_history"], [])  # ignored

    def test_price_observations_are_payer_attributed(self):
        # Confirmed settlements carry {payer, amount} so the verdict engine can
        # build a wash-trade-resistant median. Payer-less verdicts contribute to
        # price_history but NOT to price_observations.
        events = [
            {"kind": "verdict", "receipt_id": "r1", "counterparty": "0xC",
             "amount": "5.00", "payer": "0xpayerA", "ts": "t1"},
            chain_settled("r1", "0xtx1"),
            {"kind": "verdict", "receipt_id": "r2", "counterparty": "0xC",
             "amount": "6.00", "payer": "0xpayerB", "ts": "t2"},
            chain_settled("r2", "0xtx2"),
            {"kind": "verdict", "receipt_id": "r3", "counterparty": "0xC",
             "amount": "7.00", "ts": "t3"},  # no payer
            chain_settled("r3", "0xtx3"),
        ]
        rec = L.aggregate_counterparties(events)["0xC"]
        obs = rec["price_observations"]
        self.assertEqual(len(obs), 2)  # only the two payer-bound settlements
        self.assertEqual({o["payer"] for o in obs}, {"0xpayerA", "0xpayerB"})
        self.assertEqual({o["amount"] for o in obs}, {"5.00", "6.00"})
        # price_history still includes the payer-less one
        self.assertEqual(len(rec["price_history"]), 3)
        # and it flows into the wash-resistant median path
        med, n = bw.robust_price_median(obs, min_payers=2)
        self.assertEqual(n, 2)

    def test_price_observations_carry_resource(self):
        # REGRESSION: the verdict's `resource` must flow into the observation so
        # the engine can do per-invoice-class price comparison. Mutation: drop the
        # resource tag in aggregate -> this FAILS (obs has no resource).
        events = [
            {"kind": "verdict", "receipt_id": "r1", "counterparty": "0xC",
             "amount": "5000", "payer": "0xpayerA", "resource": "INV", "ts": "t1"},
            chain_settled("r1", "0xtx1"),
        ]
        rec = L.aggregate_counterparties(events)["0xC"]
        self.assertEqual(rec["price_observations"][0]["resource"], "INV")

    def test_orphan_outcome_ignored(self):
        events = [chain_settled("ghost", "0xtx")]
        self.assertEqual(L.aggregate_counterparties(events), {})


class TestEventLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "led.jsonl")
        self.led = L.EventLedger(self.path)

    def test_roundtrip_and_aggregate(self):
        self.led.record_verdict("bw_x", "0xA", "0.09", "GO", score=0.99)
        self.led.record_outcome("bw_x", "settled", settlement_tx="0xtx",
                                source="chain-watch")
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
            led.record_outcome("r%d" % i, "settled", settlement_tx="0xtx%d" % i,
                               source="chain-watch")
        chained = L.ChainedReputationSource(
            [L.LedgerReputationSource(led), bw.MockReputationSource()])
        # Known in ledger -> ledger record wins.
        self.assertEqual(chained.lookup("0xA")["_meta"]["source"],
                         "blackwall-ledger")
        # Unknown to ledger but seeded in mock -> falls through to mock.
        rec = chained.lookup("0xSANCTIONED00000000000000000000000000003")
        self.assertTrue(rec["sanctioned"])

    def test_sanctioned_not_masked_by_leading_self_report(self):
        # A self-report on a sanctioned counterparty must NOT let the leading
        # ledger source mask the sanctions flag from a downstream source.
        class SanctSrc:
            def lookup(self, c):
                return {"settlement_count": 0, "sanctioned": True,
                        "price_history": [], "_meta": {"known": True}}
        led = L.EventLedger(os.path.join(tempfile.mkdtemp(), "l.jsonl"))
        led.record_verdict("r1", "0xSANC", "0.09", "STOP")
        led.record_outcome("r1", "settled")  # self-report
        chained = L.ChainedReputationSource(
            [L.LedgerReputationSource(led), SanctSrc()])
        self.assertTrue(chained.lookup("0xSANC")["sanctioned"])


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

        # 25 SELF-REPORTED deliveries -> do NOT count at all (advisory only) ->
        # still HOLD (self-reports can't graduate).
        for i in range(25):
            r, _ = bw.forecast(dict(payload), src, ledger=led)
            led.record_outcome(r["receipt_id"], "delivered")  # source=self-report
        resp2, _ = bw.forecast(payload, src, ledger=led)
        self.assertEqual(resp2["verdict"], "HOLD")
        self.assertEqual(src.lookup(cp)["settlement_count"], 0)
        self.assertEqual(src.lookup(cp)["confirmed_settlement_count"], 0)

        # Now 25 CHAIN-CONFIRMED settlements from 5 distinct payers -> GO.
        for i in range(25):
            p = dict(payload, payer="0x" + ("%040x" % (i % 5)))
            r, _ = bw.forecast(p, src, ledger=led)
            led.record_outcome(r["receipt_id"], "settled", settlement_tx="0xtx%d" % i,
                               source="chain-watch")
        resp3, _ = bw.forecast(payload, src, ledger=led)
        self.assertEqual(resp3["verdict"], "GO")


if __name__ == "__main__":
    unittest.main()
