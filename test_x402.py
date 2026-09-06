"""
Unit tests for the x402 billing layer.

Run: python -m unittest test_x402.py -v

The pure protocol core (to_atomic / build_requirements / decode_payment_header /
payment_satisfies / session tokens) is tested first, then the BillingGate
orchestration with a mock facilitator. Each class notes the mutation it kills.
"""
import base64
import json
import os
import shutil
import subprocess
import unittest

import x402 as X

PAY_TO = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # a valid EVM address
USDC = X.BASE_USDC


def make_payment(to=PAY_TO, value="1000", scheme="exact", network="base",
                 nonce="0xnonce1", asset=None):
    # x402 v2 PaymentPayload: the chosen requirements go under `accepted`; the
    # EIP-3009 authorization nests under `payload.authorization` (unchanged).
    accepted = {"scheme": scheme, "network": X.to_caip2(network)}
    if asset is not None:
        accepted["asset"] = asset
    p = {"x402Version": 2, "accepted": accepted,
         "payload": {"authorization": {"from": "0x" + "a" * 40, "to": to,
                                       "value": value, "nonce": nonce,
                                       "validAfter": "0", "validBefore": "99999999999"},
                     "signature": "0xsig"}}
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


class TestRequirementsExtraDomain(unittest.TestCase):
    """The 402's `extra` must carry the asset EIP-712 domain so the facilitator
    can verify the EIP-3009 signature (else: missing_eip712_domain).

    Mutation notes:
      - omit `extra` for known assets -> test_known_assets FAILS.
      - emit a wrong domain -> test_domain_values FAILS.
    """
    def test_known_assets(self):
        r = X.build_requirements(1000, PAY_TO, "https://r", asset=X.BASE_SEPOLIA_USDC)
        self.assertEqual(r["extra"], {"name": "USDC", "version": "2"})
        r2 = X.build_requirements(1000, PAY_TO, "https://r", asset=X.BASE_USDC)
        self.assertEqual(r2["extra"], {"name": "USD Coin", "version": "2"})

    def test_unknown_asset_omits_extra(self):
        r = X.build_requirements(1000, PAY_TO, "https://r", asset="0x" + "9" * 40)
        self.assertNotIn("extra", r)


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

    def test_overpay_rejected_exact(self):
        # exact scheme (spec 6.1.2) requires value == amount; an overpay is
        # rejected here rather than dying at the facilitator. Mutation: accept
        # value >= required -> this FAILS.
        ok, reason = X.payment_satisfies(make_payment(value="5000"), self.req)
        self.assertFalse(ok)
        self.assertEqual(reason, "overpaid")

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
        self.assertEqual(r.body["accepts"][0]["amount"], "100000")


