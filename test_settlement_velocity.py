"""
Tests for settlement_velocity.py -- the temporal reputation axis. Pure over injected
(ts, payer) events + an injected `now`. Each test states the mutation it kills.
"""
import unittest
from datetime import datetime, timezone

import settlement_velocity as SV


def dt(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


NOW = dt(2026, 8, 2)


class TestTemporalSignal(unittest.TestCase):
    """
    Mutation notes:
      - measuring settlement times not payer FIRST-seen -> burst share wrong.
      - not bucketing acquisition by day -> peak_day_share meaningless.
      - dropping the stale gate -> a dormant endpoint stays GO-eligible.
    """
    def test_organic_spread_not_burst(self):
        # 6 distinct payers, each first seen on a different day -> low peak share.
        ev = [("2026-07-%02dT12:00:00Z" % (10 + i), "0x%040x" % i) for i in range(6)]
        s = SV.temporal_signal(ev, now=NOW)
        self.assertEqual(s["distinct_payers"], 6)
        self.assertEqual(s["peak_day_new_payers"], 1)
        self.assertLess(s["peak_day_share"], 0.3)
        self.assertFalse(s["burst_sybil"])

    def test_burst_flagged(self):
        # 6 distinct payers all first seen the SAME day -> peak share 1.0 -> burst.
        ev = [("2026-07-10T%02d:00:00Z" % (1 + i), "0x%040x" % i) for i in range(6)]
        s = SV.temporal_signal(ev, now=NOW)
        self.assertEqual(s["peak_day_share"], 1.0)
        self.assertTrue(s["burst_sybil"])

    def test_burst_needs_min_distinct(self):
        # 4 same-day payers: below BURST_MIN_DISTINCT -> not a "burst" (just thin).
        ev = [("2026-07-10T0%d:00:00Z" % i, "0x%040x" % i) for i in range(1, 5)]
        self.assertFalse(SV.temporal_signal(ev, now=NOW)["burst_sybil"])

    def test_repeat_payer_counted_once_at_first_seen(self):
        # a payer paying twice (two days) is acquired ONCE, on the earlier day.
        ev = [("2026-07-10T00:00:00Z", "0xaa"), ("2026-07-20T00:00:00Z", "0xaa"),
              ("2026-07-10T00:00:00Z", "0xbb")]
        s = SV.temporal_signal(ev, now=NOW)
        self.assertEqual(s["distinct_payers"], 2)
        self.assertEqual(s["peak_day_new_payers"], 2)      # both acquired on the 10th

    def test_stale_flag_and_recency(self):
        ev = [("2026-01-01T00:00:00Z", "0xaa"), ("2026-02-01T00:00:00Z", "0xbb")]
        s = SV.temporal_signal(ev, now=NOW)               # last=Feb 1, now=Aug 2
        self.assertGreater(s["recency_days"], SV.STALE_DAYS)
        self.assertTrue(s["stale"])

    def test_fresh_not_stale(self):
        ev = [("2026-07-30T00:00:00Z", "0xaa"), ("2026-08-01T00:00:00Z", "0xbb")]
        self.assertFalse(SV.temporal_signal(ev, now=NOW)["stale"])

    def test_no_now_means_no_recency_no_stale(self):
        s = SV.temporal_signal([("2020-01-01T00:00:00Z", "0xaa")])
        self.assertIsNone(s["recency_days"])
        self.assertFalse(s["stale"])

    def test_empty_and_unparseable_returns_none(self):
        self.assertIsNone(SV.temporal_signal([]))
        self.assertIsNone(SV.temporal_signal([("not-a-date", "0xaa")]))


class TestVelocitySource(unittest.TestCase):
    def test_reads_store_and_caches(self):
        class _Store:
            calls = 0
            def iter_settlement_events(self, cp):
                _Store.calls += 1
                return [("2026-07-10T00:00:00Z", "0xaa"), ("2026-07-11T00:00:00Z", "0xbb")]
        vs = SV.VelocitySource(_Store(), now=NOW)
        a = vs.temporal("0xCP")
        b = vs.temporal("0xCP")
        self.assertIs(a, b)                    # cached
        self.assertEqual(_Store.calls, 1)

    def test_store_failure_is_fail_open(self):
        class _Boom:
            def iter_settlement_events(self, cp):
                raise RuntimeError("db down")
        self.assertIsNone(SV.VelocitySource(_Boom(), now=NOW).temporal("0xCP"))


if __name__ == "__main__":
    unittest.main()
