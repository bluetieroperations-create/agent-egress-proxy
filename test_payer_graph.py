"""
Tests for payer_graph.py -- the cross-counterparty payer graph signal. Pure over
injected edges; each test states the mutation it kills.
"""
import unittest

import payer_graph as PG

# payees
A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40
# payers
P = lambda n: ("0x" + "%040x" % n)


class TestBuildIndex(unittest.TestCase):
    def test_bipartite_and_lowercased(self):
        g = PG.build_index([(P(1), A.upper()), (P(1), B), (P(2), A)])
        self.assertEqual(g["payee_to_payers"][A], {P(1), P(2)})   # A.upper joined to A
        self.assertEqual(g["payer_to_payees"][P(1)], {A, B})

    def test_repeated_edge_does_not_inflate_breadth(self):
        # Mutation: using a list not a set double-counts -> breadth wrong.
        g = PG.build_index([(P(1), A), (P(1), A), (P(1), A)])
        self.assertEqual(PG.payer_breadth(g, P(1)), 1)

    def test_blank_edges_dropped(self):
        g = PG.build_index([("", A), (P(1), None), (P(2), B)])
        self.assertEqual(set(g["payee_to_payers"]), {B})


class TestCrossSignal(unittest.TestCase):
    """
    Mutation notes:
      - counting all payers as established -> captive_ratio collapses.
      - not gating captive_sybil on established==0 -> a real payee gets flagged.
      - firing captive_sybil below the distinct gate -> duplicates the naive gate.
    """
    def test_established_vs_captive(self):
        # A paid by P1,P2,P3; P1 also pays B and C (breadth 3, established),
        # P2 also pays B (breadth 2, established), P3 pays only A (captive).
        edges = [(P(1), A), (P(1), B), (P(1), C),
                 (P(2), A), (P(2), B),
                 (P(3), A)]
        s = PG.cross_signal(PG.build_index(edges), A)
        self.assertEqual(s["distinct_payers"], 3)
        self.assertEqual(s["established_payers"], 2)
        self.assertEqual(s["captive_payers"], 1)
        self.assertAlmostEqual(s["captive_ratio"], 0.333, places=2)
        self.assertFalse(s["captive_sybil"])          # has established payers

    def test_captive_sybil_flag(self):
        # A cleared the distinct>=3 gate with 4 payers who each pay ONLY A.
        edges = [(P(i), A) for i in range(1, 5)]
        s = PG.cross_signal(PG.build_index(edges), A)
        self.assertEqual(s["distinct_payers"], 4)
        self.assertEqual(s["established_payers"], 0)
        self.assertEqual(s["captive_ratio"], 1.0)
        self.assertTrue(s["captive_sybil"])           # all captive -> flagged

    def test_not_flagged_below_distinct_gate(self):
        # 2 captive payers: already caught by the naive Sybil gate; this signal
        # must NOT also fire (it targets what the naive gate MISSES).
        edges = [(P(1), A), (P(2), A)]
        self.assertFalse(PG.cross_signal(PG.build_index(edges), A)["captive_sybil"])

    def test_not_flagged_above_ceiling(self):
        # a large all-captive set is more likely an ingestion artifact than an
        # affordable farm; bounded by CAPTIVE_SYBIL_MAX_DISTINCT.
        n = PG.CAPTIVE_SYBIL_MAX_DISTINCT + 1
        edges = [(P(i), A) for i in range(1, n + 1)]
        self.assertFalse(PG.cross_signal(PG.build_index(edges), A)["captive_sybil"])

    def test_absent_payee_returns_none(self):
        self.assertIsNone(PG.cross_signal(PG.build_index([(P(1), A)]), B))


class TestSource(unittest.TestCase):
    def test_from_store_and_cache(self):
        class _Store:
            def iter_settlement_edges(self):
                return iter([(P(1), A), (P(1), B), (P(2), A), (P(3), A)])
        gs = PG.PayerGraphSource.from_store(_Store())
        s = gs.cross_signal(A)
        self.assertEqual(s["established_payers"], 1)   # P1 pays A and B
        self.assertIs(gs.cross_signal(A), s)           # cached, same object


if __name__ == "__main__":
    unittest.main()
