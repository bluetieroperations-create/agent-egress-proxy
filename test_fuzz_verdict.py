"""
Tests for fuzz_verdict.py -- the property-based verdict fuzzer.

Two jobs: (1) a fixed-seed batch must find ZERO invariant violations (a regression
guard on the whole signal stack), and (2) the invariant CHECKER must actually detect a
violation when one is present -- otherwise "0 violations" would be vacuous.
"""
import unittest

import fuzz_verdict as F


class TestFuzzRun(unittest.TestCase):
    def test_fixed_seed_no_violations(self):
        # deterministic: if a future change breaks a safety invariant, this fails with
        # the reproducing case + seed.
        rep = F.run(iterations=5000, seed=1337)
        self.assertEqual(rep["violations"], [], "invariant violations: %s"
                         % rep["violations"][:3])

    def test_exercises_blockable_and_go(self):
        # guard against a vacuous fuzzer: the 5000-case batch must hit BOTH blockable
        # (STOP) and GO paths, or it isn't testing P2/P6.
        import random
        rng = random.Random(1337)
        seen = set()
        for _ in range(5000):
            kw = F.random_case(rng)
            import blackwall as bw
            seen.add(bw.decide_payment(**kw)["verdict"])
            if F._blockable(kw):
                seen.add("blockable")
        self.assertIn("GO", seen)
        self.assertIn("blockable", seen)


class TestForecastFuzz(unittest.TestCase):
    """forecast() end-to-end: validation, source lookup, payload-sim/calldata, wiring."""

    def test_fixed_seed_no_violations(self):
        rep = F.run_forecast(iterations=3000, seed=1337)
        self.assertEqual(rep["violations"], [], "forecast invariant violations: %s"
                         % rep["violations"][:3])

    def test_exercises_error_success_and_raising_sources(self):
        # non-vacuous: the batch must hit validation errors, successes, AND survive a
        # raising graph/velocity source (the fail-open contract) without a P7 crash.
        import random
        import blackwall as bw
        rng = random.Random(1337)
        errors = success = survived_raise = 0
        for _ in range(3000):
            payload = F.random_payload(rng)
            record = F.random_record(rng)
            raising = rng.random() < 0.3
            graph = F._MockGraph(rng, raises=raising) if raising else None
            resp, err = bw.forecast(payload, F._SingleSource(record), None,
                                    graph_source=graph)
            if err is not None:
                errors += 1
            else:
                success += 1
            if raising:
                survived_raise += 1   # no exception escaped -> fail-open held
        self.assertGreater(errors, 0)          # validation error path hit
        self.assertGreater(success, 0)         # success path hit
        self.assertGreater(survived_raise, 0)  # raising source survived


class TestFuzzerCatchesRegressions(unittest.TestCase):
    """Meta-test: the fuzzer must CATCH a real safety regression end-to-end (generator
    -> engine -> checker), not just pass on a healthy engine. Inject a bug into
    decide_payment and confirm run() reports violations. Guards the fuzzer's ongoing
    value as the engine evolves."""

    def _run_with_mutant(self, mutant):
        import blackwall as bw
        orig = bw.decide_payment
        bw.decide_payment = mutant
        try:
            return F.run(iterations=3000, seed=1337)
        finally:
            bw.decide_payment = orig

    def test_catches_ignored_sanctions(self):
        import blackwall as bw
        orig = bw.decide_payment

        def ignore_sanctions(**kw):
            r = dict(kw.get("record") or {})
            r.pop("sanctioned", None)          # BUG: sanctions no longer stop
            kw = dict(kw); kw["record"] = r
            return orig(**kw)
        rep = self._run_with_mutant(ignore_sanctions)
        self.assertGreater(len(rep["violations"]), 0)   # fuzzer must notice

    def test_catches_badge_overrides_stop(self):
        import blackwall as bw
        orig = bw.decide_payment

        def audit_override(**kw):
            r = dict(orig(**kw))
            if kw.get("verified_floor") is not None and r["verdict"] == "STOP":
                r["verdict"], r["hard_stop"] = "GO", False   # BUG: badge buys past STOP
            return r
        rep = self._run_with_mutant(audit_override)
        self.assertGreater(len(rep["violations"]), 0)

    def test_forecast_catches_ignored_sanctions(self):
        # the same regression must be caught through the forecast() path too.
        import blackwall as bw
        orig = bw.decide_payment

        def ignore_sanctions(**kw):
            r = dict(kw.get("record") or {}); r.pop("sanctioned", None)
            kw = dict(kw); kw["record"] = r
            return orig(**kw)
        bw.decide_payment = ignore_sanctions
        try:
            rep = F.run_forecast(iterations=3000, seed=1337)
        finally:
            bw.decide_payment = orig
        self.assertGreater(len(rep["violations"]), 0)


