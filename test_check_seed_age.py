"""
Tests for check_seed_age.py -- the prebuilt-store freshness check. Each test states
the mutation it kills.
"""
import datetime
import unittest

import check_seed_age as C

UTC = datetime.timezone.utc


class TestSeedAge(unittest.TestCase):
    """
    Mutation notes:
      - subtract wrong direction -> test_age FAILS (negative days).
      - not clamping future ts -> test_future_clamped FAILS.
    """
    def test_age(self):
        now = datetime.datetime(2026, 8, 4, tzinfo=UTC)
        self.assertEqual(C.seed_age_days("2026-08-04T00:00:00Z", now), 0)
        self.assertEqual(C.seed_age_days("2026-07-05T00:00:00Z", now), 30)

    def test_z_and_naive_handled(self):
        now = datetime.datetime(2026, 8, 4, tzinfo=UTC)
        self.assertEqual(C.seed_age_days("2026-08-04T00:00:00", now), 0)  # naive -> UTC

    def test_future_clamped(self):
        now = datetime.datetime(2026, 8, 4, tzinfo=UTC)
        self.assertEqual(C.seed_age_days("2026-09-01T00:00:00Z", now), 0)


class TestAssess(unittest.TestCase):
    """
    Mutation notes:
      - return ok=True past the cliff -> test_stale FAILS (CI wouldn't catch a dead store).
      - drop the warn tier -> test_aging FAILS (no lead time before the cliff).
    """
    def test_fresh(self):
        a = C.assess(10, warn=60, stale_days=90)
        self.assertTrue(a["ok"])
        self.assertEqual(a["level"], "fresh")
        self.assertEqual(a["days_to_cliff"], 80)

    def test_aging_warns_before_cliff(self):
        a = C.assess(60, warn=60, stale_days=90)
        self.assertFalse(a["ok"])            # CI should fail in the warn window
        self.assertEqual(a["level"], "aging")

    def test_stale_past_cliff(self):
        a = C.assess(95, warn=60, stale_days=90)
        self.assertFalse(a["ok"])
        self.assertEqual(a["level"], "stale")

    def test_boundary_just_fresh(self):
        self.assertTrue(C.assess(59, warn=60, stale_days=90)["ok"])


if __name__ == "__main__":
    unittest.main()
