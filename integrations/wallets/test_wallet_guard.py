"""
Tests for wallet_guard.py -- the provider-agnostic signing gate. Stdlib only.
Run from this directory:  python -m unittest test_wallet_guard.py
"""
import unittest

import wallet_guard as W

GOOD = "0x" + "a" * 40
BAD = "0x" + "b" * 40
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _transfer(to, amt):
    return "0xa9059cbb" + (bytes(12) + bytes.fromhex(to[2:])).hex() \
        + amt.to_bytes(32, "big").hex()


def _fixed(verdict):
    return lambda payload: verdict


def _v(v, **o):
    return dict({"verdict": v, "hard_stop": v == "STOP", "score": 1.0,
                 "reasons": ["r"], "receipt_id": "bw_x"}, **o)


class TestClaimFromTx(unittest.TestCase):
    """Mutation notes: not decoding transfer -> recipient/amount wrong; dropping
    `transaction` -> Phase-3 screen never runs downstream."""

    def test_erc20_transfer(self):
        p = W.claim_from_tx({"to": USDC, "value": 0, "data": _transfer(GOOD, 90000)})
        self.assertEqual(p["counterparty"], GOOD)
        self.assertEqual(p["amount"], "0.09")           # 90000 atomic USDC (6dp)
        self.assertEqual(p["asset"].lower(), USDC.lower())
        self.assertEqual(p["transaction"]["data"][:10], "0xa9059cbb")

    def test_native_transfer(self):
        p = W.claim_from_tx({"to": GOOD, "value": 10 ** 18, "data": "0x"})
        self.assertEqual(p["counterparty"], GOOD)
        self.assertEqual(p["amount"], "1")              # 1e18 wei = 1.0 native

    def test_non_value_call_gets_placeholder(self):
        # an approve (value 0, not a transfer) -> nominal amount so the verdict +
        # calldata drainer-screen still run
        approve = "0x095ea7b3" + (bytes(12) + bytes.fromhex(BAD[2:])).hex() \
            + (100).to_bytes(32, "big").hex()
        p = W.claim_from_tx({"to": USDC, "value": 0, "data": approve})
        self.assertEqual(p["amount"], "0.000001")
        self.assertEqual(p["transaction"]["to"], USDC)


class TestWalletGuardDecision(unittest.TestCase):
    """
    Mutation notes:
      - STOP not -> block / GO not -> allow / HOLD not -> confirm each FAILS.
      - observe ever blocking -> test_observe FAILS.
    """
    def test_mapping(self):
        self.assertEqual(W.WalletGuard(_fixed(_v("GO"))).check({}).action, W.ALLOW)
        self.assertEqual(W.WalletGuard(_fixed(_v("STOP"))).check({}).action, W.BLOCK)
        self.assertEqual(W.WalletGuard(_fixed(_v("HOLD"))).check({}).action, W.CONFIRM)

    def test_observe_never_blocks(self):
        g = W.WalletGuard(_fixed(_v("STOP")), mode=W.OBSERVE)
        self.assertEqual(g.check({}).action, W.ALLOW)


class TestGuardSign(unittest.TestCase):
    """Mutation notes: sign on BLOCK -> test_stop_withheld FAILS; sign a CONFIRM
    without approval -> test_hold FAILS."""

    def _guard(self, verdict, **kw):
        return W.WalletGuard(_fixed(verdict), **kw)

    def test_go_signs(self):
        r = self._guard(_v("GO")).guard_sign({}, lambda: "0xSIG")
        self.assertTrue(r.signed)
        self.assertEqual(r.signature, "0xSIG")

    def test_stop_withheld(self):
        called = []
        r = self._guard(_v("STOP")).guard_sign({}, lambda: called.append(1) or "x")
        self.assertFalse(r.signed)
        self.assertIsNone(r.signature)
        self.assertEqual(called, [])                    # signer never invoked

    def test_hold_requires_confirm(self):
        self.assertFalse(self._guard(_v("HOLD")).guard_sign({}, lambda: "x").signed)
        ok = self._guard(_v("HOLD"), confirm=lambda d: True)
        self.assertTrue(ok.guard_sign({}, lambda: "0xSIG").signed)
        no = self._guard(_v("HOLD"), confirm=lambda d: False)
        self.assertFalse(no.guard_sign({}, lambda: "x").signed)


