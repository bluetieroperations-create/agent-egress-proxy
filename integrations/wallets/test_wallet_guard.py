"""
Tests for W.py -- the provider-agnostic signing gate. Stdlib only.
Run from this directory:  python -m unittest test_W.py
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


class TestCustomerMessage(unittest.TestCase):
    """
    Mutation notes:
      - leak internal signals into the reason -> test_block_reason_is_clean FAILS.
      - not label actions in plain words -> test_status_labels FAILS.
      - not handle the degraded case -> test_degraded FAILS.
    """
    def _decide(self, verdict):
        return W.WalletGuard(_fixed(verdict)).check({})

    def test_status_labels(self):
        self.assertEqual(W.customer_message(self._decide(_v("GO")))["status"], "Allowed")
        self.assertEqual(W.customer_message(self._decide(_v("HOLD")))["status"],
                         "Needs your approval")
        self.assertEqual(W.customer_message(self._decide(_v("STOP")))["status"], "Blocked")

    def test_block_reason_is_clean(self):
        d = self._decide(_v("STOP", reasons=[
            "counterparty is on a sanctions list",
            "counterparty has 80 prior settlements, 1.0% dispute rate"]))
        msg = W.customer_message(d)
        self.assertEqual(msg["reason"], "counterparty is on a sanctions list")
        # the internal signal line must NOT appear
        self.assertNotIn("prior settlements", msg["detail"])
        self.assertNotIn("atomic", msg["detail"])

    def test_go_has_no_reason(self):
        msg = W.customer_message(self._decide(_v("GO")))
        self.assertIsNone(msg["reason"])
        self.assertIn("safe", msg["headline"].lower())

    def test_degraded_paused(self):
        g = W.WalletGuard(lambda p: (_ for _ in ()).throw(ConnectionError("x")),
                          on_unreachable=W.FAIL_CLOSED)
        msg = W.customer_message(g.check({}))
        self.assertEqual(msg["status"], "Blocked")
        self.assertIn("couldn't complete", msg["headline"].lower())
        self.assertIsNone(msg["reason"])          # no internal error leaked

    def test_degraded_allowed(self):
        g = W.WalletGuard(lambda p: (_ for _ in ()).throw(ConnectionError("x")),
                          on_unreachable=W.FAIL_OPEN)
        msg = W.customer_message(g.check({}))
        self.assertEqual(msg["status"], "Allowed")
        self.assertIn("without it", msg["headline"].lower())


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


class TestDecimalsAudit(unittest.TestCase):
    """Audit 2026-08-27: claim_from_tx assumed 6 decimals for EVERY token."""

    RECIP = "0x" + "2" * 40
    GUSD = "0x056Fd409E1d7A124BD7017459dFEa2F387b6d5Cd"   # 2 decimals
    DAI = "0x6B175474E89094C44Da98b954EedeAC495271d0F"    # 18 decimals
    USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"   # 6 decimals

    def _tx(self, token, atomic):
        return {"to": token, "value": 0,
                "data": "0xa9059cbb" + self.RECIP[2:].rjust(64, "0")
                        + hex(atomic)[2:].rjust(64, "0")}

    def test_sub_six_decimal_transfer_is_not_shrunk(self):
        # kills: the 6-decimal assumption. A 50,000 GUSD transfer (2dp) was
        # reported as "5" and sailed under a hold_above=100 spending cap --
        # a cap bypass in the SIGNING path.
        c = W.claim_from_tx(self._tx(self.GUSD, 50000 * 10 ** 2))
        self.assertEqual(c["amount"], "50000")

    def test_eighteen_decimal_transfer_is_not_inflated(self):
        # kills: the mirror failure -- 1,000 DAI reported as 10^15, an absurd
        # amount that false-STOPs a legitimate payment
        c = W.claim_from_tx(self._tx(self.DAI, 1000 * 10 ** 18))
        self.assertEqual(c["amount"], "1000")

    def test_usdc_unchanged(self):
        # kills: a fix that regresses the common case
        c = W.claim_from_tx(self._tx(self.USDC, 25 * 10 ** 6))
        self.assertEqual(c["amount"], "25")

    def test_unknown_token_transfer_is_flagged(self):
        # kills: reusing the "no value moved" placeholder for a REAL transfer we
        # could not scale -- that presents any size transfer as 0.000001
        c = W.claim_from_tx(self._tx("0x" + "9" * 40, 10 ** 18))
        self.assertTrue(c["amount_unverified"])

    def test_approve_is_not_flagged(self):
        # kills: flagging every non-value call, which would force a human confirm
        # on approvals and make the guard unusable
        appr = {"to": "0x" + "9" * 40, "value": 0,
                "data": "0x095ea7b3" + self.RECIP[2:].rjust(64, "0") + "f" * 64}
        self.assertNotIn("amount_unverified", W.claim_from_tx(appr))

    def test_go_on_unverified_amount_forces_confirm(self):
        # kills: allowing a GO earned on a placeholder amount -- the spending-cap
        # and price gates never saw the real sum
        g = W.WalletGuard(lambda p: {"verdict": "GO", "reasons": []},
                                     mode=W.ENFORCE)
        c = W.claim_from_tx(self._tx("0x" + "9" * 40, 10 ** 18))
        self.assertEqual(g.check(c).action, W.CONFIRM)

    def test_signature_is_withheld_without_confirmation(self):
        # kills: the flag being advisory only -- it must actually stop the signing
        g = W.WalletGuard(lambda p: {"verdict": "GO", "reasons": []},
                                     mode=W.ENFORCE)
        c = W.claim_from_tx(self._tx("0x" + "9" * 40, 10 ** 18))
        self.assertFalse(g.guard_sign(c, lambda: "0xSIG").signed)

    def test_normal_go_still_allows(self):
        # kills: forcing confirm on everything, which would break the happy path
        g = W.WalletGuard(lambda p: {"verdict": "GO", "reasons": []},
                                     mode=W.ENFORCE)
        self.assertEqual(g.check({"amount": "1"}).action, W.ALLOW)
