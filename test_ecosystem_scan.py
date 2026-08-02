"""
Tests for ecosystem_scan.py -- profiles + the four derived views. Each test states
its mutation.
"""
import unittest

import ecosystem_scan as E

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40


def _r(payee, price=1000, resource="https://svc/x", network="eip155:8453"):
    return {"payTo": payee, "price_atomic": price, "resource": resource,
            "asset": USDC, "network": network}


class _Store:
    def __init__(self, recs):
        self.recs = recs

    def lookup(self, cp):
        return self.recs.get(cp, {})


class TestProfiles(unittest.TestCase):
    """Mutation notes: not grouping by payee -> duplicate profiles; not reading the
    store -> no reputation; not flagging sanctioned -> the sink breaks."""

    def test_groups_and_enriches(self):
        resources = [_r(A, resource="https://svc/1"), _r(A, resource="https://svc/2"),
                     _r(B)]
        store = _Store({A: {"settlement_count": 40, "distinct_payers": 6},
                        B: {"settlement_count": 3, "distinct_payers": 1}})
        profs = {p["payee"]: p for p in
                 E.build_profiles(resources, store=store, sanctioned=[])}
        self.assertEqual(profs[A]["resource_count"], 2)
        self.assertEqual(profs[A]["distinct_payers"], 6)
        self.assertFalse(profs[A]["thin"])
        self.assertTrue(profs[B]["thin"])                 # 1 < 3 distinct payers

    def test_sanctioned_flag(self):
        profs = {p["payee"]: p for p in
                 E.build_profiles([_r(A), _r(B)], sanctioned=[A])}
        self.assertTrue(profs[A]["sanctioned"])
        self.assertFalse(profs[B]["sanctioned"])

    def test_price_human(self):
        p = E.build_profiles([_r(A, price=90000)])[0]
        self.assertEqual(p["min_price"], "0.09")

    def test_zero_settlements_is_unknown_not_thin(self):
        # a payee with no ingested history -> distinct UNKNOWN (None), not "0 thin"
        store = _Store({A: {"settlement_count": 0, "distinct_payers": 0}})
        p = E.build_profiles([_r(A)], store=store)[0]
        self.assertIsNone(p["distinct_payers"])
        self.assertFalse(p["thin"])


class TestDirectory(unittest.TestCase):
    """
    Mutation notes:
      - sanctioned not sunk to the bottom -> test_sanctioned_last FAILS.
      - distinct payers not dominating the score -> test_order FAILS.
    """
    def _profiles(self):
        store = _Store({A: {"settlement_count": 100, "distinct_payers": 20},
                        B: {"settlement_count": 50, "distinct_payers": 2},
                        C: {"settlement_count": 999, "distinct_payers": 99}})
        return E.build_profiles([_r(A), _r(B), _r(C)], store=store, sanctioned=[C])

    def test_order_by_distinct_then_volume(self):
        d = E.rank_directory(self._profiles())
        self.assertEqual(d[0]["payee"], A)                # 20 distinct payers -> top
        self.assertEqual(d[-1]["payee"], C)               # sanctioned -> bottom

    def test_sanctioned_scores_zero(self):
        d = {p["payee"]: p for p in E.rank_directory(self._profiles())}
        self.assertEqual(d[C]["trust_score"], 0.0)
        self.assertIn("sanctioned", d[C]["trust_reason"])

    def test_top_limit(self):
        self.assertEqual(len(E.rank_directory(self._profiles(), top=2)), 2)


class TestAuditCandidates(unittest.TestCase):
    """Mutation notes: include sanctioned -> a bad endpoint gets pitched; include
    already-verified -> we re-pitch our own customers."""

    def test_excludes_sanctioned_and_verified(self):
        store = _Store({A: {"settlement_count": 40, "distinct_payers": 6},
                        B: {"settlement_count": 40, "distinct_payers": 6},
                        C: {"settlement_count": 40, "distinct_payers": 6}})
        profs = E.build_profiles([_r(A), _r(B), _r(C)], store=store, sanctioned=[C])
        cands = E.audit_candidates(profs, verified=[B])
        names = {c["payee"] for c in cands}
        self.assertIn(A, names)
        self.assertNotIn(B, names)                        # already verified
        self.assertNotIn(C, names)                        # sanctioned

    def test_ranked_by_opportunity(self):
        store = _Store({A: {"settlement_count": 5}, B: {"settlement_count": 500}})
        profs = E.build_profiles([_r(A), _r(B)], store=store)
        cands = E.audit_candidates(profs)
        self.assertEqual(cands[0]["payee"], B)            # more activity first


class TestStats(unittest.TestCase):
    def test_aggregates(self):
        store = _Store({A: {"settlement_count": 10, "distinct_payers": 6},
                        B: {"settlement_count": 4, "distinct_payers": 1}})
        resources = [_r(A, price=1000, resource="https://svc/1"),
                     _r(A, price=2000, resource="https://svc/2"),
                     _r(B, price=90000)]
        stats = E.ecosystem_stats(
            E.build_profiles(resources, store=store, sanctioned=[]), resources)
        self.assertEqual(stats["endpoints"], 2)
        self.assertEqual(stats["resources"], 3)
        self.assertEqual(stats["with_onchain_history"], 2)
        self.assertEqual(stats["thin_distinct_payers"], 1)    # B
        self.assertEqual(stats["multi_resource_endpoints"], 1)  # A
        self.assertEqual(stats["price_usdc"]["min"], "0.001")
        self.assertEqual(stats["price_usdc"]["max"], "0.09")

    def test_scan_returns_all_four(self):
        out = E.scan([_r(A), _r(B)], sanctioned=[])
        self.assertEqual(set(out), {"profiles", "stats", "directory", "candidates"})


if __name__ == "__main__":
    unittest.main()