class TestAvailabilityToggle(unittest.TestCase):
    """
    Mutation notes:
      - fail OPEN on engine error under enforce -> test_fail_closed FAILS.
      - ignore the toggle -> test_fail_open / test_runtime_toggle FAIL.
    """
    def _down(self):
        def d(payload):
            raise ConnectionError("engine down")
        return d

    def test_fail_closed_withholds(self):
        g = W.WalletGuard(self._down(), on_unreachable=W.FAIL_CLOSED)
        r = g.guard_sign({}, lambda: "x")
        self.assertFalse(r.signed)
        self.assertTrue(r.decision.degraded)

    def test_fail_open_signs(self):
        g = W.WalletGuard(self._down(), on_unreachable=W.FAIL_OPEN)
        self.assertTrue(g.guard_sign({}, lambda: "0xSIG").signed)

    def test_observe_ignores_toggle(self):
        g = W.WalletGuard(self._down(), mode=W.OBSERVE, on_unreachable=W.FAIL_CLOSED)
        self.assertTrue(g.guard_sign({}, lambda: "0xSIG").signed)

    def test_runtime_toggle(self):
        g = W.WalletGuard(self._down(), on_unreachable=W.FAIL_CLOSED)
        self.assertFalse(g.guard_sign({}, lambda: "x").signed)
        g.set_availability(W.FAIL_OPEN)
        self.assertTrue(g.guard_sign({}, lambda: "0xSIG").signed)
        with self.assertRaises(ValueError):
            g.set_availability("whatever")


class TestDescribePolicy(unittest.TestCase):
    """Mutation notes: wrong default -> test_control FAILS; missing the
    always-blocks note -> the reassurance is gone."""

    def test_single_policy(self):
        d = W.describe_policy(W.FAIL_CLOSED)
        self.assertEqual(d["value"], W.FAIL_CLOSED)
        self.assertEqual(d["label"], "Pause payments")
        for k in ("label", "tagline", "customer", "best_for"):
            self.assertTrue(d[k])
        self.assertIn("Keep paying", W.describe_policy(W.FAIL_OPEN)["label"])

    def test_control(self):
        c = W.describe_policy()
        self.assertEqual(c["default"], W.FAIL_CLOSED)
        self.assertEqual(len(c["options"]), 2)
        self.assertEqual({o["value"] for o in c["options"]},
                         {W.FAIL_CLOSED, W.FAIL_OPEN})
        self.assertIn("always blocked", c["note"])
        self.assertTrue(c["question"])

    def test_bad_policy_raises(self):
        with self.assertRaises(ValueError):
            W.describe_policy("nonsense")

    def test_guard_describes_current(self):
        g = W.WalletGuard(_fixed(_v("GO")), on_unreachable=W.FAIL_OPEN)
        self.assertEqual(g.describe_availability()["value"], W.FAIL_OPEN)
        g.set_availability(W.FAIL_CLOSED)
        self.assertEqual(g.describe_availability()["label"], "Pause payments")


class TestInProcessEndToEnd(unittest.TestCase):
    """Real forecast via a small reputation source (no network)."""
    class _Src:
        def lookup(self, cp):
            cp = cp.lower()
            if cp == BAD:
                return {"sanctioned": True, "settlement_count": 5}
            if cp == GOOD:
                return {"settlement_count": 1240, "confirmed_settlement_count": 1240,
                        "distinct_payers": 400, "dispute_rate": 0.002,
                        "price_history": ["0.09"] * 20}
            return {"settlement_count": 0}

    def _guard(self):
        return W.WalletGuard(W.in_process_decider(self._Src()))

    def test_good_signs_bad_withheld(self):
        g = self._guard()
        good = W.claim_from_tx({"to": USDC, "value": 0, "data": _transfer(GOOD, 90000)})
        bad = W.claim_from_tx({"to": USDC, "value": 0, "data": _transfer(BAD, 90000)})
        self.assertTrue(g.guard_sign(good, lambda: "0xSIG").signed)
        self.assertFalse(g.guard_sign(bad, lambda: "x").signed)

    def test_unlimited_approval_withheld(self):
        approve = "0x095ea7b3" + (bytes(12) + bytes.fromhex(BAD[2:])).hex() \
            + ((1 << 256) - 1).to_bytes(32, "big").hex()
        payload = W.claim_from_tx({"to": USDC, "value": 0, "data": approve})
        r = self._guard().guard_sign(payload, lambda: "x")
        self.assertFalse(r.signed)


if __name__ == "__main__":
    unittest.main()