class TestForecastChecker(unittest.TestCase):
    """forecast_violations must catch a bad (resp, err) -- kills the vacuous-checker mutation."""

    def test_catches_both_set(self):
        probs = F.forecast_violations({"record": {}}, {}, {"verdict": "GO"}, "an error")
        self.assertTrue(any("F1" in p for p in probs))

    def test_catches_both_none(self):
        probs = F.forecast_violations({"record": {}}, {}, None, None)
        self.assertTrue(any("F1" in p for p in probs))

    def test_catches_go_on_sanctioned(self):
        probs = F.forecast_violations(
            {"counterparty": "0x" + "1" * 40}, {"sanctioned": True},
            {"verdict": "GO", "hard_stop": False, "score": 0.9,
             "signals": {}, "reasons": [], "receipt_id": "x"}, None)
        self.assertTrue(any("F3" in p for p in probs))

    def test_catches_missing_schema_key(self):
        probs = F.forecast_violations(
            {"record": {}}, {}, {"verdict": "GO", "hard_stop": False, "score": 0.5}, None)
        self.assertTrue(any("F4" in p and "signals" in p for p in probs))

    def test_clean_forecast_result_ok(self):
        probs = F.forecast_violations(
            {"counterparty": "0x" + "1" * 40}, {},
            {"verdict": "GO", "hard_stop": False, "score": 0.9,
             "signals": {}, "reasons": [], "receipt_id": "x"}, None)
        self.assertEqual(probs, [])


class TestChecker(unittest.TestCase):
    """The checker must CATCH violations -- these kill the 'checker returns []' mutation."""

    def test_catches_go_on_blockable(self):
        kw = {"record": {"sanctioned": True}, "counterparty": "0x" + "1" * 40}
        probs = F.invariant_violations(kw, {"verdict": "GO", "hard_stop": False, "score": 0.9})
        self.assertTrue(any("P2" in p or "P6" in p for p in probs))

    def test_catches_hard_stop_not_stop(self):
        probs = F.invariant_violations(
            {"record": {}}, {"verdict": "HOLD", "hard_stop": True, "score": 0.0})
        self.assertTrue(any("P3" in p for p in probs))

    def test_catches_score_out_of_range(self):
        probs = F.invariant_violations(
            {"record": {}}, {"verdict": "GO", "hard_stop": False, "score": 2.0})
        self.assertTrue(any("P5" in p for p in probs))

    def test_catches_bad_verdict_value(self):
        probs = F.invariant_violations(
            {"record": {}}, {"verdict": "MAYBE", "hard_stop": False, "score": 0.5})
        self.assertTrue(any("P1" in p for p in probs))

    def test_clean_result_no_violations(self):
        probs = F.invariant_violations(
            {"record": {}}, {"verdict": "GO", "hard_stop": False, "score": 0.9})
        self.assertEqual(probs, [])

    def test_blockable_detects_recipient_mismatch(self):
        kw = {"record": {}, "counterparty": "0x" + "1" * 40,
              "expected_recipient": "0x" + "2" * 40}
        self.assertTrue(F._blockable(kw))
        # same address (case-insensitive) is NOT a mismatch
        kw2 = {"record": {}, "counterparty": "0x" + "a" * 40,
               "expected_recipient": "0x" + "A" * 40}
        self.assertFalse(F._blockable(kw2))


if __name__ == "__main__":
    unittest.main()
