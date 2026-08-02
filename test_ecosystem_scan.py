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


def _r(payee, price=1000, resource="https://svc/x", network="eip155:8453", asset=USDC):
    return {"payTo": payee, "price_atomic": price, "resource": resource,
            "asset": asset, "network": network}


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

    def test_resource_count_is_distinct_not_options(self):
        # one resource URL priced on 3 networks == 3 accepts == 1 distinct resource.
        # Mutation: counting raw accepts inflates resource_count / multi / prices.
        recs = [_r(A, resource="https://svc/pay", network="eip155:8453"),
                _r(A, resource="https://svc/pay", network="eip155:10"),
                _r(A, resource="https://svc/pay", network="eip155:137")]
        p = E.build_profiles(recs)[0]
        self.assertEqual(p["resource_count"], 1)          # ONE distinct URL
        self.assertEqual(p["option_count"], 3)            # three payment options
        st = E.ecosystem_stats(E.build_profiles(recs), recs)
        self.assertEqual(st["resources"], 1)
        self.assertEqual(st["payment_options"], 3)
        self.assertEqual(st["multi_resource_endpoints"], 0)   # not "multi" -- one resource
        self.assertEqual(st["price_usdc"]["count"], 1)        # same price 3x -> counted once

    def test_zero_settlements_is_unknown_not_thin(self):
        # a payee with no ingested history -> distinct UNKNOWN (None), not "0 thin"
        store = _Store({A: {"settlement_count": 0, "distinct_payers": 0}})
        p = E.build_profiles([_r(A)], store=store)[0]
        self.assertIsNone(p["distinct_payers"])
        self.assertFalse(p["thin"])


class TestPriceDecimals(unittest.TestCase):
    """The decimals trap: only KNOWN 6-dp USDC assets get a USD price. An 18-dp
    token amount divided by 10^6 would read ~10^12x too large -> must be excluded,
    not published as a '$10T price'."""
    NON_USDC = "0x431d5dff03120afa4bdf332c61a6e1766ef37bdb"   # an 18-dp token

    def test_non_usdc_asset_not_priced_in_usd(self):
        # 10^19 atomic of an 18-dp token == 10 tokens, NOT $10 trillion.
        recs = [_r(A, price=10**19, asset=self.NON_USDC, network="eip155:137")]
        p = E.build_profiles(recs)[0]
        self.assertIsNone(p["min_price"])                 # unknown decimals -> no USD
        self.assertIsNone(p["max_price"])
        self.assertEqual(p["non_usdc_options"], 1)
        st = E.ecosystem_stats(E.build_profiles(recs), recs)
        self.assertIsNone(st["price_usdc"])               # nothing to distribute
        self.assertEqual(st["non_usdc_priced_options"], 1)

    def test_usdc_price_survives_amid_non_usdc(self):
        recs = [_r(A, price=90000, asset=USDC),                       # $0.09 USDC
                _r(A, price=10**19, asset=self.NON_USDC)]             # excluded
        p = E.build_profiles(recs)[0]
        self.assertEqual(p["max_price"], "0.09")          # not poisoned by the 18-dp row
        st = E.ecosystem_stats(E.build_profiles(recs), recs)
        self.assertEqual(st["price_usdc"]["max"], "0.09")
        self.assertEqual(st["price_usdc"]["count"], 1)

    def test_negative_and_zero_price_excluded(self):
        recs = [_r(A, price=-100, asset=USDC), _r(A, price=0, asset=USDC)]
        p = E.build_profiles(recs)[0]
        self.assertIsNone(p["min_price"])                 # no negative published min
        st = E.ecosystem_stats(E.build_profiles(recs), recs)
        self.assertIsNone(st["price_usdc"])

    def test_percentile_does_not_degenerate_to_max(self):
        # 10 distinct prices; p90 must not equal the max (the int(q*n) bug did).
        recs = [_r(A, price=(i + 1) * 1000, resource="https://svc/%d" % i, asset=USDC)
                for i in range(10)]
        st = E.ecosystem_stats(E.build_profiles(recs), recs)
        self.assertNotEqual(st["price_usdc"]["p90"], st["price_usdc"]["max"])


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

    def test_no_history_scores_zero(self):
        # an endpoint with no on-chain history must not climb the directory on
        # attacker-controllable advertised breadth alone.
        recs = [_r(A, resource="https://svc/%d" % i) for i in range(20)]  # 20 resources
        p = E.build_profiles(recs)[0]                                     # no store -> None
        self.assertIsNone(p["distinct_payers"])
        self.assertEqual(E.trust_score(p), 0.0)


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


class TestCsvSafe(unittest.TestCase):
    """Mutation: writing a raw resource URL starting with = + - @ lets it execute
    as a formula when the BD team opens candidates.csv in Excel/Sheets."""
    def test_formula_prefixes_neutralized(self):
        for bad in ("=cmd()", "+1", "-2", "@x", "\tx", "\rx"):
            self.assertTrue(E._csv_safe(bad).startswith("'"))

    def test_normal_url_untouched(self):
        self.assertEqual(E._csv_safe("https://svc/x"), "https://svc/x")


if __name__ == "__main__":
    unittest.main()
