"""
Tests for seller_intel.py -- the seller-side products (price benchmark, defection
alerts). Pure functions, no store, no network. Each test names the mutation it kills.

The defection tests matter most: the first working version of that function
flagged 322 events, of which 175 were a buyer alternating between two sellers
inside one session -- one pair TEN SECONDS apart. Those tests exist so that
false-positive class cannot come back. An alert product that cries wolf on its
first message is worse than no product.
"""
import unittest

import seller_intel as S


def _events(*rows):
    """rows: (payer, ts, seller) -> {payer: [(ts, seller)]}"""
    out = {}
    for payer, ts, seller in rows:
        out.setdefault(payer, []).append((ts, seller))
    return out


def _day(n, hour=12):
    return "2026-08-%02dT%02d:00:00.000000Z" % (n, hour)


class TestDataQualityGuards(unittest.TestCase):
    def test_page_capped_counts_are_flagged(self):
        # Mutation: trust a page-capped total -> every derived figure understates.
        for n in (100, 150, 200, 250, 300, 500):
            self.assertTrue(S.is_truncated(n), n)
        for n in (99, 151, 201, 437):
            self.assertFalse(S.is_truncated(n), n)

    def test_whale_skew_detected(self):
        # Mutation: keep a whale-skewed payee -> scaling its revenue by a price
        # multiple invents money that was never per-transaction revenue.
        self.assertTrue(S.is_whale_skewed([0.01] * 20 + [5000.0]))
        self.assertFalse(S.is_whale_skewed([0.01, 0.012, 0.009, 0.011]))

    def test_whale_skew_is_safe_on_degenerate_input(self):
        self.assertTrue(S.is_whale_skewed([]))
        self.assertTrue(S.is_whale_skewed([0.0, 0.0]))

    def test_clean_sellers_applies_every_guard(self):
        amounts = {
            "thin": [0.01] * 5,                  # too little history
            "capped": [0.01] * 200,              # page cap
            "whale": [0.01] * 20 + [9000.0],     # skewed
            "good": [0.01] * 40,
        }
        self.assertEqual(sorted(S.clean_sellers(amounts)), ["good"])


class TestCategoryBaselines(unittest.TestCase):
    def test_median_of_medians_not_of_all_amounts(self):
        # Mutation: pool every amount -> one high-VOLUME seller defines the
        # category's "normal" for everyone else.
        amounts = {"loud": [0.10] * 400}
        amounts.update({"q%d" % i: [0.01] * 20 for i in range(5)})
        cats = dict({"loud": "c"}, **{"q%d" % i: "c" for i in range(5)})
        # 'loud' is page-capped-free but its 400 amounts must not dominate.
        self.assertEqual(S.category_baselines(amounts, cats)["c"], 0.01)

    def test_thin_category_gets_no_baseline(self):
        # Mutation: quote a median from two sellers -> advice from noise.
        amounts = {"a": [0.01] * 20, "b": [0.02] * 20}
        self.assertEqual(S.category_baselines(amounts, {"a": "c", "b": "c"}), {})


class TestPriceBenchmark(unittest.TestCase):
    def setUp(self):
        self.amounts = {"me": [0.001] * 40}
        self.amounts.update({"p%d" % i: [0.01] * 20 for i in range(5)})
        self.cats = dict({"me": "c"}, **{"p%d" % i: "c" for i in range(5)})
        self.base = S.category_baselines(self.amounts, self.cats)

    def test_reports_position_and_multiple(self):
        b = S.price_benchmark("me", self.amounts["me"], "c", self.base)
        self.assertEqual(b["position"], "below")
        self.assertAlmostEqual(b["multiple_to_median"], 10.0)
        self.assertAlmostEqual(b["observed_revenue"], 0.04)
        self.assertAlmostEqual(b["revenue_at_category_median"], 0.4)

    def test_refuses_a_page_capped_seller(self):
        # Mutation: benchmark a truncated history -> confident advice from a
        # revenue figure we know is wrong. Returning None is the feature.
        self.assertIsNone(S.price_benchmark("x", [0.001] * 200, "c", self.base))

    def test_refuses_a_whale_skewed_seller(self):
        self.assertIsNone(
            S.price_benchmark("x", [0.001] * 40 + [9000.0], "c", self.base))

    def test_refuses_when_the_category_has_no_baseline(self):
        self.assertIsNone(S.price_benchmark("me", self.amounts["me"], "nope", self.base))

    def test_refuses_on_thin_history(self):
        self.assertIsNone(S.price_benchmark("x", [0.001] * 3, "c", self.base))

    def test_uplift_is_zero_for_a_seller_already_above(self):
        # Mutation: report a negative "uplift" and invite a price CUT, which the
        # data does not support in either direction.
        b = S.price_benchmark("hi", [0.10] * 40, "c", self.base)
        self.assertEqual(b["position"], "above")
        self.assertEqual(b["implied_uplift"], 0.0)

    def test_every_result_carries_the_causation_caveat(self):
        # Mutation: drop the note -> the number gets quoted as a promise.
        b = S.price_benchmark("me", self.amounts["me"], "c", self.base)
        self.assertIn("Observational", b["uplift_note"])

    def test_rank_underpriced_lists_only_the_below_group(self):
        rows = S.rank_underpriced(self.amounts, self.cats, self.base)
        self.assertEqual([r["seller"] for r in rows], ["me"])


