#!/usr/bin/env python3
"""
test_holder_concentration.py -- TDD for the holder-concentration rug-check. Each test
names the MUTATION it kills.
"""
import unittest

from holder_concentration import (HolderConcentrationSource, apply_concentration,
                                   assess_concentration, top_eoa_share)

WHALE = "0x" + "11" * 20
SMALL = "0x" + "22" * 20
LP = "0x" + "33" * 20
ZERO = "0x" + "00" * 20
TOKEN = "0x" + "44" * 20


class TestTopEoaShare(unittest.TestCase):
    def test_dominant_eoa(self):
        holders = [{"address": WHALE, "value": 700, "is_contract": False},
                   {"address": SMALL, "value": 100, "is_contract": False}]
        share, addr = top_eoa_share(holders, 1000)
        self.assertAlmostEqual(share, 0.70)
        self.assertEqual(addr, WHALE)

    def test_excludes_contracts(self):
        # MUTATION: counting a contract (LP/issuer custody) holder would false-flag a
        # legit RWA where the issuer contract holds most supply.
        holders = [{"address": LP, "value": 900, "is_contract": True},
                   {"address": SMALL, "value": 100, "is_contract": False}]
        share, addr = top_eoa_share(holders, 1000)
        self.assertAlmostEqual(share, 0.10)      # LP excluded; top EOA is SMALL
        self.assertEqual(addr, SMALL)

    def test_excludes_zero_address(self):
        holders = [{"address": ZERO, "value": 999, "is_contract": False},
                   {"address": SMALL, "value": 1, "is_contract": False}]
        share, addr = top_eoa_share(holders, 1000)
        self.assertEqual(addr, SMALL)

    def test_zero_supply_none(self):
        self.assertEqual(top_eoa_share([{"address": WHALE, "value": 1}], 0), (None, None))

    def test_no_eoa_holders_none(self):
        self.assertEqual(top_eoa_share([{"address": LP, "value": 999, "is_contract": True}],
                                       1000), (None, None))


class TestAssess(unittest.TestCase):
    def test_concentrated(self):
        self.assertEqual(assess_concentration(0.8, WHALE)["grade"], "concentrated")

    def test_dispersed(self):
        self.assertEqual(assess_concentration(0.2)["grade"], "dispersed")

    def test_unknown(self):
        self.assertEqual(assess_concentration(None)["grade"], "unknown")


class TestApply(unittest.TestCase):
    def _go(self):
        return {"verdict": "GO", "reasons": [], "signals": {}}

    def test_concentrated_holds(self):
        out = apply_concentration(self._go(), assess_concentration(0.9, WHALE))
        self.assertEqual(out["verdict"], "HOLD")
        self.assertEqual(out["signals"]["holder_concentration"]["grade"], "concentrated")

    def test_dispersed_stays_go(self):
        self.assertEqual(apply_concentration(self._go(),
                         assess_concentration(0.1))["verdict"], "GO")

    def test_never_touches_stop(self):
        stop = {"verdict": "STOP", "reasons": [], "signals": {}}
        self.assertEqual(apply_concentration(stop, assess_concentration(0.99))["verdict"],
                         "STOP")

    def test_none_noop_nonmutating(self):
        base = self._go()
        self.assertEqual(apply_concentration(base, None), base)
        self.assertNotIn("holder_concentration", base["signals"])


class TestSource(unittest.TestCase):
    def _get(self, total, items):
        def get(url):
            if url.endswith("/holders"):
                return {"items": items}
            return {"total_supply": str(total)}
        return get

    def test_concentrated_via_blockscout_shape(self):
        items = [{"address": {"hash": WHALE, "is_contract": False}, "value": "800"},
                 {"address": {"hash": LP, "is_contract": True}, "value": "150"}]
        src = HolderConcentrationSource(get=self._get(1000, items))
        sig = src.check(TOKEN, "ethereum")
        self.assertEqual(sig["grade"], "concentrated")   # 800/1000 EOA

    def test_contract_whale_not_flagged(self):
        # MUTATION: a huge CONTRACT holder (issuer custody) must NOT flag concentration.
        items = [{"address": {"hash": LP, "is_contract": True}, "value": "950"},
                 {"address": {"hash": SMALL, "is_contract": False}, "value": "50"}]
        sig = HolderConcentrationSource(get=self._get(1000, items)).check(TOKEN, "ethereum")
        self.assertEqual(sig["grade"], "dispersed")      # top EOA is 5%

    def test_unsupported_chain_none(self):
        self.assertIsNone(HolderConcentrationSource(get=self._get(1, [])).check(TOKEN, "dogechain"))

    def test_fetch_raises_fail_open(self):
        def boom(url):
            raise RuntimeError("blockscout down")
        self.assertIsNone(HolderConcentrationSource(get=boom).check(TOKEN, "ethereum"))


if __name__ == "__main__":
    unittest.main()