class TestPricingPolicy(unittest.TestCase):
    """
    Value-aligned fee = f(amount-at-risk).

    Mutation notes:
      - drop the free_below branch -> test_micro_is_free FAILS.
      - drop the max cap -> test_large_capped FAILS.
      - drop the min floor -> test_small_floored FAILS.
      - drop the proportionality cap -> test_fee_never_exceeds_a_share_of_the_amount
        FAILS.
    """
    def setUp(self):
        self.p = X.PricingPolicy(free_below="1.00", bps=10,
                                 min_fee="0.001", max_fee="0.10")

    def test_micro_is_free(self):
        self.assertEqual(self.p.fee_atomic("0.09"), 0)
        self.assertEqual(self.p.fee_atomic("1.00"), 0)  # at threshold -> free

    def test_proportional(self):
        # $50 * 0.1% = $0.05 -> 50000 atomic
        self.assertEqual(self.p.fee_atomic("50"), 50000)

    def test_large_capped(self):
        # $5000 * 0.1% = $5 -> capped at $0.10 = 100000 atomic
        self.assertEqual(self.p.fee_atomic("5000"), 100000)

    def test_small_floored(self):
        """The min floor binds when the proportional fee falls below it.

        NB the cap is explicitly DISABLED here. This case ($0.01 on $0.50) is a
        2% fee, which the proportionality invariant now refuses -- so with the
        cap on, this asserts the very behaviour the invariant exists to prevent.
        Disabling it keeps the test's original intent (does the floor bind?)
        while `test_fee_never_exceeds_a_share_of_the_amount` pins the new rule.
        """
        p = X.PricingPolicy(free_below="0.10", bps=10, min_fee="0.01",
                            max_fee="1.00", max_fee_ratio_bps=0)
        self.assertEqual(p.fee_atomic("0.50"), 10000)

    def test_fee_never_exceeds_a_share_of_the_amount(self):
        """THE PROPORTIONALITY INVARIANT.

        `min_fee` is an ABSOLUTE floor, so as the amount falls it becomes an
        ever-larger fraction of the payment. Nothing bounded that; it was hidden
        only because `free_below` sat above the range where the floor bites, which
        is why the paid tier was unreachable (0 of 265 live payees billable at the
        deployed $10.00).

        kills: removing the ratio check, or ignoring an operator's request to
        disable it. NOT killed by moving the check before quantization -- that
        mutant survives, and a sweep of ~184k amounts finds no input where
        rounding to the 1e-6 quantum crosses the bound. The post-quantization
        order is kept as defence for coarser constants, not because a failing
        case exists; saying otherwise would be a mutation note that cannot be
        demonstrated.
        """
        p = X.PricingPolicy(free_below="0.005", bps=10, min_fee="0.001",
                            max_fee="0.10", max_fee_ratio_bps=100)
        # $0.001 on $0.01 would be 10%; on $0.028, 3.6%. Both refused -> free.
        self.assertEqual(p.fee_atomic("0.01"), 0)
        self.assertEqual(p.fee_atomic("0.028"), 0)
        # At exactly 1% the fee stands -- the bound is a ceiling, not a strict <.
        self.assertEqual(p.fee_atomic("0.10"), 1000)
        # Above it, proportional pricing takes over normally.
        self.assertEqual(p.fee_atomic("1.00"), 1000)

    def test_cap_holds_across_the_whole_range(self):
        # PROPERTY, not an example: no amount may ever be charged above the cap.
        # kills: a cap that only fires in the band the examples happen to probe
        from decimal import Decimal
        p = X.PricingPolicy(free_below="0", bps=10, min_fee="0.0001",
                            max_fee="0.10", max_fee_ratio_bps=100)
        amt = Decimal("0.000001")
        while amt < Decimal("100000"):
            fee = Decimal(p.fee_atomic(amt)) / Decimal(10 ** 6)
            self.assertLessEqual(fee, amt * Decimal("0.01") + Decimal("0.0000005"),
                                 "fee %s exceeds 1%% of %s" % (fee, amt))
            amt *= Decimal("1.7")

    def test_bad_config_is_refused_at_boot_not_at_request_time(self):
        """AUDIT 2026-08-30. Every pricing constant arrives from an env var, and
        the policy used to accept values that failed later or not at all:

          * `nan` parsed as a valid Decimal on free_below / bps / min_fee, then
            raised InvalidOperation on EVERY priced request -- valid at boot,
            fatal at runtime, so the banner reported a healthy service that 500s
            the moment anyone is billed.
          * negatives passed silently, and a NEGATIVE max_fee_ratio_bps failed the
            `> 0` guard, SILENTLY DISABLING the proportionality invariant. Nobody
            writes -100 meaning "off".

        kills: dropping the finite/negative checks, or moving them out of the
        constructor so a bad value survives to request time.
        """
        for field in ("free_below", "bps", "min_fee", "max_fee",
                      "max_fee_ratio_bps"):
            for bad in ("nan", "inf", "-1", "abc", ""):
                kw = {"free_below": "0.01", "min_fee": "0.0001",
                      "max_fee_ratio_bps": 100}
                kw[field] = bad
                with self.assertRaises(ValueError, msg="%s=%r accepted" % (field, bad)):
                    X.PricingPolicy(**kw)

    def test_zero_is_still_a_legal_disable(self):
        # kills: over-tightening the validator so an operator cannot turn the cap
        # off deliberately -- 0 is documented and must stay legal
        X.PricingPolicy(max_fee_ratio_bps=0)
        X.PricingPolicy(free_below="0")

    def test_fee_is_monotonic_in_the_amount(self):
        """PROPERTY: paying more must never cost less.

        A non-monotonic curve would create an incentive to OVER-declare the
        amount, which is the opposite of the under-declaration the docstring
        already defends against.

        kills: a cap that returns 0 for a band above a charged band
        """
        from decimal import Decimal
        p = X.PricingPolicy(free_below="0.01", min_fee="0.0001",
                            max_fee_ratio_bps=100)
        amt, prev = Decimal("0.0001"), Decimal(-1)
        while amt < Decimal("100000"):
            fee = Decimal(p.fee_atomic(amt))
            self.assertGreaterEqual(fee, prev, "fee fell as the amount rose at %s" % amt)
            prev, amt = fee, amt * Decimal("1.09")

    def test_cap_can_be_disabled(self):
        # kills: hardcoding the cap so an operator cannot restore prior behaviour
        p = X.PricingPolicy(free_below="0.005", bps=10, min_fee="0.001",
                            max_fee="0.10", max_fee_ratio_bps=0)
        self.assertEqual(p.fee_atomic("0.01"), 1000)

    def test_unknown_amount_charges_floor(self):
        self.assertEqual(self.p.fee_atomic(None), 1000)
        self.assertEqual(self.p.fee_atomic("junk"), 1000)


