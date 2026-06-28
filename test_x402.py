"""
Unit tests for the x402 billing layer.

Run: python -m unittest test_x402.py -v

The pure protocol core (to_atomic / build_requirements / decode_payment_header /
payment_satisfies / session tokens) is tested first, then the BillingGate
orchestration with a mock facilitator. Each class notes the mutation it kills.
"""
import base64
import json
import unittest

import x402 as X

PAY_TO = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # a valid EVM address
USDC = X.BASE_USDC


def make_payment(to=PAY_TO, value="1000", scheme="exact", network="base",
                 nonce="0xnonce1", asset=None):
    p = {"x402Version": 1, "scheme": scheme, "network": network,
         "payload": {"authorization": {"to": to, "value": value, "nonce": nonce},
                     "signature": "0xsig"}}
    if asset is not None:
        p["asset"] = asset
    return p


def encode(payment):
    return base64.b64encode(json.dumps(payment).encode()).decode()


class TestToAtomic(unittest.TestCase):
    """
    Mutation notes:
      - drop the over-precision reject -> test_over_precision FAILS.
      - allow negatives -> test_negative FAILS.
    """
    def test_basic(self):
        self.assertEqual(X.to_atomic("0.001", 6), 1000)

    def test_integer(self):
        self.assertEqual(X.to_atomic("1", 6), 1_000_000)

    def test_over_precision(self):
        # more decimals than the asset supports -> reject, not silent round.
        self.assertIsNone(X.to_atomic("0.0000001", 6))

    def test_negative(self):
        self.assertIsNone(X.to_atomic("-1", 6))

    def test_junk(self):
        self.assertIsNone(X.to_atomic("free", 6))


class TestDecodeHeader(unittest.TestCase):
    def test_roundtrip(self):
        p = make_payment()
        self.assertEqual(X.decode_payment_header(encode(p)), p)

    def test_missing_padding_tolerated(self):
        raw = base64.b64encode(json.dumps(make_payment()).encode()).decode().rstrip("=")
        self.assertIsNotNone(X.decode_payment_header(raw))

    def test_not_base64(self):
        self.assertIsNone(X.decode_payment_header("!!!not base64!!!"))

    def test_not_json(self):
        self.assertIsNone(X.decode_payment_header(base64.b64encode(b"nope").decode()))

    def test_empty(self):
        self.assertIsNone(X.decode_payment_header(""))


class TestPaymentSatisfies(unittest.TestCase):
    """
    Mutation notes:
      - drop the payTo check   -> test_wrong_recipient FAILS.
      - use > instead of <     -> test_underpaid FAILS.
      - drop the network check -> test_wrong_network FAILS.
    """
    def setUp(self):
        self.req = X.build_requirements(1000, PAY_TO, "https://r", asset=USDC)

    def test_ok(self):
        ok, _ = X.payment_satisfies(make_payment(), self.req)
        self.assertTrue(ok)

    def test_exact_amount_ok(self):
        ok, _ = X.payment_satisfies(make_payment(value="1000"), self.req)
        self.assertTrue(ok)

    def test_overpay_ok(self):
        ok, _ = X.payment_satisfies(make_payment(value="5000"), self.req)
        self.assertTrue(ok)

    def test_underpaid(self):
        ok, reason = X.payment_satisfies(make_payment(value="999"), self.req)
        self.assertFalse(ok)
        self.assertEqual(reason, "underpaid")

    def test_wrong_recipient(self):
        ok, _ = X.payment_satisfies(make_payment(to="0x" + "1" * 40), self.req)
        self.assertFalse(ok)

    def test_recipient_case_insensitive(self):
        ok, _ = X.payment_satisfies(make_payment(to=PAY_TO.lower()), self.req)
        self.assertTrue(ok)

    def test_wrong_network(self):
        ok, _ = X.payment_satisfies(make_payment(network="ethereum"), self.req)
        self.assertFalse(ok)

    def test_wrong_asset(self):
        ok, _ = X.payment_satisfies(make_payment(asset="0x" + "2" * 40), self.req)
        self.assertFalse(ok)

    def test_missing_nonce_rejected(self):
        # A real EIP-3009 authorization always has a nonce; one without is
        # malformed and must not pass (else the local replay guard is bypassed).
        p = make_payment(nonce=None)
        del p["payload"]["authorization"]["nonce"]
        ok, reason = X.payment_satisfies(p, self.req)
        self.assertFalse(ok)
        self.assertIn("nonce", reason)

    def test_non_dict_payload_does_not_crash(self):
        # Crafted X-PAYMENT with a non-dict payload/authorization must return
        # cleanly, never raise (fail-closed, not fault).
        for bad in ({"scheme": "exact", "network": "base", "payload": ["x"]},
                    {"scheme": "exact", "network": "base",
                     "payload": {"authorization": "nope"}}):
            ok, _ = X.payment_satisfies(bad, self.req)
            self.assertFalse(ok)


class TestSessionToken(unittest.TestCase):
    def test_sign_verify_roundtrip(self):
        key = b"k"
        tok = X.sign_session_token("sid123", key)
        self.assertEqual(X.verify_session_token(tok, key), "sid123")

    def test_tampered_rejected(self):
        key = b"k"
        tok = X.sign_session_token("sid123", key)
        self.assertIsNone(X.verify_session_token(tok + "x", key))

    def test_wrong_key_rejected(self):
        tok = X.sign_session_token("sid", b"k1")
        self.assertIsNone(X.verify_session_token(tok, b"k2"))


