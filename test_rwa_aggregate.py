#!/usr/bin/env python3
"""
test_rwa_aggregate.py -- TDD for the RWA signal-aggregation / confidence layer. Each
test names the MUTATION it kills.
"""
import unittest

from rwa_aggregate import aggregate, apply_aggregate


class TestAggregate(unittest.TestCase):
    def test_empty_is_low(self):
        a = aggregate({})
        self.assertEqual(a["level"], "low")
        self.assertEqual(a["score"], 0.0)
        self.assertIsNone(a["primary_concern"])

    def test_gating_signal_ranks_primary(self):
        # A blocked transfer (weight .9) + off-peg (.6): primary is the heavier one.
        a = aggregate({"rwa_transfer_readiness": {"grade": "blocked"},
                       "dex_market": {"grade": "off_peg"}})
        self.assertEqual(a["level"], "high")
        self.assertIn("REVERT", a["primary_concern"])
        self.assertEqual(len(a["gating"]), 2)
        self.assertGreater(a["score"], 0)

    def test_peg_ratio_threshold(self):
        # MUTATION: reading peg as bad below 1.10 (or ignoring it) would mis-score.
        self.assertEqual(aggregate({"peg": {"divergence_ratio": 1.05}})["level"], "low")
        self.assertEqual(aggregate({"peg": {"divergence_ratio": 1.30}})["level"], "high")

    def test_advisory_only_is_elevated_not_high(self):
        # holder-concentration alone (.3) -> elevated, and in `advisory`, not `gating`.
        a = aggregate({"holder_concentration": {"grade": "concentrated"}})
        self.assertEqual(a["level"], "elevated")
        self.assertEqual(a["gating"], [])
        self.assertEqual(len(a["advisory"]), 1)
        self.assertAlmostEqual(a["advisory_bad_weight"], 0.3)

    def test_trading_halt_and_backing(self):
        a = aggregate({"rwa_asset": {"trading_halted": True},
                       "backing": {"grade": "under_collateralized"}})
        self.assertEqual(a["level"], "high")
        self.assertEqual(len(a["gating"]), 2)

    def test_never_raises_on_junk(self):
        self.assertEqual(aggregate(None)["level"], "low")
        self.assertEqual(aggregate({"peg": "not-a-dict"})["level"], "low")
        self.assertEqual(aggregate({"backing": {"grade": None}})["level"], "low")


class TestApplyAggregate(unittest.TestCase):
    def _go(self):
        return {"verdict": "GO", "reasons": [], "signals": {}}

    def test_records_rwa_risk(self):
        agg = aggregate({"backing": {"grade": "under_collateralized"}})
        out = apply_aggregate(self._go(), agg)
        self.assertEqual(out["signals"]["rwa_risk"]["level"], "high")
        self.assertIn("under-collateralized", out["signals"]["rwa_risk"]["primary_concern"])

    def test_single_advisory_does_not_gate(self):
        # THE POINT: one advisory signal (holder-concentration, .3 < .5) must NOT flip GO.
        # MUTATION: gating on any advisory would reintroduce the false-HOLD noise.
        agg = aggregate({"holder_concentration": {"grade": "concentrated"}})
        self.assertEqual(apply_aggregate(self._go(), agg)["verdict"], "GO")

    def test_collective_advisory_escalates(self):
        # Several advisory signals agreeing past the weight threshold -> HOLD once.
        agg = {"score": 0.2, "level": "elevated", "primary_concern": "x", "concerns": ["a", "b"],
               "advisory": ["a", "b"], "advisory_bad_weight": 0.6}
        out = apply_aggregate(self._go(), agg)
        self.assertEqual(out["verdict"], "HOLD")
        self.assertTrue(any("multiple advisory" in r for r in out["reasons"]))

    def test_never_touches_stop(self):
        agg = {"advisory_bad_weight": 0.9, "advisory": ["a"], "score": 0.5,
               "level": "high", "primary_concern": "x", "concerns": ["a"]}
        stop = {"verdict": "STOP", "reasons": [], "signals": {}}
        self.assertEqual(apply_aggregate(stop, agg)["verdict"], "STOP")

    def test_none_agg_noop(self):
        base = self._go()
        self.assertEqual(apply_aggregate(base, None), base)


class TestForecastAggregation(unittest.TestCase):
    def test_forecast_records_rwa_risk_and_holder_advisory_no_gate(self):
        import blackwall
        from holder_concentration import assess_concentration

        class Holder:
            def check(self, token, chain):
                return assess_concentration(0.9, "0x" + "11" * 20)   # concentrated

        class Reg:
            def lookup(self, token, chain=None):
                return {"issuer": "ondo", "symbol": "AAPLon",
                        "underlying_symbol": "AAPL", "trading_halted": False}

        payload = {"counterparty": "0xKNOWNGOOD000000000000000000000000000001",
                   "amount": "0.09", "asset": "USDC", "chain": "base",
                   "resource": "https://api.example.com/buy",
                   "acquires": {"token": "0x" + "ab" * 20, "chain": "base"}}
        out = blackwall.forecast(payload, blackwall.MockReputationSource(),
                                 stock_registry=Reg(), holder_source=Holder())[0]
        # holder-concentration is ADVISORY now -> records the signal + rwa_risk, but a
        # single advisory does NOT flip the verdict.
        self.assertEqual(out["verdict"], "GO")
        self.assertEqual(out["signals"]["holder_concentration"]["grade"], "concentrated")
        self.assertEqual(out["signals"]["rwa_risk"]["level"], "elevated")


if __name__ == "__main__":
    unittest.main()