class TestValueAlignedGate(unittest.TestCase):
    def setUp(self):
        self.gate = X.BillingGate(
            X.BillingConfig(pay_to=PAY_TO), facilitator=X.MockFacilitator(),
            pricing=X.PricingPolicy(free_below="1.00"))

    def test_micro_served_free(self):
        # a $0.09 forecast is free -> paid, no 402, no payment needed.
        r = self.gate.check("https://r", amount_at_risk="0.09")
        self.assertTrue(r.paid)
        self.assertEqual(r.via, "free")

    def test_large_gets_priced_402(self):
        r = self.gate.check("https://r", amount_at_risk="50")
        self.assertFalse(r.paid)
        self.assertEqual(r.body["accepts"][0]["amount"], "50000")

    def test_pay_the_value_aligned_price(self):
        r = self.gate.check("https://r", amount_at_risk="50",
                            x_payment=encode(make_payment(value="50000")))
        self.assertTrue(r.paid)

    def test_underpay_value_aligned_rejected(self):
        # pay the base price for a large forecast -> underpaid.
        r = self.gate.check("https://r", amount_at_risk="50",
                            x_payment=encode(make_payment(value="1000")))
        self.assertFalse(r.paid)
        self.assertEqual(r.body["error"], "underpaid")

    def test_free_does_not_consume_session(self):
        # a micro forecast with a session token stays free (no credit spent).
        store = self.gate.sessions
        tok = store.open(credits=5, ttl_seconds=100)
        r = self.gate.check("https://r", amount_at_risk="0.09", x_session=tok)
        self.assertEqual(r.via, "free")
        self.assertTrue(store.consume(tok)[0])  # all 5 still there
        self.assertEqual(store.consume(tok)[1], 3)  # 5 -> consumed 2 here -> 3


