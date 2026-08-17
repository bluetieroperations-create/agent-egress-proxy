#!/usr/bin/env python3
"""
test_settlement_sim.py -- TDD for pre-signature x402 settlement feasibility. Each test
names the MUTATION it kills. Revert strings are the REAL ones harvested live from USDC.
"""
import unittest

from settlement_sim import (PAYEE_BLOCKED, PAYER_BLOCKED, READY, SIM_UNKNOWN, UNDERFUNDED,
                            SettlementSimSource, apply_settlement_sim, assess_settlement)
from transfer_sim import OK, RECEIVER_BLOCKED, SENDER_BLOCKED, UNKNOWN, TransferSimulator

PAYER = "0x" + "a1" * 20
PAYEE = "0x" + "b2" * 20
BLACKLIST = "Blacklistable: account is blacklisted"      # LIVE-harvested from USDC


def _err(msg):
    body = msg.encode()
    return {"error": {"message": "execution reverted",
                      "data": "0x08c379a0" + "%064x" % 32 + "%064x" % len(body)
                              + body.hex().ljust(64, "0")}}


class TestAssess(unittest.TestCase):
    def test_ok_is_ready(self):
        self.assertEqual(assess_settlement({"outcome": OK})["grade"], READY)

    def test_receiver_restriction_is_payee_blocked(self):
        s = assess_settlement({"outcome": RECEIVER_BLOCKED, "revert_class": "restriction",
                               "reason": BLACKLIST})
        self.assertEqual(s["grade"], PAYEE_BLOCKED)
        self.assertEqual(s["blocked_party"], "payee")

    def test_sender_restriction_is_payer_blocked(self):
        s = assess_settlement({"outcome": SENDER_BLOCKED, "revert_class": "restriction",
                               "reason": BLACKLIST})
        self.assertEqual(s["grade"], PAYER_BLOCKED)
        self.assertEqual(s["blocked_party"], "payer")

    def test_balance_is_underfunded_not_blocked(self):
        # MUTATION: treating a balance revert as a block would gate every under-funded
        # agent as if its counterparty were blacklisted.
        s = assess_settlement({"outcome": SENDER_BLOCKED, "revert_class": "balance",
                               "reason": "ERC20: transfer amount exceeds balance"})
        self.assertEqual(s["grade"], UNDERFUNDED)

    def test_opaque_and_unknown_are_unknown(self):
        for cls in ("opaque", "other", "gas", None):
            self.assertEqual(
                assess_settlement({"outcome": RECEIVER_BLOCKED, "revert_class": cls,
                                   "reason": "x"})["grade"], SIM_UNKNOWN)
        self.assertEqual(assess_settlement({"outcome": UNKNOWN})["grade"], SIM_UNKNOWN)

    def test_never_raises(self):
        for j in (None, "x", 5, []):
            assess_settlement(j)


