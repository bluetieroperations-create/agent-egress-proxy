#!/usr/bin/env python3
"""
test_rwa_outcomes.py -- TDD for the outcome-capture loop (labeling the corpus) and the
outcome-aware issuer/asset rollups. Each test names the MUTATION it kills.
"""
import os
import tempfile
import unittest

from rwa_ledger import (ISSUER_TRUST_MIN_OUTCOMES, RwaLedger, build_outcome_event,
                        issuer_trust)
from rwa_outcomes import OutcomeChecker, assess_outcome, capture_outcomes

EVM = "0xF758E87CA18824b767aa4F3ed58C188d3bABE428"


def _buy(receipt_id="r1", ts=100, paid=110.0, u0=100.0, issuer="backed",
         token=EVM, chain="base", verdict="GO", peg=1.10):
    return {"event": "buy", "receipt_id": receipt_id, "ts": ts, "token": token,
            "chain": chain, "issuer": issuer, "symbol": "AAPLx",
            "underlying_symbol": "AAPL", "underlying_price": u0, "paid_unit_price": paid,
            "peg_ratio": peg, "verdict": verdict, "payer": "0x" + "11" * 20,
            "trading_halted": False}


class TestAssessOutcome(unittest.TestCase):
    def test_underwater_when_stock_below_paid(self):
        # Paid 110, underlying now 90 -> mark 90/110 < 1 -> underwater.
        # MUTATION: mark = paid/underlying (inverted) would flip the label.
        o = assess_outcome(_buy(paid=110.0, u0=100.0), underlying_now=90.0)
        self.assertLess(o["mark_ratio"], 1.0)
        self.assertTrue(o["underwater"])
        self.assertAlmostEqual(o["underlying_move"], 0.90)

    def test_in_profit_when_stock_above_paid(self):
        o = assess_outcome(_buy(paid=100.0, u0=100.0), underlying_now=120.0)
        self.assertGreater(o["mark_ratio"], 1.0)
        self.assertFalse(o["underwater"])

    def test_missing_price_yields_none_not_fabricated(self):
        # MUTATION: defaulting to a rate (e.g. mark=1.0) when the oracle is down would
        # invent evidence. Must be None.
        o = assess_outcome(_buy(), underlying_now=None)
        self.assertIsNone(o["mark_ratio"])
        self.assertIsNone(o["underwater"])

    def test_holds_balance_passthrough(self):
        self.assertTrue(assess_outcome(_buy(), 100.0, holds_balance=True)["holds_balance"])
        self.assertIsNone(assess_outcome(_buy(), 100.0, holds_balance="yes")["holds_balance"])


class TestChecker(unittest.TestCase):
    class _Pyth:
        def __init__(self, px):
            self.px = px

        def price(self, sym):
            return self.px

    def test_check_uses_pyth_and_balance_reader(self):
        class Bal:
            def balance_of(self, t, c, p):
                return 5             # positive -> holds True
        chk = OutcomeChecker(self._Pyth(90.0), balance_reader=Bal())
        o = chk.check(_buy(paid=110.0))
        self.assertTrue(o["underwater"])
        self.assertTrue(o["holds_balance"])

    def test_pyth_raises_fail_open(self):
        class Boom:
            def price(self, sym):
                raise RuntimeError("down")
        o = OutcomeChecker(Boom()).check(_buy())
        self.assertIsNone(o["mark_ratio"])

    class _Bal:
        def __init__(self, post):
            self.post = post

        def balance_of(self, token, chain, payer):
            return self.post

    def test_definitive_settled_from_delta(self):
        # buy captured pre_balance=100; post=250 -> arrived -> settled True + holds True.
        buy = dict(_buy(), pre_balance=100)
        o = OutcomeChecker(self._Pyth(90.0), balance_reader=self._Bal(250)).check(buy)
        self.assertTrue(o["settled"])
        self.assertTrue(o["holds_balance"])

    def test_settled_false_when_no_arrival(self):
        # MUTATION: settled = post>0 (holds) instead of post>pre would MISS a
        # pre-existing balance that never grew -> false "settled".
        buy = dict(_buy(), pre_balance=100)
        o = OutcomeChecker(self._Pyth(90.0), balance_reader=self._Bal(100)).check(buy)
        self.assertFalse(o["settled"])       # 100 not > 100
        self.assertTrue(o["holds_balance"])  # but still holds a positive balance

    def test_settled_none_without_pre_snapshot(self):
        # No pre_balance on the buy -> can't compute the delta -> settled None (heuristic
        # holds still available).
        o = OutcomeChecker(self._Pyth(90.0), balance_reader=self._Bal(250)).check(_buy())
        self.assertIsNone(o["settled"])
        self.assertTrue(o["holds_balance"])


