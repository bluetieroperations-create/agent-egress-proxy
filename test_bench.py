"""
test_bench.py -- guards the benchmark HARNESS, never absolute latency.

The cardinal rule (and the way latency tests usually burn you): DO NOT assert
microsecond thresholds -- they are environment- and load-dependent and will flap in
CI. This guards only that the harness is CORRECT: percentiles are ordered, every case
runs, and the environment is stamped. The numbers themselves are for humans to read
from `python bench.py`, not for a test to police.
"""
import unittest

import bench


class TestBenchHarness(unittest.TestCase):
    def test_measure_returns_ordered_percentiles(self):
        # p50 <= p95 <= p99 must hold by construction (sorted samples). Mutation: pick
        # percentiles from an unsorted list -> ordering can break and this FAILS.
        s = bench.measure(lambda: sum(range(50)), n=500, warmup=20)
        self.assertLessEqual(s["p50"], s["p95"])
        self.assertLessEqual(s["p95"], s["p99"])
        self.assertGreaterEqual(s["min"], 0.0)
        self.assertEqual(s["unit"], "us")

    def test_all_cases_run(self):
        # every declared case executes without raising and yields a summary. Run at a
        # tiny scale so the suite stays fast; we assert STRUCTURE, not timing.
        rep = bench.run(scale=0.01)
        for key, label, fn, n in bench.CASES:
            self.assertIn(key, rep["results"])
            self.assertIn("p50", rep["results"][key])
        # the derived signed-verify estimate is present and composed from the primitives
        self.assertIn("signed_verify_est", rep["results"])
        self.assertGreater(rep["results"]["signed_verify_est"]["p50"], 0.0)

    def test_environment_is_stamped(self):
        # any quoted number is meaningless without the environment -> it must be captured.
        env = bench.environment()
        for k in ("python", "machine", "system"):
            self.assertTrue(env.get(k))

    def test_no_absolute_latency_assertions_here(self):
        # a self-documenting guard: this test file intentionally makes NO microsecond
        # assertion. If a future edit adds one, delete it -- absolute latency in a test
        # is a flake. (Nothing to assert; the presence of this note is the point.)
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