class TestDefections(unittest.TestCase):
    """A defection needs all three conditions. Each test removes one."""

    def _loyal_then_gone(self, gap_days=30, after=6):
        rows = [("buyer", _day(1, h), "seller_a") for h in range(10)]
        rows += [("buyer", _day(1 + gap_days, h), "seller_b") for h in range(after)]
        return _events(*rows)

    def test_detects_a_real_defection(self):
        d = S.defections(self._loyal_then_gone())
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["left_seller"], "seller_a")
        self.assertEqual(d[0]["purchases"], 10)
        self.assertEqual(d[0]["moved_to"], ["seller_b"])
        self.assertGreater(d[0]["gap_days"], 7)

    def test_session_interleaving_is_not_a_defection(self):
        # THE BUG THIS FILE EXISTS FOR. A buyer using two sellers seconds apart is
        # multi-homing, not churn. 175 of an initial 322 "defections" were this.
        rows = [("buyer", "2026-08-01T12:00:%02d.000000Z" % s, "seller_a")
                for s in range(10)]
        rows += [("buyer", "2026-08-01T12:00:%02d.000000Z" % s, "seller_b")
                 for s in range(10, 16)]
        self.assertEqual(S.defections(_events(*rows)), [])

    def test_a_single_stray_call_elsewhere_is_not_a_defection(self):
        # Mutation: drop min_purchases_after -> one experimental call to another
        # seller marks a loyal buyer as lost.
        self.assertEqual(S.defections(self._loyal_then_gone(after=1)), [])

    def test_a_buyer_who_left_x402_entirely_is_not_a_defection(self):
        # Mutation: report it anyway -> the seller chases someone who is gone,
        # and we look like we cannot tell the difference.
        rows = [("buyer", _day(1, h), "seller_a") for h in range(12)]
        self.assertEqual(S.defections(_events(*rows)), [])

    def test_a_light_relationship_is_not_a_defection(self):
        rows = [("buyer", _day(1, h), "seller_a") for h in range(3)]
        rows += [("buyer", _day(20, h), "seller_b") for h in range(6)]
        self.assertEqual(S.defections(_events(*rows)), [])

    def test_unparseable_timestamps_suppress_rather_than_invent(self):
        # Mutation: treat an unparseable gap as passing -> silent false alerts.
        rows = [("buyer", "not-a-date-%d" % h, "seller_a") for h in range(12)]
        rows += [("buyer", "zzz-later-%d" % h, "seller_b") for h in range(6)]
        self.assertEqual(S.defections(_events(*rows)), [])

    def test_defections_for_filters_to_one_seller(self):
        d = S.defections(self._loyal_then_gone())
        self.assertEqual(len(S.defections_for("seller_a", d)), 1)
        self.assertEqual(S.defections_for("seller_b", d), [])


class TestSellerReport(unittest.TestCase):
    def test_combines_both_halves(self):
        amounts = {"seller_a": [0.001] * 40}
        amounts.update({"p%d" % i: [0.01] * 20 for i in range(5)})
        cats = dict({"seller_a": "c"}, **{"p%d" % i: "c" for i in range(5)})
        rows = [("buyer", _day(1, h), "seller_a") for h in range(10)]
        rows += [("buyer", _day(25, h), "seller_b") for h in range(6)]
        rep = S.seller_report("seller_a", amounts_by_seller=amounts,
                              category_of=cats, events_by_payer=_events(*rows))
        self.assertEqual(rep["pricing"]["position"], "below")
        self.assertEqual(rep["buyers_lost"], 1)
        self.assertEqual(rep["purchases_lost"], 10)

    def test_report_survives_an_unmeasurable_seller(self):
        # Mutation: raise instead of returning pricing=None -> the whole report
        # dies for a seller we merely cannot price.
        rep = S.seller_report("ghost", amounts_by_seller={}, category_of={},
                              events_by_payer={})
        self.assertIsNone(rep["pricing"])
        self.assertEqual(rep["defections"], [])


if __name__ == "__main__":
    unittest.main()
