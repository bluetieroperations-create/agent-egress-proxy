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
