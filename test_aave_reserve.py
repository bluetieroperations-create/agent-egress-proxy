#!/usr/bin/env python3
"""
test_aave_reserve.py -- TDD for the Aave v3 reserve advisory source. Each test names the
MUTATION it kills.
"""
import unittest

from aave_reserve import (AaveReserveSource, apply_aave, assess_aave,
                          decode_reserve_config)

TOKEN = "0x" + "22" * 20


def _config(ltv=8000, collateral=1, active=1, frozen=0):
    # 10 static words: decimals, ltv, liqThresh, liqBonus, reserveFactor,
    # usageAsCollateral, borrowingEnabled, stableBorrow, isActive, isFrozen.
    vals = [18, ltv, 8250, 10500, 1000, collateral, 1, 0, active, frozen]
    return "0x" + "".join(format(v, "064x") for v in vals)


class TestDecode(unittest.TestCase):
    def test_decodes_fields(self):
        c = decode_reserve_config(_config(ltv=8050, active=1, frozen=0))
        self.assertEqual(c["ltv"], 8050)
        self.assertTrue(c["is_active"])
        self.assertFalse(c["is_frozen"])

    def test_frozen_bit(self):
        # MUTATION: reading the wrong word for isFrozen (offset 9) would miss a freeze.
        self.assertTrue(decode_reserve_config(_config(frozen=1))["is_frozen"])

    def test_too_short_none(self):
        self.assertIsNone(decode_reserve_config("0x" + "00" * 64))
        self.assertIsNone(decode_reserve_config(None))


class TestAssess(unittest.TestCase):
    def test_unlisted_when_inactive(self):
        # MUTATION: treating an inactive reserve as listed would fabricate a signal.
        self.assertEqual(assess_aave({"is_active": False})["grade"], "unlisted")

    def test_frozen(self):
        self.assertEqual(assess_aave({"is_active": True, "is_frozen": True})["grade"],
                         "frozen")

    def test_listed(self):
        r = assess_aave({"is_active": True, "is_frozen": False, "ltv": 8000})
        self.assertEqual(r["grade"], "listed")
        self.assertEqual(r["ltv"], 8000)


class TestApply(unittest.TestCase):
    def _go(self):
        return {"verdict": "GO", "reasons": [], "signals": {}}

    def test_frozen_records_but_does_not_gate(self):
        # ADVISORY: a frozen reserve records the signal + a note but must NOT flip GO
        # (the aggregation layer weighs it collectively).
        out = apply_aave(self._go(), assess_aave({"is_active": True, "is_frozen": True}))
        self.assertEqual(out["verdict"], "GO")
        self.assertEqual(out["signals"]["aave"]["grade"], "frozen")

    def test_listed_positive_note(self):
        out = apply_aave(self._go(), assess_aave({"is_active": True, "is_frozen": False,
                                                  "ltv": 8000}))
        self.assertEqual(out["signals"]["aave"]["grade"], "listed")

    def test_unlisted_noop(self):
        base = self._go()
        self.assertEqual(apply_aave(base, assess_aave({"is_active": False})), base)
        self.assertNotIn("aave", base["signals"])


class TestSource(unittest.TestCase):
    def test_listed_token(self):
        src = AaveReserveSource(eth_call=lambda to, d: _config(ltv=8050))
        self.assertEqual(src.check(TOKEN, "ethereum")["grade"], "listed")

    def test_frozen_token(self):
        src = AaveReserveSource(eth_call=lambda to, d: _config(frozen=1))
        self.assertEqual(src.check(TOKEN, "ethereum")["grade"], "frozen")

    def test_inactive_reserve_none(self):
        # not an active reserve (the common RWA case) -> no signal.
        src = AaveReserveSource(eth_call=lambda to, d: _config(active=0))
        self.assertIsNone(src.check(TOKEN, "ethereum"))

    def test_unsupported_chain_none(self):
        self.assertIsNone(AaveReserveSource(eth_call=lambda to, d: _config()).check(
            TOKEN, "dogechain"))

    def test_transport_raises_fail_open(self):
        def boom(to, d):
            raise RuntimeError("rpc down")
        self.assertIsNone(AaveReserveSource(eth_call=boom).check(TOKEN, "ethereum"))


if __name__ == "__main__":
    unittest.main()
