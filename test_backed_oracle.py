#!/usr/bin/env python3
"""
test_backed_oracle.py -- TDD for the Backed oracle + proof-of-reserves signals. Each
test names the MUTATION it kills.
"""
import unittest

from backed_oracle import (BackedOracleIndex, apply_backing, assess_backing,
                           load_backed_oracle_index, parse_oracles, parse_reserves)

ORACLES = {"nodes": [
    {"symbol": "TSLAx", "metadata": {"hermesId": "abc123", "exponent": -8},
     "collateral": {"symbol": "TSLA"}},
    {"symbol": "XLEx", "metadata": {"hermesId": None, "pythLazerId": 3406},
     "collateral": {"symbol": "XLE"}},
], "page": {"hasNextPage": False}}

RESERVES = {"nodes": [
    {"symbol": "TSLAx", "sharesHeld": "1000", "circulatingSupply": "1000"},   # 100%
    {"symbol": "SHORTx", "sharesHeld": "900", "circulatingSupply": "1000"},    # 90% short
    {"symbol": "NEWx", "sharesHeld": "0", "circulatingSupply": "0"},           # new/inactive
], "page": {"hasNextPage": False}}


class TestParseOracles(unittest.TestCase):
    def test_maps_underlying_and_hermes(self):
        o = parse_oracles(ORACLES)
        self.assertEqual(o["TSLAX"]["underlying"], "TSLA")
        self.assertEqual(o["TSLAX"]["hermes_id"], "abc123")

    def test_null_hermes_is_none(self):
        # MUTATION: keeping a null/empty hermesId would feed a bad feed id to Pyth.
        self.assertIsNone(parse_oracles(ORACLES)["XLEX"]["hermes_id"])

    def test_never_raises(self):
        self.assertEqual(parse_oracles(None), {})
        self.assertEqual(parse_oracles({"nodes": ["junk", 5]}), {})


class TestParseReserves(unittest.TestCase):
    def test_backing_ratio(self):
        r = parse_reserves(RESERVES)
        self.assertAlmostEqual(r["TSLAX"]["backing_ratio"], 1.0)
        self.assertAlmostEqual(r["SHORTX"]["backing_ratio"], 0.9)

    def test_zero_supply_ratio_none_not_zero(self):
        # MUTATION: dividing by zero (or defaulting to 0.0) would crash or falsely flag
        # a brand-new token as unbacked. Supply 0 -> ratio None (unknown).
        self.assertIsNone(parse_reserves(RESERVES)["NEWX"]["backing_ratio"])


class TestAssessBacking(unittest.TestCase):
    def test_under_collateralized_flagged(self):
        r = assess_backing({"backing_ratio": 0.90})
        self.assertEqual(r["grade"], "under_collateralized")

    def test_fully_backed_ready(self):
        self.assertEqual(assess_backing({"backing_ratio": 1.0})["grade"], "ready")

    def test_slack_under_one_still_ready(self):
        # 99% backed is within slack -> ready (benign reporting lag, not a shortfall).
        self.assertEqual(assess_backing({"backing_ratio": 0.99})["grade"], "ready")

    def test_unknown_when_no_ratio(self):
        self.assertEqual(assess_backing({"backing_ratio": None})["grade"], "unknown")
        self.assertEqual(assess_backing({})["grade"], "unknown")


class TestApplyBacking(unittest.TestCase):
    def _go(self):
        return {"verdict": "GO", "reasons": [], "signals": {}}

    def test_under_collateralized_escalates_hold(self):
        out = apply_backing(self._go(), assess_backing({"backing_ratio": 0.85}))
        self.assertEqual(out["verdict"], "HOLD")
        self.assertEqual(out["signals"]["backing"]["grade"], "under_collateralized")

    def test_ready_leaves_go(self):
        self.assertEqual(apply_backing(self._go(),
                         assess_backing({"backing_ratio": 1.0}))["verdict"], "GO")

    def test_never_touches_stop(self):
        stop = {"verdict": "STOP", "reasons": [], "signals": {}}
        self.assertEqual(apply_backing(stop, assess_backing({"backing_ratio": 0.5}))["verdict"],
                         "STOP")

    def test_none_noop_nonmutating(self):
        base = self._go()
        self.assertEqual(apply_backing(base, None), base)
        self.assertNotIn("backing", base["signals"])


class TestIndex(unittest.TestCase):
    def test_feed_map_only_real_hermes(self):
        idx = BackedOracleIndex(oracles=parse_oracles(ORACLES))
        fm = idx.feed_map()
        # MUTATION: mapping the Lazer-only XLEx would send a bad id to Pyth.
        self.assertEqual(fm.get("TSLAX"), "abc123")
        self.assertEqual(fm.get("TSLA"), "abc123")      # underlying ticker too
        self.assertNotIn("XLEX", fm)

    def test_backing_and_underlying_lookup(self):
        idx = BackedOracleIndex(oracles=parse_oracles(ORACLES),
                                reserves=parse_reserves(RESERVES))
        self.assertEqual(idx.underlying("tslax"), "TSLA")
        self.assertAlmostEqual(idx.backing("SHORTx")["backing_ratio"], 0.9)

    def test_load_via_injected_get_paginates_failsoft(self):
        pages = {"oracles": ORACLES, "reserves": RESERVES}

        def get(url):
            return pages["oracles"] if "oracles" in url else pages["reserves"]
        idx = load_backed_oracle_index(get=get)
        self.assertEqual(idx.underlying("TSLAx"), "TSLA")
        self.assertEqual(assess_backing(idx.backing("SHORTx"))["grade"],
                         "under_collateralized")


class TestForecastBacking(unittest.TestCase):
    def test_under_collateralized_holds_forecast(self):
        import blackwall

        class Reg:
            def lookup(self, token, chain=None):
                return {"issuer": "backed", "symbol": "SHORTx",
                        "underlying_symbol": "SHORT", "trading_halted": False}
        idx = BackedOracleIndex(reserves=parse_reserves(RESERVES))
        payload = {"counterparty": "0xKNOWNGOOD000000000000000000000000000001",
                   "amount": "0.09", "asset": "USDC", "chain": "base",
                   "resource": "https://api.example.com/buy",
                   "acquires": {"token": "0x" + "ab" * 20, "chain": "base"}}
        out = blackwall.forecast(payload, blackwall.MockReputationSource(),
                                 stock_registry=Reg(), backing_index=idx)[0]
        self.assertEqual(out["verdict"], "HOLD")
        self.assertEqual(out["signals"]["backing"]["grade"], "under_collateralized")

    def test_no_backing_index_untouched(self):
        import blackwall
        out = blackwall.forecast(
            {"counterparty": "0xKNOWNGOOD000000000000000000000000000001",
             "amount": "0.09", "asset": "USDC", "chain": "base",
             "resource": "https://api.example.com/run"},
            blackwall.MockReputationSource())[0]
        self.assertEqual(out["verdict"], "GO")
        self.assertNotIn("backing", out["signals"])


if __name__ == "__main__":
    unittest.main()
