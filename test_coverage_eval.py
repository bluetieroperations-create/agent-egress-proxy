"""
test_coverage_eval.py -- guards the Stage-1 coverage instrument on a SYNTHETIC
ecosystem with a known answer (the live corpus changes, so it can't be the fixture).

The scenario is built so the mechanism is unambiguous: one known-good payee whose
sole reputable payer becomes reputable ONLY once two late-discovered anchors are
ingested. So the false-flag must be present at low coverage and gone at full coverage,
and convergence_verdict must read that correctly.

Mutation notes:
  - measure the ring rate over ALL payees instead of the known-good set ->
    test_curve_converges FAILS (real rings would pollute the rate).
  - drop the sub-full honesty fix (judge on f=1.0) -> test_verdict_ignores_definitional
    FAILS (f=1.0 is definitionally 0 and must not be the evidence).
  - non-monotone coverage (un-ingest as f grows) -> test_subsets_are_nested FAILS.
"""
import unittest

import coverage_eval as CE
import payer_reputation as PR


def _anchor_edges(anchor, n, tag):
    # give an anchor >= ANCHOR_MIN_DISTINCT distinct payers so it qualifies as a trust
    # anchor. Payers are unique per anchor EXCEPT the shared reputable payer added later.
    return [("0x%s_p%03d" % (tag, i), anchor) for i in range(n)]


class TestCoverageEval(unittest.TestCase):
    def _ecosystem(self):
        A1, A2 = "0xanchor1", "0xanchor2"
        n = PR.ANCHOR_MIN_DISTINCT + 2
        edges = _anchor_edges(A1, n, "a1") + _anchor_edges(A2, n, "a2")
        # REPUTABLE payer g1 pays both anchors -> reputation saturates >= 0.5.
        edges += [("0xg1", A1), ("0xg1", A2)]
        # known-good payee G: 3 distinct payers, one of them the reputable g1. At full
        # coverage G has a reputable payer -> NOT a ring. At low coverage (anchors not
        # yet ingested) g1 looks unreputable -> G spuriously flags.
        G = "0xgoodpayee"
        edges += [("0xg1", G), ("0xg2", G), ("0xg3", G)]
        # discovery order: G early (rank 0.1); anchors discovered mid-coverage (0.70)
        # so the false flag is present below 0.7 and cleared well before full coverage
        # -> a flat sub-full tail (the reachable case).
        rank = {CE.PG._norm(k): v for k, v in {
            A1: 0.70, A2: 0.71, G: 0.10}.items()}
        rank_fn = lambda payee: rank.get(CE.PG._norm(payee), 0.5)
        return edges, G, rank_fn

    def test_known_good_excludes_real_rings(self):
        # a genuine ring (3 payers, none pays any anchor) must NOT be in known_good.
        edges, G, _ = self._ecosystem()
        ring = "0xring"
        edges = edges + [("0xr1", ring), ("0xr2", ring), ("0xr3", ring)]
        good = CE.known_good_payees(edges)
        self.assertIn(CE.PG._norm(G), good)          # vouched payee is known-good
        self.assertNotIn(CE.PG._norm(ring), good)    # real ring is excluded

    def test_curve_converges(self):
        edges, G, rank_fn = self._ecosystem()
        curve = CE.coverage_curve(edges, fractions=(0.2, 0.5, 0.95, 1.0), rank_fn=rank_fn)
        by_f = {r["fraction"]: r for r in curve}
        # at 0.5 (anchors NOT ingested) the known-good G is ingested and false-flags.
        self.assertEqual(by_f[0.5]["false_flagged"], 1)
        self.assertGreater(by_f[0.5]["false_flag_rate"], 0.0)
        # by 0.95 (anchors ingested) the false flag is cleared.
        self.assertEqual(by_f[0.95]["false_flagged"], 0)

    def test_verdict_ignores_definitional_full(self):
        # even if f=1.0 is 0 (always true), the verdict must judge the SUB-FULL tail.
        # Here the sub-full tail is clean, so gating is reachable.
        edges, G, rank_fn = self._ecosystem()
        curve = CE.coverage_curve(edges, fractions=(0.2, 0.5, 0.9, 0.95, 1.0),
                                  rank_fn=rank_fn)
        v = CE.convergence_verdict(curve)
        self.assertLess(v["evidence_fraction"], 1.0)   # evidence is NOT f=1.0
        self.assertTrue(v["gating_reachable"])

    def test_verdict_flags_coverage_sensitive(self):
        # a curve still moving in the sub-full tail -> NOT reachable (stay advisory).
        curve = [
            {"fraction": 0.8, "false_flag_rate": 0.20, "ingested_known_good": 10,
             "false_flagged": 2},
            {"fraction": 0.9, "false_flag_rate": 0.10, "ingested_known_good": 10,
             "false_flagged": 1},
            {"fraction": 1.0, "false_flag_rate": 0.0, "ingested_known_good": 10,
             "false_flagged": 0},
        ]
        v = CE.convergence_verdict(curve)
        self.assertFalse(v["gating_reachable"])        # 10% at f=0.9 is too hot to gate

    def test_subsets_are_nested(self):
        # monotone coverage: every edge present at f is present at any f' > f.
        edges, G, rank_fn = self._ecosystem()
        s_lo = set(map(tuple, CE._subset_edges(edges, 0.5, rank_fn)))
        s_hi = set(map(tuple, CE._subset_edges(edges, 0.95, rank_fn)))
        self.assertTrue(s_lo.issubset(s_hi))


class TestSeedCorpusConvergence(unittest.TestCase):
    """The live Stage-1 -> Stage-3 gate, run on the SHIPPED corpus. This is the
    regression guard: if a future seed refresh REGRESSES coverage convergence (the
    sub-full false-flag tail starts moving again), this fails -- telling us the corpus
    is not complete enough to gate sybil_ring, before anyone flips the gate on.

    Skips cleanly if the seed artifact isn't present (e.g. a slim checkout)."""
    def test_shipped_corpus_is_gating_reachable(self):
        import os
        seed = os.path.join(os.path.dirname(__file__) or ".",
                            "data", "reputation_seed.db.gz")
        if not os.path.exists(seed):
            self.skipTest("reputation_seed.db.gz not present")
        edges = CE._load_seed_edges(seed)
        v = CE.convergence_verdict(CE.coverage_curve(edges))
        self.assertTrue(
            v["gating_reachable"],
            "shipped corpus regressed coverage convergence -- sybil_ring must stay "
            "ADVISORY until this is reachable again: %s" % v["reason"])


if __name__ == "__main__":
    unittest.main()
