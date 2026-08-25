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

    def test_self_edge_dropped(self):
        # audit F3: a payee paying its OWN address must not count itself as a payer
        # (self-vouch) -- it would inflate distinct/established and disable captive.
        g = PG.build_index([(A, A), (P(1), A), (P(2), A)])
        self.assertEqual(g["payee_to_payers"][A], {P(1), P(2)})   # A not among them
        self.assertNotIn(A, g["payer_to_payees"])                 # A isn't a payer of itself

    def test_self_dealing_farm_stays_captive_flagged(self):
        # a fully-captive farm whose payee also pays ITSELF must still flag captive
        # (the self-edge must not create a fake "established" payer).
        edges = [(A, A)] + [(P(i), A) for i in range(1, 5)]       # 4 captive + 1 self
        s = PG.cross_signal(PG.build_index(edges), A)
        self.assertEqual(s["established_payers"], 0)
        self.assertTrue(s["captive_sybil"])


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



class ZeroAddressIsNotAPayer(unittest.TestCase):
    """A transfer from 0x0 is a MINT, not a payment."""

    ZERO = "0x" + "0" * 40
    PAYEE = "0x" + "ab" * 20
    REAL = "0x" + "cd" * 20

    def test_mint_does_not_count_as_a_distinct_payer(self):
        # Mutation: dropping the ZERO_ADDRESS guard. The payee would be credited
        # with 2 distinct payers when only 1 wallet ever chose to pay it --
        # unearned breadth, and the cheapest possible assist past a Sybil gate.
        g = PG.build_index([(self.REAL, self.PAYEE), (self.ZERO, self.PAYEE)])
        self.assertEqual(g["payee_to_payers"][self.PAYEE], {self.REAL})

    def test_zero_address_is_matched_case_insensitively(self):
        # Mutation: comparing before _norm. Chain data is not consistently cased.
        g = PG.build_index([("0X" + "0" * 40, self.PAYEE)])
        self.assertEqual(g["payee_to_payers"].get(self.PAYEE), None)

    def test_real_payers_are_untouched(self):
        # Mutation: an over-broad guard that drops any leading-zero address.
        low = "0x" + "0" * 39 + "1"
        g = PG.build_index([(low, self.PAYEE)])
        self.assertEqual(g["payee_to_payers"][self.PAYEE], {low})

if __name__ == "__main__":
    unittest.main()
