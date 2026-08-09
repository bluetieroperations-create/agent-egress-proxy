"""
Tests for confidence.py -- how much evidence backs a verdict. Pure; each test states
the mutation it kills.
"""
import unittest

import confidence as C


class TestAssessConfidence(unittest.TestCase):
    """
    Mutation notes:
      - cold-start scored as high -> a no-history GO looks trustworthy.
      - outcome data not counted -> the depth dimension never raises confidence.
      - stale still credited as fresh -> a dormant payee reads as fresh.
    """

    def test_cold_start_is_low_and_lists_gaps(self):
        c = C.assess_confidence({"settlement_count": 0}, {})
        self.assertEqual(c["level"], "low")
        self.assertEqual(c["score"], 0.0)
        self.assertIn("no on-chain history (cold start)", c["missing"])
        self.assertEqual(c["backed_by"], [])

    def test_established_breadth_is_high_but_flags_no_outcomes(self):
        record = {"confirmed_settlement_count": 143, "distinct_payers": 49}
        signals = {"cross_counterparty": {"reputable_payers": 28, "established_payers": 32},
                   "temporal": {"recency_days": 2.0, "stale": False}}
        c = C.assess_confidence(record, signals)
        self.assertEqual(c["level"], "high")               # 0.30+0.20+0.20+0.10 = 0.80
        self.assertTrue(any("143 confirmed settlements" in b for b in c["backed_by"]))
        self.assertTrue(any("reputable payers" in b for b in c["backed_by"]))
        # breadth-only: the outcome dimension is the honest gap.
        self.assertIn("no outcome/dispute data (breadth only)", c["missing"])

    def test_outcome_data_raises_confidence(self):
        base = {"confirmed_settlement_count": 143, "distinct_payers": 49}
        no_out = C.assess_confidence(base, {})["score"]
        with_out = C.assess_confidence(dict(base, recent_outcomes=10), {})["score"]
        self.assertAlmostEqual(with_out - no_out, 0.20, places=3)

    def test_thin_history_is_medium_or_low_not_high(self):
        c = C.assess_confidence({"confirmed_settlement_count": 5, "distinct_payers": 2}, {})
        self.assertNotEqual(c["level"], "high")
        self.assertTrue(any("thin history" in m for m in c["missing"]))

    def test_stale_not_credited_as_fresh(self):
        record = {"confirmed_settlement_count": 143, "distinct_payers": 49}
        c = C.assess_confidence(record, {"temporal": {"stale": True, "recency_days": 200}})
        self.assertIn("stale (no recent settlement)", c["missing"])
        self.assertFalse(any("fresh" in b for b in c["backed_by"]))

    def test_established_only_scores_less_than_reputable(self):
        rec = {"confirmed_settlement_count": 143, "distinct_payers": 49}
        est = C.assess_confidence(rec, {"cross_counterparty": {"established_payers": 5}})
        rep = C.assess_confidence(rec, {"cross_counterparty": {"reputable_payers": 5}})
        self.assertLess(est["score"], rep["score"])


class TestFoldedIntoVerdict(unittest.TestCase):
    def test_decide_payment_carries_confidence(self):
        import blackwall as bw
        v = bw.decide_payment("0.09", {"settlement_count": 1240, "dispute_rate": 0.002},
                              ["0.09", "0.09"], counterparty="0xA")
        self.assertIn("confidence", v)
        self.assertIn(v["confidence"]["level"], ("low", "medium", "high"))

    def test_confidence_is_descriptive_not_a_gate(self):
        # a low-confidence GO is still GO -- confidence never changes the verdict.
        import blackwall as bw
        v = bw.decide_payment("0.09", {"settlement_count": 1240, "dispute_rate": 0.002},
                              ["0.09", "0.09"], counterparty="0xA")
        self.assertEqual(v["verdict"], "GO")
        # no graph/temporal/outcome-breadth -> not high confidence, yet still GO.
        self.assertNotEqual(v["confidence"]["level"], "high")


if __name__ == "__main__":
    unittest.main()
