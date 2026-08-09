"""
Tests for price_integrity.py -- advertised-vs-settled divergence. Each test states the
mutation it kills.
"""
import unittest

import price_integrity as PI

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
A = "0x" + "a" * 40
B = "0x" + "b" * 40


class _Store:
    def __init__(self, recs):
        self.recs = recs

    def lookup(self, cp):
        return self.recs.get(cp.lower(), {})


def _r(payee, price_atomic, asset=USDC):
    return {"payTo": payee, "price_atomic": price_atomic, "asset": asset}


class TestDivergenceRatio(unittest.TestCase):
    """
    Mutation notes:
      - divide by min instead of MAX advertised -> test_uses_max FAILS (would flag a
        payee with a legit expensive listed endpoint people pay).
      - not None on missing side -> test_missing FAILS.
    """
    def test_uses_max_advertised(self):
        # settles 0.09; advertised {0.001, 0.10} -> vs MAX 0.10 = 0.9 (NOT flagged);
        # vs min 0.001 would be 90 (false alarm). Kills the min-vs-max mutation.
        self.assertAlmostEqual(PI.divergence_ratio(["0.09"], ["0.001", "0.10"]), 0.9, places=2)

    def test_bait_and_switch(self):
        # advertise only 0.001, settle median 0.09 -> 90x
        self.assertAlmostEqual(PI.divergence_ratio(["0.09"] * 5, ["0.001"]), 90.0, places=1)

    def test_settled_below_advertised(self):
        self.assertAlmostEqual(PI.divergence_ratio(["0.001"], ["0.01"]), 0.1, places=2)

    def test_missing_side_is_none(self):
        self.assertIsNone(PI.divergence_ratio([], ["0.01"]))
        self.assertIsNone(PI.divergence_ratio(["0.01"], []))

    def test_non_positive_ignored(self):
        self.assertIsNone(PI.divergence_ratio(["0", "-1"], ["0.01"]))


class TestBuildIndex(unittest.TestCase):
    """
    Mutation notes:
      - index payees below the watch ratio -> test_only_watchlist FAILS (bloat + noise).
      - count a non-USDC advertised price -> test_usdc_only FAILS (unknown decimals).
    """
    def test_only_watchlist_indexed(self):
        store = _Store({A: {"price_history": ["0.05"] * 3},   # settles 0.05
                        B: {"price_history": ["0.001"] * 3}})  # settles 0.001
        res = [_r(A, 1000), _r(B, 1000)]   # both advertise 0.001 (atomic 1000)
        idx = PI.build_divergence_index(res, store, watch_ratio=2.0)
        self.assertIn(A, idx)              # 0.05 / 0.001 = 50x -> on the list
        self.assertNotIn(B, idx)           # 0.001 / 0.001 = 1x -> off the list
        self.assertAlmostEqual(float(idx[A]), 50.0, places=1)

    def test_usdc_only(self):
        # a non-USDC advertised asset -> unknown decimals -> ignored -> no index entry
        store = _Store({A: {"price_history": ["0.05"] * 3}})
        res = [_r(A, 1000, asset="0xNOTUSDC")]
        self.assertEqual(PI.build_divergence_index(res, store), {})


if __name__ == "__main__":
    unittest.main()