class TestV2WireFormat(unittest.TestCase):
    """x402 v2 wire-format conformance (specs/x402-specification-v2.md).

    Mutation notes:
      - X402_VERSION back to 1 -> test_version_is_2 FAILS.
      - emit `maxAmountRequired` instead of `amount` -> test_accept_uses_amount FAILS.
      - keep the bare network name -> test_network_is_caip2 FAILS.
      - drop the top-level ResourceInfo -> test_body_has_resource_info FAILS.
    """
    def test_version_is_2(self):
        self.assertEqual(X.X402_VERSION, 2)

    def test_caip2_mapping(self):
        self.assertEqual(X.to_caip2("base"), "eip155:8453")
        self.assertEqual(X.to_caip2("base-sepolia"), "eip155:84532")
        # already-CAIP-2 passes through; unknown bare name is left visible.
        self.assertEqual(X.to_caip2("eip155:8453"), "eip155:8453")
        self.assertEqual(X.to_caip2("weirdchain"), "weirdchain")

    def test_accept_uses_amount(self):
        r = X.build_requirements(1000, PAY_TO, "https://r", asset=USDC)
        self.assertEqual(r["amount"], "1000")
        self.assertNotIn("maxAmountRequired", r)

    def test_network_is_caip2(self):
        r = X.build_requirements(1000, PAY_TO, "https://r", network="base")
        self.assertEqual(r["network"], "eip155:8453")

    def test_accept_has_no_resource_fields(self):
        # v2 moves resource/description/mimeType OUT of each accept.
        r = X.build_requirements(1000, PAY_TO, "https://r")
        for k in ("resource", "description", "mimeType"):
            self.assertNotIn(k, r)

    def test_body_has_resource_info(self):
        info = X.build_resource_info("https://r", service_name="Blackwall")
        body = X.make_402_body([X.build_requirements(1000, PAY_TO)], resource=info)
        self.assertEqual(body["x402Version"], 2)
        self.assertEqual(body["resource"]["url"], "https://r")
        self.assertEqual(body["resource"]["serviceName"], "Blackwall")
        self.assertIn("accepts", body)
        self.assertIn("extensions", body)

    def test_resource_info_caps(self):
        # serviceName capped at 32 chars, tags capped at 5 entries / 32 chars.
        info = X.build_resource_info("https://r", service_name="x" * 50,
                                     tags=["a"] * 9)
        self.assertEqual(len(info["serviceName"]), 32)
        self.assertEqual(len(info["tags"]), 5)

    def test_v2_payment_satisfies(self):
        req = X.build_requirements(1000, PAY_TO, "https://r", asset=USDC, network="base")
        ok, reason = X.payment_satisfies(make_payment(value="1000"), req)
        self.assertTrue(ok, reason)

    def test_v2_gate_402_body_shape(self):
        gate = X.BillingGate(X.BillingConfig(price="0.001", pay_to=PAY_TO),
                             facilitator=X.MockFacilitator())
        r = gate.check("https://r")
        self.assertEqual(r.body["x402Version"], 2)
        self.assertEqual(r.body["resource"]["url"], "https://r")
        acc = r.body["accepts"][0]
        self.assertEqual(acc["amount"], "1000")
        self.assertEqual(acc["network"], "eip155:8453")
        self.assertEqual(acc["payTo"], PAY_TO)

    def test_402_body_has_bazaar_input_schema(self):
        # x402scan marks a 402 challenge non-invocable ("skipped") if it lacks an
        # input schema at extensions.bazaar.schema.properties.input.properties.body.
        # Mutation: drop the bazaar extension -> this FAILS -> endpoint skipped.
        gate = X.BillingGate(X.BillingConfig(price="0.001", pay_to=PAY_TO),
                             facilitator=X.MockFacilitator())
        body = gate.check("https://r").body
        inp = (body["extensions"]["bazaar"]["schema"]["properties"]
               ["input"]["properties"]["body"])
        self.assertEqual(inp["type"], "object")
        self.assertIn("counterparty", inp["properties"])

    def test_resource_url_capped(self):
        # The resource url is attacker-controlled and flows into the base64
        # PAYMENT-REQUIRED header; an unbounded url bloats the header. Mutation:
        # drop the url cap -> this FAILS (header/body oversize).
        info = X.build_resource_info("https://x/" + "a" * 100000)
        self.assertLessEqual(len(info["url"]), X.MAX_RESOURCE_URL)

    def test_build_bazaar_extension_shape(self):
        ext = X.build_bazaar_extension({"type": "object"}, {"verdict": "GO"})
        self.assertEqual(ext["bazaar"]["schema"]["properties"]["input"]
                         ["properties"]["body"], {"type": "object"})
        self.assertEqual(ext["bazaar"]["schema"]["properties"]["output"]
                         ["properties"]["example"], {"verdict": "GO"})

    def test_facilitator_envelope_is_v2(self):
        # The facilitator POST envelope must carry x402Version: 2.
        captured = {}

        class _CaptureFac(X.HttpFacilitator):
            def _post(self, path, payment, requirements):
                captured["path"] = path
                captured["body"] = {"x402Version": X.X402_VERSION,
                                    "paymentPayload": payment,
                                    "paymentRequirements": requirements}
                return {"isValid": True, "success": True, "transaction": "0xtx"}

        fac = _CaptureFac("http://unused")
        req = X.build_requirements(1000, PAY_TO, "https://r")
        fac.verify(make_payment(), req)
        self.assertEqual(captured["body"]["x402Version"], 2)


