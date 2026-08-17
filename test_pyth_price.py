#!/usr/bin/env python3
"""
test_pyth_price.py -- TDD for the Pyth peg/NAV divergence signal. Each test names the
MUTATION it kills.
"""
import unittest

from pyth_price import (PythPriceSource, apply_peg, compute_peg, peg_divergence_ratio,
                        pyth_price_value, select_equity_feed)


class TestPriceValue(unittest.TestCase):
    def test_mantissa_expo(self):
        # MUTATION: ignoring expo (or wrong sign) would misprice by orders of magnitude.
        self.assertAlmostEqual(pyth_price_value({"price": "29564500", "expo": -5}),
                               295.645, places=3)

    def test_bad_inputs_none(self):
        self.assertIsNone(pyth_price_value({"price": "x", "expo": -5}))
        self.assertIsNone(pyth_price_value(None))
        self.assertIsNone(pyth_price_value({}))


class TestSelectFeed(unittest.TestCase):
    FEEDS = [
        {"id": "dep", "attributes": {"base": "AAPL", "asset_type": "Equity",
                                     "symbol": "Equity.US.AAPL/USD.ON",
                                     "description": "DEPRECATED FEED - OVERNIGHT"}},
        {"id": "good", "attributes": {"base": "AAPL", "asset_type": "Equity",
                                      "symbol": "Equity.US.AAPL/USD",
                                      "description": "APPLE INC / US DOLLAR"}},
        {"id": "other", "attributes": {"base": "MSFT", "asset_type": "Equity",
                                       "symbol": "Equity.US.MSFT/USD"}},
    ]

    def test_prefers_non_deprecated_exact_base(self):
        # MUTATION: taking the first result would pick the DEPRECATED overnight feed.
        self.assertEqual(select_equity_feed(self.FEEDS, "AAPL"), "good")

    def test_no_match_none(self):
        self.assertIsNone(select_equity_feed(self.FEEDS, "TSLA"))


class TestDivergence(unittest.TestCase):
    def test_ratio(self):
        self.assertAlmostEqual(peg_divergence_ratio(110, 100), 1.10)

    def test_zero_and_bad_none(self):
        self.assertIsNone(peg_divergence_ratio(100, 0))
        self.assertIsNone(peg_divergence_ratio("x", 100))


class TestComputePeg(unittest.TestCase):
    class _Src:
        def __init__(self, px):
            self.px = px

        def price(self, sym):
            return self.px

    def test_unit_price_explicit(self):
        peg = compute_peg(self._Src(100.0), "AAPL", "0", {"unit_price": "110"})
        self.assertAlmostEqual(peg["divergence_ratio"], 1.10)

    def test_amount_over_quantity(self):
        # implied unit = amount/quantity = 220/2 = 110 vs underlying 100 -> 1.10
        peg = compute_peg(self._Src(100.0), "AAPL", "220", {"quantity": "2"})
        self.assertAlmostEqual(peg["divergence_ratio"], 1.10)

    def test_no_unit_price_none(self):
        self.assertIsNone(compute_peg(self._Src(100.0), "AAPL", "220", {}))

    def test_oracle_unreachable_keeps_paid_price(self):
        # MUTATION: dropping the record when the oracle is down loses the accumulation
        # data point (paid price is still worth recording).
        peg = compute_peg(self._Src(None), "AAPL", "0", {"unit_price": "110"})
        self.assertIsNone(peg["divergence_ratio"])
        self.assertEqual(peg["paid_unit_price"], 110.0)


class TestApplyPeg(unittest.TestCase):
    def _go(self):
        return {"verdict": "GO", "reasons": [], "signals": {}}

    def test_overpay_escalates_hold(self):
        # MUTATION: not gating on overpay lets an agent buy a depegged token.
        peg = {"underlying_symbol": "AAPL", "underlying_price": 100.0,
               "paid_unit_price": 130.0, "divergence_ratio": 1.30}
        out = apply_peg(self._go(), peg)
        self.assertEqual(out["verdict"], "HOLD")

    def test_within_peg_stays_go(self):
        peg = {"underlying_symbol": "AAPL", "underlying_price": 100.0,
               "paid_unit_price": 101.0, "divergence_ratio": 1.01}
        self.assertEqual(apply_peg(self._go(), peg)["verdict"], "GO")

    def test_discount_not_anomalous(self):
        peg = {"underlying_symbol": "AAPL", "underlying_price": 100.0,
               "paid_unit_price": 80.0, "divergence_ratio": 0.80}
        self.assertEqual(apply_peg(self._go(), peg)["verdict"], "GO")

    def test_never_touches_stop(self):
        peg = {"underlying_symbol": "AAPL", "underlying_price": 100.0,
               "paid_unit_price": 300.0, "divergence_ratio": 3.0}
        stop = {"verdict": "STOP", "reasons": [], "signals": {}}
        self.assertEqual(apply_peg(stop, peg)["verdict"], "STOP")

    def test_none_noop_nonmutating(self):
        base = self._go()
        self.assertEqual(apply_peg(base, None), base)
        self.assertNotIn("peg", base["signals"])


class TestPythSource(unittest.TestCase):
    def test_resolve_and_price_via_injected_get(self):
        feeds = [{"id": "abc", "attributes": {"base": "AAPL", "asset_type": "Equity",
                                              "symbol": "Equity.US.AAPL/USD"}}]

        def get(url):
            if "price_feeds" in url:
                return feeds
            return {"parsed": [{"price": {"price": "10000", "expo": -2}}]}   # $100.00
        self.assertAlmostEqual(PythPriceSource(get=get).price("AAPL"), 100.0)

    def test_feed_map_shortcut_and_failopen(self):
        def boom(url):
            raise RuntimeError("hermes down")
        # feed_map avoids the resolve call, but the price fetch still fails open.
        self.assertIsNone(PythPriceSource(get=boom, feed_map={"AAPL": "abc"}).price("AAPL"))


if __name__ == "__main__":
    unittest.main()
