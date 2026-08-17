"""
test_refresh_guard.py -- guards the automated-refresh release gate (Stage 2).

The point: a bad refresh must NEVER ship. Each test states the mutation it kills.
"""
import unittest

import refresh_guard as G


def _stats(payees, edges, age_days, gating_reachable=True):
    # a minimal stats dict shaped like store_stats() output.
    return {"payees": payees, "edges": edges, "age_days": age_days,
            "gating_reachable": gating_reachable, "payers": payees * 5,
            "anchors": max(0, payees // 8), "subfull_false_flag_rate": 0.0}


class TestAssessRefresh(unittest.TestCase):
    # a healthy refresh: similar size, clearly fresher.
    OLD = _stats(290, 23000, age_days=45)

    def test_accepts_healthy_refresh(self):
        new = _stats(300, 24000, age_days=2)
        r = G.assess_refresh(self.OLD, new)
        self.assertTrue(r["accept"], r["reasons"])
        self.assertFalse(r["reasons"])

    def test_rejects_payee_collapse(self):
        # partial crawl: 290 -> 5 payees. Mutation: drop the retention check -> a sparse
        # store ships and sends the whole corpus cold-start (HOLD).
        new = _stats(5, 400, age_days=1)
        r = G.assess_refresh(self.OLD, new)
        self.assertFalse(r["accept"])
        self.assertTrue(any("collapsed" in x for x in r["reasons"]))

    def test_rejects_edge_collapse_even_if_payees_ok(self):
        # payees retained but edges gutted (crawl truncated each payee's history).
        new = _stats(290, 1000, age_days=1)
        r = G.assess_refresh(self.OLD, new)
        self.assertFalse(r["accept"])
        self.assertTrue(any("edge count" in x for x in r["reasons"]))

    def test_rejects_no_progress(self):
        # crawl produced a store no fresher than the current one -> pointless churn.
        # Mutation: use `>` instead of `>=` -> an equal-age no-op would ship.
        new = _stats(300, 24000, age_days=45)
        r = G.assess_refresh(self.OLD, new)
        self.assertFalse(r["accept"])
        self.assertTrue(any("no progress" in x for x in r["reasons"]))

    def test_rejects_stale_result(self):
        # even if "fresher" than a very old store, a result past the warn window is not
        # fresh enough to ship. old very stale, new still stale.
        old = _stats(290, 23000, age_days=200)
        new = _stats(290, 23000, age_days=80)   # fresher than 200 but > 60-day warn
        r = G.assess_refresh(old, new)
        self.assertFalse(r["accept"])
        self.assertTrue(any("not fresh" in x for x in r["reasons"]))

    def test_convergence_regression_warns_but_accepts(self):
        # a fresher, healthy-size store whose coverage convergence regressed: ACCEPT
        # (freshness wins) but WARN. Mutation: turn this into a reject -> we'd keep a
        # stale store just because convergence dipped, which is worse.
        new = _stats(300, 24000, age_days=2, gating_reachable=False)
        r = G.assess_refresh(self.OLD, new)
        self.assertTrue(r["accept"])
        self.assertTrue(any("REGRESSED" in x for x in r["warnings"]))

    def test_empty_old_store_does_not_divide_by_zero(self):
        old = _stats(0, 0, age_days=None)
        new = _stats(10, 100, age_days=1)
        r = G.assess_refresh(old, new)
        self.assertTrue(r["accept"])


class TestStoreStats(unittest.TestCase):
    def test_stats_shape_and_convergence(self):
        # a tiny synthetic graph: two anchors + a vouched payee. store_stats should
        # report structure and a convergence verdict without raising. Mutation: drop
        # the empty-store guard -> convergence_verdict on 0 payees raises.
        edges = [("0xp%d" % i, "0xanchor1") for i in range(22)]
        edges += [("0xp%d" % i, "0xanchor2") for i in range(22)]
        s = G.store_stats(edges, age_days=3)
        self.assertEqual(s["age_days"], 3)
        self.assertGreaterEqual(s["payees"], 2)
        self.assertIn("gating_reachable", s)

    def test_empty_store_stats_safe(self):
        s = G.store_stats([], age_days=None)
        self.assertEqual(s["payees"], 0)
        self.assertFalse(s["gating_reachable"])


class TestGatingCapableUtility(unittest.TestCase):
    """The UTILITY metric. REGRESSION: the 2026-08-17 refresh kept 85% of payees and 87%
    of edges -- clearing MIN_RETENTION -- while payees able to EARN a GO fell 237 -> 207.
    Size retention and the boolean `gating_reachable` were both blind to it."""

    def _edges(self, spec):
        """spec: {payee: (n_settlements, n_distinct_payers)} -> edge list."""
        out = []
        for payee, (n, dp) in spec.items():
            for i in range(n):
                out.append(("0xpayer%d" % (i % dp), payee))
        return out

    def test_counts_only_payees_clearing_BOTH_gates(self):
        e = self._edges({
            "0xgood": (25, 4),     # clears both -> counts
            "0xthin": (5, 4),      # too few settlements -> excluded
            "0xsybil": (25, 2),    # too few distinct payers -> excluded
        })
        self.assertEqual(G.gating_capable(e), 1)

    def test_boundaries_are_inclusive(self):
        self.assertEqual(G.gating_capable(self._edges({"0xa": (20, 3)})), 1)
        self.assertEqual(G.gating_capable(self._edges({"0xa": (19, 3)})), 0)
        self.assertEqual(G.gating_capable(self._edges({"0xa": (20, 2)})), 0)

    def test_normalization_matches_build_index(self):
        # self-vouch edges are dropped by build_index; the count must agree or the two
        # halves of the metric would disagree about who exists.
        e = [("0xa", "0xa")] * 30 + [("0xp%d" % (i % 3), "0xb") for i in range(30)]
        self.assertEqual(G.gating_capable(e), 1)      # only 0xb

    def test_never_raises_on_junk_edges(self):
        for bad in (None, [], [None], [("a",)], [(None, None)], ["notatuple"], [(1, 2)]):
            G.gating_capable(bad)

    def test_reject_on_utility_collapse_that_size_check_MISSES(self):
        # THE REGRESSION, with the real shape: size retention passes, utility does not.
        old = {"payees": 281, "edges": 23037, "gating_capable": 237, "age_days": 13,
               "gating_reachable": True}
        new = {"payees": 240, "edges": 20141, "gating_capable": 207, "age_days": 0,
               "gating_reachable": True}
        # size-only view would have been happy: 85% and 87%, both over MIN_RETENTION
        self.assertGreater(new["payees"], old["payees"] * G.MIN_RETENTION)
        self.assertGreater(new["edges"], old["edges"] * G.MIN_RETENTION)
        r = G.assess_refresh(old, new)
        self.assertFalse(r["accept"])
        self.assertTrue(any("gating-capable" in x for x in r["reasons"]), r["reasons"])

    def test_accept_when_utility_is_retained(self):
        old = {"payees": 281, "edges": 23037, "gating_capable": 237, "age_days": 13,
               "gating_reachable": True}
        # what MERGE semantics actually produced on the real corpora: utility GREW.
        new = {"payees": 281, "edges": 32971, "gating_capable": 251, "age_days": 0,
               "gating_reachable": True}
        self.assertTrue(G.assess_refresh(old, new)["accept"])

    def test_absent_metric_does_not_block(self):
        # fail-open: a stats dict without the new key must not start rejecting refreshes.
        old = {"payees": 100, "edges": 1000, "age_days": 13, "gating_reachable": True}
        new = {"payees": 100, "edges": 1000, "age_days": 0, "gating_reachable": True}
        self.assertTrue(G.assess_refresh(old, new)["accept"])


if __name__ == "__main__":
    unittest.main()