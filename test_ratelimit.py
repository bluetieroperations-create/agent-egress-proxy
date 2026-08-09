"""
Tests for ratelimit.py -- token-bucket limiter + client-IP extraction. Each test states
the mutation it kills. The clock is injected (`now`), so no sleeping.
"""
import unittest

import ratelimit as R


class TestClientIP(unittest.TestCase):
    """
    Mutation notes:
      - take the LEFTMOST XFF -> test_rightmost FAILS (client-spoofable).
      - ignore XFF -> test_rightmost FAILS.
    """
    def test_rightmost_xff(self):
        # a client injects a fake IP; the proxy appends the real one on the right
        self.assertEqual(R.client_ip_from("1.2.3.4, 9.9.9.9", "10.0.0.1"), "9.9.9.9")

    def test_single_xff(self):
        self.assertEqual(R.client_ip_from("9.9.9.9", "10.0.0.1"), "9.9.9.9")

    def test_no_xff_uses_peer(self):
        self.assertEqual(R.client_ip_from("", "10.0.0.1"), "10.0.0.1")
        self.assertEqual(R.client_ip_from(None, "10.0.0.1"), "10.0.0.1")

    def test_blank_falls_back(self):
        self.assertEqual(R.client_ip_from("  ,  ", "10.0.0.1"), "10.0.0.1")
        self.assertEqual(R.client_ip_from(None, None), "unknown")


class TestRateLimiter(unittest.TestCase):
    """
    Mutation notes:
      - never deny (always True) -> test_burst_then_denied FAILS.
      - don't refill over time -> test_refill FAILS.
      - share one bucket across keys -> test_per_key FAILS.
    """
    def test_burst_then_denied(self):
        rl = R.RateLimiter(rate=60, per_seconds=60, burst=3)   # 1/sec, burst 3
        for _ in range(3):
            self.assertTrue(rl.allow("a", now=0.0)[0])
        allowed, retry = rl.allow("a", now=0.0)
        self.assertFalse(allowed)
        self.assertGreater(retry, 0.0)                          # tells the caller when

    def test_refill_over_time(self):
        rl = R.RateLimiter(rate=60, per_seconds=60, burst=1)   # 1 token/sec
        self.assertTrue(rl.allow("a", now=0.0)[0])
        self.assertFalse(rl.allow("a", now=0.0)[0])            # empty
        self.assertTrue(rl.allow("a", now=1.0)[0])             # +1s -> 1 token back

    def test_per_key_independent(self):
        rl = R.RateLimiter(rate=60, per_seconds=60, burst=1)
        self.assertTrue(rl.allow("a", now=0.0)[0])
        self.assertTrue(rl.allow("b", now=0.0)[0])            # b has its own bucket
        self.assertFalse(rl.allow("a", now=0.0)[0])

    def test_capacity_cap(self):
        # tokens never exceed burst even after a long idle
        rl = R.RateLimiter(rate=60, per_seconds=60, burst=2)
        for _ in range(2):
            self.assertTrue(rl.allow("a", now=1000.0)[0])
        self.assertFalse(rl.allow("a", now=1000.0)[0])         # only 2, not more

    def test_memory_bounded(self):
        rl = R.RateLimiter(rate=60, per_seconds=60, max_keys=5)
        for i in range(50):
            rl.allow("k%d" % i, now=float(i))
        self.assertLessEqual(len(rl), 5)                       # LRU-evicted

    def test_bad_config(self):
        with self.assertRaises(ValueError):
            R.RateLimiter(rate=0)
        with self.assertRaises(ValueError):
            R.RateLimiter(rate=1, per_seconds=0)


if __name__ == "__main__":
    unittest.main()
