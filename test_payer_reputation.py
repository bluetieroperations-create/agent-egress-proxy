"""
Tests for payer_reputation.py -- trust propagated from anchors to payers to payees.
Pure over injected edges; each test states the mutation it kills.
"""
import unittest

import payer_reputation as PR
import payer_graph as PG

PR.ANCHOR_MIN_DISTINCT      # referenced so a rename is caught by import


def payer(n):
    return "0x" + "%040x" % n


def anchor(tag):
    # a payee paid by ANCHOR_MIN_DISTINCT distinct payers -> qualifies as an anchor.
    payee = "0x" + ("%02x" % tag) + "e" * 38
    edges = [(payer(1000 * tag + i), payee) for i in range(PR.ANCHOR_MIN_DISTINCT)]
    return payee, edges


class TestCeilingAlignment(unittest.TestCase):
    def test_ring_and_captive_share_one_ceiling(self):
        # audit F5: a 9-12 captive/ring farm must not slip through a mismatch.
        self.assertEqual(PR.RING_MAX_DISTINCT, PG.CAPTIVE_SYBIL_MAX_DISTINCT)


class TestAnchorsAndPayerRep(unittest.TestCase):
    def test_anchor_detection(self):
        a, edges = anchor(1)
        g = PG.build_index(edges)
        self.assertIn(a, PR.anchor_payees(g))

    def test_payer_reputation_saturates_on_anchors(self):
        # a payer that pays >= saturation distinct anchors -> reputation 1.0.
        a1, e1 = anchor(1); a2, e2 = anchor(2); a3, e3 = anchor(3)
        agent = payer(999999)
        edges = e1 + e2 + e3 + [(agent, a1), (agent, a2), (agent, a3)]
        g = PG.build_index(edges)
        anchors = PR.anchor_payees(g)
        r, k = PR.payer_reputation(g, anchors, agent, saturation=3)
        self.assertEqual(k, 3)
        self.assertEqual(r, 1.0)

    def test_payer_paying_no_anchor_scores_zero(self):
        # Mutation: crediting non-anchor payees would give a ring member reputation.
        small = "0x" + "5" * 40
        edges = [(payer(1), small), (payer(2), small)]      # small is NOT an anchor
        g = PG.build_index(edges)
        r, k = PR.payer_reputation(g, PR.anchor_payees(g), payer(1))
        self.assertEqual((r, k), (0.0, 0))


class TestPayeeCorroboration(unittest.TestCase):
    """
    Mutation notes:
      - counting all payers as reputable -> sybil_ring never fires.
      - firing sybil_ring when a reputable payer exists -> a real payee is flagged.
    """
    def _ring_and_real(self):
        # Two anchors + reputable agents that pay them.
        a1, e1 = anchor(1); a2, e2 = anchor(2)
        agentA, agentB = payer(700001), payer(700002)
        real = "0x" + "9" * 40
        # `real` is paid by two anchor-paying (reputable) agents -> corroborated.
        real_edges = [(agentA, a1), (agentA, a2), (agentB, a1), (agentB, a2),
                      (agentA, real), (agentB, real), (payer(3), real)]
        # `ring`: 4 payers, each with breadth>=2 within the ring, none pay an anchor.
        r1, r2 = "0x" + "a" * 40, "0x" + "b" * 40
        ring = "0x" + "c" * 40
        rp = [payer(800000 + i) for i in range(4)]
        ring_edges = []
        for p in rp:
            ring_edges += [(p, ring), (p, r1)]   # breadth 2, but r1/ring aren't anchors
        return PG.build_index(e1 + e2 + real_edges + ring_edges), real, ring

    def test_real_payee_corroborated(self):
        g, real, ring = self._ring_and_real()
        _, anchors = PR.payer_scores(g)
        rep, _ = PR.payer_scores(g)
        sig = PR.payee_corroboration(g, rep, real)
        self.assertGreaterEqual(sig["reputable_payers"], 2)
        self.assertFalse(sig["sybil_ring"])

    def test_ring_flagged_even_though_not_captive(self):
        g, real, ring = self._ring_and_real()
        rep, _ = PR.payer_scores(g)
        # the graph-only captive signal is fooled: ring payers have breadth 2.
        graph_sig = PG.cross_signal(g, ring)
        self.assertGreater(graph_sig["established_payers"], 0)   # looks "established"
        self.assertFalse(graph_sig["captive_sybil"])            # captive misses it
        # the reputation layer catches it: no payer pays an anchor.
        corr = PR.payee_corroboration(g, rep, ring)
        self.assertEqual(corr["reputable_payers"], 0)
        self.assertTrue(corr["sybil_ring"])


class TestSource(unittest.TestCase):
    def test_source_signal_is_superset_of_graph(self):
        a1, e1 = anchor(1)
        agent = payer(500001)
        payee = "0x" + "d" * 40
        edges = e1 + [(agent, a1), (agent, payee), (payer(2), payee), (payer(3), payee)]

        class _Store:
            def iter_settlement_edges(self):
                return iter(edges)
        src = PR.PayerReputationSource.from_store(_Store())
        sig = src.cross_signal(payee)
        # graph keys AND reputation keys present in one signal.
        for k in ("established_payers", "captive_ratio", "captive_sybil",
                  "reputable_payers", "avg_payer_reputation", "sybil_ring"):
            self.assertIn(k, sig)
        self.assertEqual(src.reputation(agent), PR.payer_reputation(
            src.graph, src.anchors, agent)[0])

    def test_absent_payee_none(self):
        class _Store:
            def iter_settlement_edges(self):
                return iter([(payer(1), "0x" + "a" * 40)])
        self.assertIsNone(PR.PayerReputationSource.from_store(_Store())
                          .cross_signal("0x" + "f" * 40))


class TestScreenPayer(unittest.TestCase):
    """Screening a PAYER (the queryable output). Mutation notes: unknown treated as
    negative -> a cold-start end-user is wrongly de-tiered; anchors miscounted ->
    tier wrong."""
    def _source(self):
        a1, e1 = anchor(1); a2, e2 = anchor(2); a3, e3 = anchor(3)
        est = payer(600001)                    # pays 3 anchors -> established
        emg = payer(600002)                    # pays 1 anchor  -> emerging
        unk = payer(600003)                    # pays 1 non-anchor -> unknown
        small = "0x" + "7" * 40
        edges = e1 + e2 + e3 + [
            (est, a1), (est, a2), (est, a3),
            (emg, a1),
            (unk, small)]

        class _Store:
            def iter_settlement_edges(self):
                return iter(edges)
        return PR.PayerReputationSource.from_store(_Store()), est, emg, unk

    def test_tiers(self):
        src, est, emg, unk = self._source()
        self.assertEqual(src.screen(est)["tier"], "established")
        self.assertTrue(src.screen(est)["reputable"])
        self.assertGreaterEqual(src.screen(est)["anchors_paid"], 2)
        self.assertEqual(src.screen(emg)["tier"], "emerging")
        self.assertEqual(src.screen(unk)["tier"], "unknown")

    def test_unknown_is_neutral_not_negative(self):
        src, est, emg, unk = self._source()
        prof = src.screen("0x" + "e" * 40)     # never-seen wallet
        self.assertEqual(prof["tier"], "unknown")
        self.assertEqual(prof["reputation"], 0.0)
        self.assertIn("NEUTRAL", prof["summary"])   # not a block/negative


if __name__ == "__main__":
    unittest.main()
