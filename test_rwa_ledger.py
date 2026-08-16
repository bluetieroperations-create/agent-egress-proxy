#!/usr/bin/env python3
"""
test_rwa_ledger.py -- TDD for the RWA accumulation corpus. Each test names the
MUTATION it kills.
"""
import os
import tempfile
import unittest

from rwa_ledger import RwaLedger, build_rwa_event

EVM = "0xF758E87CA18824b767aa4F3ed58C188d3bABE428"


def _clean(token=EVM, chain="ethereum", cp="0x" + "cc" * 20, amount="100.00"):
    return {"counterparty": cp, "payer": "0x" + "11" * 20, "amount": amount,
            "chain": chain, "acquires": {"token": token, "chain": chain}}


class TestBuildEvent(unittest.TestCase):
    def test_captures_context(self):
        ev = build_rwa_event(
            _clean(), {"verdict": "HOLD"},
            asset_record={"issuer": "backed", "symbol": "NVDAx",
                          "underlying_symbol": "NVDA", "trading_halted": True},
            rwa_signal={"grade": "blocked", "standard": "erc3643"},
            peg={"divergence_ratio": 1.3, "underlying_price": 100.0,
                 "paid_unit_price": 130.0},
            now=1000)
        self.assertEqual(ev["verdict"], "HOLD")
        self.assertEqual(ev["issuer"], "backed")
        self.assertEqual(ev["restriction_grade"], "blocked")
        self.assertEqual(ev["peg_ratio"], 1.3)
        self.assertTrue(ev["trading_halted"])
        self.assertEqual(ev["ts"], 1000)

    def test_tolerates_missing_context(self):
        # MUTATION: assuming asset_record/peg present would crash on a cold-start buy.
        ev = build_rwa_event(_clean(), {"verdict": "GO"})
        self.assertEqual(ev["verdict"], "GO")
        self.assertIsNone(ev["issuer"])
        self.assertIsNone(ev["peg_ratio"])


class TestLedgerStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "rwa.jsonl")
        self.led = RwaLedger(self.path)

    def _rec(self, **kw):
        ev = build_rwa_event(_clean(kw.pop("token", EVM), kw.pop("chain", "ethereum")),
                             {"verdict": kw.pop("verdict", "GO")},
                             asset_record={"issuer": kw.pop("issuer", "backed"),
                                           "symbol": "NVDAx", "underlying_symbol": "NVDA",
                                           "trading_halted": kw.pop("halted", False)},
                             rwa_signal={"grade": kw.pop("grade", None)},
                             peg={"divergence_ratio": kw.pop("peg", None)},
                             now=kw.pop("now", 1))
        self.led.record(ev)

    def test_record_load_roundtrip(self):
        self._rec()
        self._rec()
        self.assertEqual(len(self.led.load()), 2)

    def test_asset_profile_aggregates(self):
        self._rec(verdict="GO", peg=1.0)
        self._rec(verdict="HOLD", grade="blocked", peg=1.4, now=5)
        self._rec(verdict="GO", halted=True)
        # MUTATION: a case-sensitive token match would miss the lowercased request.
        p = self.led.asset_profile(EVM.lower(), "ethereum")
        self.assertEqual(p["count"], 3)
        self.assertEqual(p["verdicts"], {"GO": 2, "HOLD": 1})
        self.assertEqual(p["restriction_grades"], {"blocked": 1})
        self.assertEqual(p["halt_count"], 1)
        self.assertEqual(p["peg_samples"], 2)
        self.assertAlmostEqual(p["max_peg_ratio"], 1.4)
        self.assertEqual(p["last_seen"], 5)

    def test_asset_profile_unknown_none(self):
        self._rec()
        self.assertIsNone(self.led.asset_profile("0x" + "00" * 20, "ethereum"))

    def test_issuer_profile_rolls_up_assets(self):
        self._rec(token=EVM, verdict="GO")
        self._rec(token="0x" + "ab" * 20, verdict="HOLD", chain="base")
        prof = self.led.issuer_profile("backed")
        self.assertEqual(prof["count"], 2)
        self.assertEqual(prof["distinct_assets"], 2)
        self.assertEqual(prof["verdicts"], {"GO": 1, "HOLD": 1})
        self.assertEqual(set(prof["chains"]), {"ethereum", "base"})

    def test_record_failsoft_on_bad_path(self):
        # MUTATION: an unguarded open() would crash the verdict on an unwritable path.
        bad = RwaLedger("/nonexistent-dir-xyz/rwa.jsonl")
        bad.record({"x": 1})            # must not raise
        self.assertEqual(bad.load(), [])

    def test_load_skips_malformed_line(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"verdict":"GO"}\n')
            fh.write("not json\n")
            fh.write('{"verdict":"HOLD"}\n')
        self.assertEqual(len(self.led.load()), 2)


class TestForecastAccumulation(unittest.TestCase):
    def test_forecast_writes_an_event(self):
        import blackwall
        d = tempfile.mkdtemp()
        led = RwaLedger(os.path.join(d, "rwa.jsonl"))
        payload = {"counterparty": "0xKNOWNGOOD000000000000000000000000000001",
                   "amount": "100.00", "asset": "USDC", "chain": "base",
                   "resource": "https://api.example.com/buy",
                   "acquires": {"token": EVM, "chain": "base"}}
        blackwall.forecast(payload, blackwall.MockReputationSource(), rwa_ledger=led)
        rows = led.load()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["token"], EVM)


if __name__ == "__main__":
    unittest.main()