class TestApplyFold(unittest.TestCase):
    def test_payee_blocked_escalates_go_to_hold(self):
        v = apply_settlement_sim({"verdict": "GO"},
                                 {"grade": PAYEE_BLOCKED, "blocked_party": "payee",
                                  "revert_class": "restriction", "reason": BLACKLIST})
        self.assertEqual(v["verdict"], "HOLD")
        self.assertTrue(v["signals"]["settlement_sim"]["gated"])

    def test_payer_blocked_escalates(self):
        v = apply_settlement_sim({"verdict": "GO"},
                                 {"grade": PAYER_BLOCKED, "blocked_party": "payer",
                                  "revert_class": "restriction", "reason": BLACKLIST})
        self.assertEqual(v["verdict"], "HOLD")

    def test_never_stops(self):
        # HARD BOUNDARY (mirrors blockscout.py): sanctions.py is the only STOP authority.
        for g in (PAYEE_BLOCKED, PAYER_BLOCKED, UNDERFUNDED, READY, SIM_UNKNOWN):
            v = apply_settlement_sim({"verdict": "GO"}, {"grade": g})
            self.assertIn(v["verdict"], ("GO", "HOLD"))
            self.assertNotEqual(v["verdict"], "STOP")

    def test_never_upgrades_a_hold_or_stop(self):
        # MUTATION: a clean simulation must NOT clear an existing HOLD/STOP.
        for start in ("HOLD", "STOP"):
            v = apply_settlement_sim({"verdict": start}, {"grade": READY})
            self.assertEqual(v["verdict"], start)

    def test_underfunded_does_not_gate(self):
        # Balance can change between forecast and signature -- record, don't gate.
        v = apply_settlement_sim({"verdict": "GO"}, {"grade": UNDERFUNDED})
        self.assertEqual(v["verdict"], "GO")
        self.assertFalse(v["signals"]["settlement_sim"]["gated"])

    def test_unknown_grade_and_junk_are_noops(self):
        base = {"verdict": "GO"}
        self.assertEqual(apply_settlement_sim(base, None), base)
        self.assertEqual(apply_settlement_sim(base, {"grade": "bogus"}), base)
        apply_settlement_sim(None, {"grade": READY})          # must not raise

    def test_non_mutating(self):
        base = {"verdict": "GO", "signals": {"x": 1}}
        apply_settlement_sim(base, {"grade": PAYEE_BLOCKED})
        self.assertNotIn("settlement_sim", base["signals"])


class TestSource(unittest.TestCase):
    def _claim(self, **kw):
        c = {"payer": PAYER, "counterparty": PAYEE, "amount": "1.5",
             "asset": "USDC", "chain": "base"}
        c.update(kw)
        return c

    def test_detects_blacklisted_payee(self):
        calls = []

        def t(p):
            calls.append(p)
            # payer->payee reverts; payer->control succeeds => the PAYEE is blocked
            return _err(BLACKLIST) if len(calls) == 1 else {"result": "0x1"}
        src = SettlementSimSource(simulator=TransferSimulator(transport=t))
        self.assertEqual(src.check(self._claim())["grade"], PAYEE_BLOCKED)

    def test_detects_blacklisted_payer(self):
        src = SettlementSimSource(
            simulator=TransferSimulator(transport=lambda p: _err(BLACKLIST)))
        self.assertEqual(src.check(self._claim())["grade"], PAYER_BLOCKED)

    def test_clean_payment_is_ready(self):
        src = SettlementSimSource(
            simulator=TransferSimulator(transport=lambda p: {"result": "0x1"}))
        self.assertEqual(src.check(self._claim())["grade"], READY)

    def test_noop_without_payer_or_on_unknown_asset_chain(self):
        src = SettlementSimSource(
            simulator=TransferSimulator(transport=lambda p: {"result": "0x1"}))
        self.assertIsNone(src.check(self._claim(payer=None)))
        self.assertIsNone(src.check(self._claim(asset="DAI")))
        self.assertIsNone(src.check(self._claim(chain="dogechain")))
        self.assertIsNone(src.check(None))

    def test_noop_without_rpc(self):
        # MUTATION: constructing a simulator with no RPC must be a no-op, not a crash.
        self.assertIsNone(SettlementSimSource().check(self._claim()))

    def test_amount_converted_to_base_units(self):
        seen = {}

        def t(p):
            seen["data"] = p["data"]
            return {"result": "0x1"}
        SettlementSimSource(simulator=TransferSimulator(transport=t)).check(
            self._claim(amount="1.5"))
        # 1.5 USDC = 1_500_000 base units (6 decimals) in the trailing uint256 word
        self.assertTrue(seen["data"].endswith("%064x" % 1500000))

    def test_source_never_raises(self):
        def boom(p):
            raise IOError("net")
        src = SettlementSimSource(simulator=TransferSimulator(transport=boom))
        src.check(self._claim())


if __name__ == "__main__":
    unittest.main()