class TestCaptureLoop(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "rwa.jsonl")
        self.led = RwaLedger(self.path)

    class _Pyth:
        def price(self, sym):
            return 90.0

    def test_pending_selects_old_unlabeled_only(self):
        self.led.record(_buy(receipt_id="old", ts=100))
        self.led.record(_buy(receipt_id="new", ts=1000))
        # horizon 500s, now 1000 -> only "old" (age 900 >= 500) qualifies; "new" age 0.
        pending = self.led.pending_buys(500, now=1000)
        self.assertEqual([b["receipt_id"] for b in pending], ["old"])

    def test_capture_records_and_is_idempotent(self):
        self.led.record(_buy(receipt_id="old", ts=100, paid=110.0, u0=100.0))
        n1 = capture_outcomes(self.led, OutcomeChecker(self._Pyth()), 500, now=1000)
        n2 = capture_outcomes(self.led, OutcomeChecker(self._Pyth()), 500, now=2000)
        self.assertEqual(n1, 1)
        # MUTATION: not excluding already-labeled buys would double-count every run.
        self.assertEqual(n2, 0)
        outs = [e for e in self.led.load() if e.get("event") == "outcome"]
        self.assertEqual(len(outs), 1)
        self.assertEqual(outs[0]["receipt_id"], "old")
        self.assertTrue(outs[0]["underwater"])

    def test_buy_without_receipt_id_never_pending(self):
        # MUTATION: a buy with no join key can't be labeled; including it would make it
        # pending forever.
        self.led.record(_buy(receipt_id=None, ts=100))
        self.assertEqual(self.led.pending_buys(500, now=1000), [])


class TestOutcomeAwareProfiles(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "rwa.jsonl")
        self.led = RwaLedger(self.path)

    def test_asset_profile_folds_outcomes(self):
        self.led.record(_buy(receipt_id="r1", verdict="GO"))
        self.led.record(build_outcome_event(_buy(receipt_id="r1"),
                        {"mark_ratio": 0.8, "underwater": True, "holds_balance": True},
                        now=2000))
        p = self.led.asset_profile(EVM, "base")
        self.assertEqual(p["count"], 1)              # buys only
        self.assertEqual(p["outcomes"], 1)
        self.assertEqual(p["underwater_rate"], 1.0)
        self.assertEqual(p["settlement_success_rate"], 1.0)
        self.assertAlmostEqual(p["avg_mark_ratio"], 0.8)

    def test_issuer_trust_grades(self):
        # 6 labeled outcomes, all settled, none underwater -> high.
        for i in range(6):
            self.led.record(_buy(receipt_id="r%d" % i, verdict="GO"))
            self.led.record(build_outcome_event(_buy(receipt_id="r%d" % i),
                            {"mark_ratio": 1.1, "underwater": False, "holds_balance": True},
                            now=2000))
        prof = self.led.issuer_profile("backed")
        self.assertEqual(issuer_trust(prof)["grade"], "high")

    def test_issuer_trust_insufficient_below_min(self):
        # MUTATION: grading on too little evidence ("no evidence is not trust").
        self.led.record(_buy(receipt_id="r1"))
        self.led.record(build_outcome_event(_buy(receipt_id="r1"),
                        {"mark_ratio": 1.1, "underwater": False, "holds_balance": True},
                        now=2000))
        prof = self.led.issuer_profile("backed")
        self.assertEqual(issuer_trust(prof)["grade"], "insufficient")

    def test_definitive_settled_preferred_over_heuristic(self):
        # MUTATION: averaging holds_balance instead of preferring `settled` would report
        # 100% settled when the definitive delta says it did NOT arrive.
        self.led.record(_buy(receipt_id="r1"))
        self.led.record(build_outcome_event(_buy(receipt_id="r1"),
                        {"mark_ratio": 1.0, "underwater": False,
                         "holds_balance": True, "settled": False}, now=2000))
        p = self.led.asset_profile(EVM, "base")
        self.assertEqual(p["settlement_success_rate"], 0.0)   # definitive wins
        self.assertEqual(p["definitive_settlements"], 1)

    def test_issuer_trust_unlabeled_outcomes_insufficient(self):
        # AUDIT REGRESSION: outcome EVENTS with no usable label (oracle down at capture)
        # are NOT evidence -- must grade 'insufficient', never a false 'medium'.
        for i in range(6):
            self.led.record(_buy(receipt_id="r%d" % i))
            self.led.record(build_outcome_event(_buy(receipt_id="r%d" % i),
                            {"mark_ratio": None, "underwater": None, "holds_balance": None},
                            now=2000))
        prof = self.led.issuer_profile("backed")
        self.assertEqual(issuer_trust(prof)["grade"], "insufficient")

    def test_issuer_trust_low_on_bad_outcomes(self):
        for i in range(6):
            self.led.record(_buy(receipt_id="r%d" % i, verdict="STOP"))
            self.led.record(build_outcome_event(_buy(receipt_id="r%d" % i),
                            {"mark_ratio": 0.5, "underwater": True, "holds_balance": False},
                            now=2000))
        prof = self.led.issuer_profile("backed")
        self.assertEqual(issuer_trust(prof)["grade"], "low")


if __name__ == "__main__":
    unittest.main()