class TestSessionStore(unittest.TestCase):
    def test_open_consume_until_exhausted(self):
        store = X.SessionStore(key=b"k")
        tok = store.open(credits=2, ttl_seconds=100, now=1000.0)
        ok, rem = store.consume(tok, now=1000.0)
        self.assertTrue(ok); self.assertEqual(rem, 1)
        ok, rem = store.consume(tok, now=1000.0)
        self.assertTrue(ok); self.assertEqual(rem, 0)
        ok, reason = store.consume(tok, now=1000.0)
        self.assertFalse(ok); self.assertEqual(reason, "session exhausted")

    def test_expiry(self):
        store = X.SessionStore(key=b"k")
        tok = store.open(credits=5, ttl_seconds=10, now=1000.0)
        ok, reason = store.consume(tok, now=2000.0)
        self.assertFalse(ok); self.assertEqual(reason, "session expired")

    def test_bad_token(self):
        store = X.SessionStore(key=b"k")
        ok, reason = store.consume("garbage", now=1000.0)
        self.assertFalse(ok)


class TestBillingGate(unittest.TestCase):
    def setUp(self):
        self.cfg = X.BillingConfig(price="0.001", pay_to=PAY_TO)
        self.gate = X.BillingGate(self.cfg, facilitator=X.MockFacilitator(approve=True))

    def test_no_payment_gets_402(self):
        r = self.gate.check("https://r")
        self.assertFalse(r.paid)
        self.assertEqual(r.status, 402)
        self.assertEqual(r.body["accepts"][0]["payTo"], PAY_TO)

    def test_valid_payment_paid(self):
        r = self.gate.check("https://r", x_payment=encode(make_payment(value="1000")))
        self.assertTrue(r.paid)
        self.assertEqual(r.via, "payment")

    def test_underpaid_gets_402_with_reason(self):
        r = self.gate.check("https://r", x_payment=encode(make_payment(value="1")))
        self.assertFalse(r.paid)
        self.assertEqual(r.body["error"], "underpaid")

    def test_replayed_payment_rejected(self):
        pay = encode(make_payment(nonce="0xreplay"))
        self.assertTrue(self.gate.check("https://r", x_payment=pay).paid)
        r2 = self.gate.check("https://r", x_payment=pay)
        self.assertFalse(r2.paid)
        self.assertIn("replay", r2.body["error"])

    def test_facilitator_rejection(self):
        gate = X.BillingGate(self.cfg,
                             facilitator=X.MockFacilitator(approve=False, reason="bad-sig"))
        r = gate.check("https://r", x_payment=encode(make_payment()))
        self.assertFalse(r.paid)
        self.assertIn("facilitator", r.body["error"])

    def test_paid_returns_settlement_tx(self):
        r = self.gate.check("https://r", x_payment=encode(make_payment()))
        self.assertTrue(r.paid)
        self.assertEqual(r.settlement, "0xmocktx")

    def test_settlement_failure_releases_nonce_for_retry(self):
        # verify ok but settle fails -> 402, and the nonce is NOT burned, so the
        # same payment can be retried once settlement is working again.
        gate = X.BillingGate(
            self.cfg, facilitator=X.MockFacilitator(approve=True, settle_ok=False))
        pay = encode(make_payment(nonce="0xretry"))
        r = gate.check("https://r", x_payment=pay)
        self.assertFalse(r.paid)
        self.assertIn("settlement failed", r.body["error"])
        # now settlement works; same payment must succeed (nonce was released).
        gate.facilitator.settle_ok = True
        r2 = gate.check("https://r", x_payment=pay)
        self.assertTrue(r2.paid)

    def test_session_open_then_spend(self):
        # open a session by paying the session price, then spend a credit.
        session_pay = encode(make_payment(value=str(self.cfg.session_price_atomic)))
        opened = self.gate.open_session("https://r", session_pay)
        self.assertTrue(opened.paid)
        token = opened.body["session_token"]
        r = self.gate.check("https://r", x_session=token)
        self.assertTrue(r.paid)
        self.assertEqual(r.via, "session")
        self.assertEqual(r.session_remaining, self.cfg.session_credits - 1)

    def test_bad_session_token_gets_402(self):
        r = self.gate.check("https://r", x_session="not.a.valid.token")
        self.assertFalse(r.paid)

    def test_402_advertises_price_override(self):
        # The unpaid 402 must quote the per-resource override price, not the base
        # price -- else the agent pays the quote and is rejected as underpaid.
        r = self.gate.check("https://r", price_atomic=100000)
        self.assertEqual(r.body["accepts"][0]["maxAmountRequired"], "100000")


class TestBillingConfig(unittest.TestCase):
    def test_requires_valid_pay_to(self):
        with self.assertRaises(ValueError):
            X.BillingConfig(price="0.001", pay_to="0xNOPE")

    def test_rejects_bad_price(self):
        with self.assertRaises(ValueError):
            X.BillingConfig(price="0", pay_to=PAY_TO)


if __name__ == "__main__":
    unittest.main()
