"""
test_calibration_lock.py -- pins every decision threshold in blackwall.py.

Risk this addresses: the verdict engine folds ~14 signals behind a dozen numeric
thresholds. Miscalibration is the likeliest silent regression -- it already
happened twice (CATEGORY_HOLD_RATIO 10->50 after a 16% false-positive eval;
DIVERGENCE_HOLD_RATIO 5->10 for precision). A threshold nudged in a refactor
changes real verdicts with a green suite otherwise.

So each constant is locked to its EVAL-CALIBRATED value with the reason it holds.
Changing a threshold is a deliberate act: update the value here AND regenerate the
verdict_oracle golden in the same commit, so the calibration change and its
verdict impact are reviewed together. A bare edit to blackwall.py fails here.

Mutation note: nudge any locked constant in blackwall.py -> the matching assert
FAILS, naming the threshold and its calibrated value.
"""
import unittest
from decimal import Decimal

import blackwall as bw


class TestCalibrationLock(unittest.TestCase):
    # (attr, expected, why it is this value -- the calibration rationale)
    LOCKS = [
        ("GO_REPUTATION_MIN", 0.70,
         "below this Bayesian trust score, never an automatic GO"),
        ("THIN_HISTORY_SETTLEMENTS", 20,
         "fewer CONFIRMED settlements => thin => cannot GO (cold-start guard)"),
        ("MIN_DISTINCT_PAYERS", 3,
         "confirmed settlements must span >=3 payers -- the core Sybil gate"),
        ("MIN_CLASS_OBSERVATIONS", 3,
         "same-resource obs needed before trusting a per-class median"),
        ("MIN_PEER_COUNTERPARTIES", 3,
         "distinct counterparties needed to define a peer market rate"),
        ("PEER_HOLD_RATIO", 3.0,
         ">=3x the peer market for its class => HOLD"),
        ("CATEGORY_HOLD_RATIO", 50.0,
         "eval-calibrated: 10x flagged 16%% of legit premium APIs; 50x => 0%% FP"),
        ("DIVERGENCE_HOLD_RATIO", 10.0,
         "eval-recalibrated 5x->10x for precision on bait-and-switch"),
        ("HOLD_ANOMALY", 0.30,
         "price-anomaly score at/above this cannot GO"),
        ("STOP_ANOMALY_RATIO", 8.0,
         ">=8x its own median => STOP (wildly off, not just a HOLD)"),
        ("GOING_BAD_MIN_OUTCOMES", 4,
         "need >=4 recent outcomes before judging a 'going bad' trend"),
        ("GOING_BAD_DISPUTE_RATE", 0.30,
         "recent dispute rate at/above this => HOLD"),
    ]

    def test_thresholds_locked(self):
        for attr, expected, why in self.LOCKS:
            self.assertTrue(hasattr(bw, attr), "missing threshold %s" % attr)
            self.assertEqual(
                getattr(bw, attr), expected,
                "%s changed to %r (locked at %r: %s). If intentional, update "
                "test_calibration_lock.py AND regenerate the verdict_oracle golden."
                % (attr, getattr(bw, attr), expected, why))

    def test_hold_amount_threshold_locked(self):
        # the auto-GO budget ceiling: amounts above this escalate (HOLD). Decimal,
        # so it is locked separately (exact money, no float drift).
        self.assertEqual(bw.HOLD_AMOUNT_THRESHOLD, Decimal("10.00"),
                         "HOLD_AMOUNT_THRESHOLD (auto-GO budget ceiling) changed")

    def test_ordering_relationships_hold(self):
        # calibration isn't just point values -- the gates must stay ordered.
        # A STOP-anomaly must be a strictly harsher bar than a HOLD-anomaly, and
        # the category/divergence HOLD ratios must sit ABOVE the peer HOLD ratio
        # (they are coarser, cross-market signals). Kills a swap/typo that keeps
        # both values "valid" individually but inverts their relationship.
        self.assertGreater(bw.STOP_ANOMALY_RATIO, bw.PEER_HOLD_RATIO)
        self.assertGreater(bw.CATEGORY_HOLD_RATIO, bw.DIVERGENCE_HOLD_RATIO)
        self.assertGreater(bw.DIVERGENCE_HOLD_RATIO, bw.PEER_HOLD_RATIO)
        self.assertLess(bw.HOLD_ANOMALY, 1.0)
        self.assertGreaterEqual(bw.GO_REPUTATION_MIN, 0.5)


if __name__ == "__main__":
    unittest.main()