class TestBillingConfig(unittest.TestCase):
    def test_requires_valid_pay_to(self):
        with self.assertRaises(ValueError):
            X.BillingConfig(price="0.001", pay_to="0xNOPE")

    def test_rejects_bad_price(self):
        with self.assertRaises(ValueError):
            X.BillingConfig(price="0", pay_to=PAY_TO)


# Optional live conformance: run our generated 402 body through the REAL
# published x402scan validator (@agentcash/discovery). Skipped unless node + the
# installed validator project are present (set X402SCAN_VALIDATE_DIR to the dir
# holding a node project with @agentcash/discovery). This pins the wire format
# against ground truth, not our reading of the spec.
_VALIDATE_DIR = os.environ.get(
    "X402SCAN_VALIDATE_DIR",
    os.path.join(os.path.dirname(__file__), "..", "scratch-x402spec", "validate"))


class TestX402ScanConformance(unittest.TestCase):
    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node not available")
        if not os.path.isdir(os.path.join(_VALIDATE_DIR, "node_modules",
                                          "@agentcash", "discovery")):
            self.skipTest("@agentcash/discovery not installed in validate dir")

    def _validate(self, body):
        script = (
            "const D=require('@agentcash/discovery');"
            "let s='';process.stdin.on('data',c=>s+=c).on('end',()=>{"
            "const v=D.validatePaymentRequiredDetailed(JSON.parse(s));"
            "process.stdout.write(JSON.stringify(v.summary||{}));});")
        p = subprocess.run(["node", "-e", script], input=json.dumps(body),
                           capture_output=True, text=True, cwd=_VALIDATE_DIR,
                           timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_generated_402_body_passes_real_validator(self):
        gate = X.BillingGate(
            X.BillingConfig(price="0.05", pay_to=PAY_TO, network="base"),
            facilitator=X.MockFacilitator())
        body = gate.check("https://blackwall.example/v1/forecast-payment",
                          amount_at_risk="50").body
        summary = self._validate(body)
        # 0 errors == invocable + accepted by x402scan's v2 validator.
        self.assertEqual(summary.get("errorCount"), 0, summary)


class TestCdpFacilitator(unittest.TestCase):
    """The authenticated CDP facilitator adds a per-request Bearer JWT bound to
    the exact endpoint, and fails CLOSED when creds can't mint a token."""

    KEY_ID = "11111111-2222-3333-4444-555555555555"
    SECRET = base64.b64encode(bytes(range(64))).decode()

    def _uri_claim(self, header_value):
        token = header_value.split(" ", 1)[1]
        claims_b64 = token.split(".")[1]
        pad = claims_b64 + "=" * (-len(claims_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(pad))["uri"]

    def test_auth_header_binds_to_settle_and_verify_paths(self):
        # Kills a token reused across endpoints -- CDP rejects a JWT whose `uri`
        # claim doesn't match the exact METHOD+host+path of the call.
        fac = X.CdpFacilitator(self.KEY_ID, self.SECRET)
        h_settle = fac._auth_headers("/settle")
        h_verify = fac._auth_headers("/verify")
        self.assertTrue(h_settle["Authorization"].startswith("Bearer "))
        self.assertEqual(self._uri_claim(h_settle["Authorization"]),
                         "POST api.cdp.coinbase.com/platform/v2/x402/settle")
        self.assertEqual(self._uri_claim(h_verify["Authorization"]),
                         "POST api.cdp.coinbase.com/platform/v2/x402/verify")

    def test_default_base_url_is_cdp_mainnet(self):
        self.assertEqual(X.CdpFacilitator(self.KEY_ID, self.SECRET).base_url,
                         X.CDP_FACILITATOR_URL)

    def test_missing_secret_fails_closed(self):
        # Kills a path that would hit CDP unauthenticated (or crash): a bad/empty
        # secret must make verify/settle return valid/success False, not raise.
        fac = X.CdpFacilitator(self.KEY_ID, "")  # empty secret -> can't mint JWT
        req = X.build_requirements(1000, PAY_TO, "https://r")
        self.assertFalse(fac.verify(make_payment(), req)["valid"])
        self.assertFalse(fac.settle(make_payment(), req)["success"])


class TestChooseFacilitator(unittest.TestCase):
    SECRET = base64.b64encode(bytes(range(64))).decode()

    def test_cdp_creds_select_cdp_at_cdp_url(self):
        fac, note = X.choose_facilitator(None, "kid", self.SECRET)
        self.assertIsInstance(fac, X.CdpFacilitator)
        self.assertEqual(fac.base_url, X.CDP_FACILITATOR_URL)
        self.assertIn("Bazaar", note)

    def test_cdp_creds_ignore_stale_community_url(self):
        # THE FOOTGUN: a leftover community BLACKWALL_FACILITATOR must NOT receive
        # CDP-authenticated requests -- route to CDP and say so, don't misroute.
        fac, note = X.choose_facilitator("https://facilitator.x402.rs",
                                         "kid", self.SECRET)
        self.assertIsInstance(fac, X.CdpFacilitator)
        self.assertEqual(fac.base_url, X.CDP_FACILITATOR_URL)
        self.assertIn("IGNORING", note)

    def test_cdp_creds_honor_explicit_cdp_override(self):
        url = "https://api.cdp.coinbase.com/platform/v2/x402"
        fac, _ = X.choose_facilitator(url, "kid", self.SECRET)
        self.assertEqual(fac.base_url, url)

    def test_no_cdp_creds_uses_keyless_http(self):
        fac, note = X.choose_facilitator("https://facilitator.x402.rs", None, None)
        self.assertIsInstance(fac, X.HttpFacilitator)
        self.assertNotIsInstance(fac, X.CdpFacilitator)
        self.assertIn("NOT Bazaar", note)

    def test_nothing_configured_is_none(self):
        fac, _ = X.choose_facilitator(None, None, None)
        self.assertIsNone(fac)

    def test_partial_cdp_creds_do_not_select_cdp(self):
        # Only one of the pair set -> not CDP (would fail to mint a token).
        fac, _ = X.choose_facilitator("https://facilitator.x402.rs", "kid", None)
        self.assertNotIsInstance(fac, X.CdpFacilitator)

    def test_cdp_host_guard_rejects_lookalike_urls(self):
        # THE TOKEN-LEAK BUG: the "is this a CDP host?" test must be a real
        # hostname check, not a substring match. A stale/typo'd/hostile
        # BLACKWALL_FACILITATOR whose STRING merely CONTAINS "cdp.coinbase.com"
        # must NOT cause a CDP Bearer JWT to be minted for and sent to that host.
        # Each of these has a non-CDP registrable host -> route to canonical CDP.
        spoofs = [
            "https://cdp.coinbase.com.evil.com/x402",   # suffix attack
            "https://evil.com/?x=cdp.coinbase.com",     # substring in query
            "https://evil.com/cdp.coinbase.com/path",   # substring in path
            "http://cdp.coinbase.com@evil.com/x402",    # userinfo @ trick
            "https://notcdp.coinbase.com.attacker.net", # lookalike
        ]
        for url in spoofs:
            fac, _ = X.choose_facilitator(url, "kid", self.SECRET)
            # It stays a CdpFacilitator (creds are present) but must be pointed at
            # the CANONICAL CDP url -- never at the spoofed host.
            self.assertEqual(fac.base_url, X.CDP_FACILITATOR_URL,
                             "leaked CDP token target: %s" % url)

    def test_cdp_host_guard_honors_real_cdp_subdomains(self):
        # The strict check must still allow genuine CDP hosts (api./staging.).
        for url in ("https://api.cdp.coinbase.com/platform/v2/x402",
                    "https://staging.cdp.coinbase.com/x402"):
            fac, _ = X.choose_facilitator(url, "kid", self.SECRET)
            self.assertEqual(fac.base_url, url)


if __name__ == "__main__":
    unittest.main()


class TestFacilitatorTimeouts(unittest.TestCase):
    """`/verify` and `/settle` get DIFFERENT budgets, and the split is the point.

    AUDIT 2026-08-30, pre-billing. Measured against a black-hole facilitator, a
    paid request held a thread for the full 20s default. ThreadingHTTPServer is
    thread-per-request and unbounded, so a facilitator DEGRADATION (not an
    outage -- an outage fails fast at 0.07s) could starve the pool and take the
    FREE tier down alongside the paid one.

    The naive fix -- one shorter timeout -- is actively dangerous. `/settle` has a
    SIDE EFFECT: time out after the facilitator has broadcast and the on-chain
    EIP-3009 nonce is spent while we return a 402, so the agent has paid and got
    nothing, and every retry with that authorization fails forever. Waiting longer
    is the safe error there.
    """

    def test_defaults_are_split_and_settle_is_longer(self):
        # kills: collapsing both back to one value, or shortening settle
        f = X.HttpFacilitator("http://x")
        self.assertEqual(f.timeout, 8.0)
        self.assertGreater(f.settle_timeout, f.timeout)

    def test_cdp_inherits_the_split(self):
        # kills: fixing HttpFacilitator but leaving the CDP path on 20s -- CDP is
        # the facilitator that actually gets used in production
        f = X.CdpFacilitator("id", "secret")
        self.assertEqual(f.timeout, 8.0)
        self.assertEqual(f.settle_timeout, 25.0)

    def test_settle_actually_gets_the_longer_budget(self):
        """The wired-and-inert check: it is not enough for the attribute to
        exist, the REAL `_post` must select it for /settle.

        My first version of this test subclassed `_post` and reimplemented the
        budget selection inside the test -- so it asserted the test's own copy of
        the logic and passed under a mutant that made `_post` ignore
        settle_timeout entirely. It is replaced with one that drives the real
        `_post` and captures what urlopen is actually handed.

        kills: adding settle_timeout but never reading it in _post
        """
        import io as _io
        import urllib.request
        seen = {}
        real = urllib.request.urlopen

        class _Resp:
            def __enter__(self_):
                return _io.BytesIO(b'{"isValid":true,"success":true}')

            def __exit__(self_, *a):
                return False

        def fake(req, timeout=None):
            seen[req.full_url.rsplit("/", 1)[-1]] = timeout
            return _Resp()

        urllib.request.urlopen = fake
        try:
            f = X.HttpFacilitator("http://x", timeout=3.0, settle_timeout=9.0)
            f.verify({}, {})
            f.settle({}, {})
        finally:
            urllib.request.urlopen = real
        self.assertEqual(seen["verify"], 3.0)
        self.assertEqual(seen["settle"], 9.0)

    def test_choose_facilitator_threads_the_budgets_through(self):
        # kills: adding the parameters but constructing with the defaults anyway
        f, _ = X.choose_facilitator("http://x", None, None,
                                    timeout=2.0, settle_timeout=7.0)
        self.assertEqual(f.timeout, 2.0)
        self.assertEqual(f.settle_timeout, 7.0)


class TestFacilitatorTimeoutEnv(unittest.TestCase):
    def test_bad_timeout_is_refused_at_boot(self):
        # kills: dropping the validator so a bad value silently reverts to the
        # default -- a silently-ignored timeout is how a protection ends up not
        # applying while the banner reports health
        import blackwall
        import os
        for bad in ("abc", "nan", "inf", "0", "-1"):
            os.environ["BW_TEST_TIMEOUT"] = bad
            with self.assertRaises(ValueError, msg="%r accepted" % bad):
                blackwall._float_env("BW_TEST_TIMEOUT", 8.0)
        os.environ.pop("BW_TEST_TIMEOUT", None)

    def test_unset_and_empty_fall_back_to_the_default(self):
        # kills: treating an unset var as an error and refusing to boot normally
        import blackwall
        import os
        os.environ.pop("BW_TEST_TIMEOUT", None)
        self.assertEqual(blackwall._float_env("BW_TEST_TIMEOUT", 8.0), 8.0)
        os.environ["BW_TEST_TIMEOUT"] = ""
        self.assertEqual(blackwall._float_env("BW_TEST_TIMEOUT", 8.0), 8.0)
        os.environ.pop("BW_TEST_TIMEOUT", None)
