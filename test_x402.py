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
        # THIS ASSERTION USED TO READ `== "https://r"` -- i.e. it pinned the
        # caller's own absolute url being echoed into our 402. That was the
        # vulnerability, not the contract: the `resource` field is
        # client-supplied, so honouring its ORIGIN let a caller choose what our
        # 402 (and the Bazaar entry bound to our payTo) pointed at. Measured
        # live with https://evil.example/owned before the fix.
        # With no origin configured, only the PATH survives.
        self.assertEqual(r.body["resource"]["url"], "/")

    def test_a_configured_origin_makes_the_402_resource_ABSOLUTE(self):
        # The listing half: 2000/2000 catalogued Bazaar entries carry an
        # absolute url, and an indexer cannot invent our host from a path.
        gate = X.BillingGate(
            X.BillingConfig(price="0.001", pay_to=PAY_TO,
                            origin="https://blackwall-free.onrender.com"),
            facilitator=X.MockFacilitator())
        r = gate.check("/v1/forecast-payment")
        self.assertEqual(r.body["resource"]["url"],
                         "https://blackwall-free.onrender.com/v1/forecast-payment")

    def test_a_configured_origin_still_discards_a_hostile_one(self):
        gate = X.BillingGate(
            X.BillingConfig(price="0.001", pay_to=PAY_TO,
                            origin="https://blackwall-free.onrender.com"),
            facilitator=X.MockFacilitator())
        r = gate.check("https://evil.example/v1/forecast-payment")
        self.assertEqual(r.body["resource"]["url"],
                         "https://blackwall-free.onrender.com/v1/forecast-payment")
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

    def test_bazaar_info_matches_the_POST_shape_the_catalog_carries(self):
        # `extensions.bazaar.info` is present in 2000/2000 catalogued entries
        # while we emitted only `schema`. MEASURED on 100 live catalog entries
        # 2026-09-16, and the measurement CORRECTED our own notes: `queryParams`
        # is the GET form (80/100) and docs/BAZAAR_LISTING.md had summarised it
        # as the universal shape. The POST form (16/100), which is ours, is
        # {body, bodyType, method, type} with output {example, type}. Copying
        # the note would have advertised a POST endpoint with query params.
        # Mutation: emit `queryParams` instead of `body`/`bodyType` -> FAILS.
        ext = X.build_bazaar_extension({"type": "object"}, {"verdict": "GO"},
                                       input_example={"counterparty": "0x1"})
        info = ext["bazaar"]["info"]
        self.assertEqual(info["input"]["method"], "POST")
        self.assertEqual(info["input"]["type"], "http")
        self.assertEqual(info["input"]["bodyType"], "json")
        self.assertEqual(info["input"]["body"], {"counterparty": "0x1"})
        self.assertEqual(info["output"]["example"], {"verdict": "GO"})
        self.assertEqual(info["output"]["type"], "json")
        self.assertNotIn("queryParams", info["input"])

    def test_bazaar_info_is_absent_without_an_example(self):
        # Fail-quiet: no example -> no `info` block at all, rather than an
        # `info` advertising an empty body. Mutation: emit info unconditionally
        # -> FAILS. `schema` must still be emitted, so the existing listing
        # behaviour is unchanged for a caller that supplies no example.
        ext = X.build_bazaar_extension({"type": "object"}, {"verdict": "GO"})
        self.assertNotIn("info", ext["bazaar"])
        self.assertIn("schema", ext["bazaar"])

    def test_schema_block_is_untouched_by_info(self):
        # `schema` is what x402scan's validator reads to mark a resource
        # INVOCABLE; `info` is additive and must not disturb it. Mutation:
        # build info by MOVING the schema fields -> FAILS.
        ext = X.build_bazaar_extension({"type": "object"}, {"verdict": "GO"},
                                       input_example={"a": 1})
        self.assertEqual(ext["bazaar"]["schema"]["properties"]["input"]
                         ["properties"]["body"], {"type": "object"})
        self.assertEqual(ext["bazaar"]["schema"]["properties"]["output"]
                         ["properties"]["example"], {"verdict": "GO"})

    def test_the_advertised_example_is_one_our_own_engine_accepts(self):
        # A catalog entry is INVOCABLE -- an indexer may send exactly this body.
        # Our own docs use `0xKNOWNGOOD000...`, which `payee_syntax` grades
        # `invalid_hex`, so publishing that would advertise an example the
        # engine that answers it would flag. Mutation: put a placeholder that
        # is not a possible address in DEFAULT_FORECAST_INPUT_EXAMPLE -> FAILS.
        import payee_syntax
        ex = X.DEFAULT_FORECAST_INPUT_EXAMPLE
        for field in X.DEFAULT_FORECAST_INPUT_SCHEMA["required"]:
            self.assertIn(field, ex, "advertised example omits a REQUIRED field")
        grade = payee_syntax.assess_payee(ex["counterparty"])["grade"]
        self.assertNotIn(grade, ("malformed", "invalid_hex"),
                         "we would advertise a counterparty our own gate flags")

    def test_the_SERVED_402_carries_info_not_just_the_helper(self):
        # THE SEVENTH-EDIT HAZARD, caught by mutation on the very change that
        # introduced it: the three tests above call build_bazaar_extension
        # DIRECTLY with an input_example, so dropping `self.cfg.input_example`
        # at the one call site leaves `info` absent from the REAL 402 with every
        # one of them still green -- measured, it SURVIVED the first pass. The
        # property is about what a stranger receives, so it is asserted on the
        # body the gate actually serves, through the config default.
        # Mutation: drop the argument at the call site -> FAILS.
        cfg = X.BillingConfig(price="0.001", pay_to=PAY_TO)
        gate = X.BillingGate(cfg, facilitator=X.MockFacilitator(approve=True))
        body = gate.check("/v1/forecast-payment").body
        info = body["extensions"]["bazaar"]["info"]
        self.assertEqual(info["input"]["body"], X.DEFAULT_FORECAST_INPUT_EXAMPLE)
        self.assertEqual(info["input"]["method"], "POST")
        # and `schema` -- what marks the resource invocable -- is still there.
        self.assertIn("schema", body["extensions"]["bazaar"])

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


class TestCanonicalResourceUrl(unittest.TestCase):
    """The 402's `resource.url` must be OURS, never the caller's.

    FOUND while fixing the Bazaar listing, and it is the more serious half.
    `_challenge` passed the request's `resource` field into
    `build_resource_info` verbatim, and that field is CLIENT-SUPPLIED.
    MEASURED ON THE LIVE SERVICE before fixing -- every one of these came back
    inside a real 402 advertising our payTo:

        resource=https://evil.example/owned  -> url 'https://evil.example/owned'
        resource=javascript:alert(1)         -> url 'javascript:alert(1)'
        resource=//evil.example/x            -> url '//evil.example/x'

    Two consequences. (1) The 402 is the document CDP indexes into the Bazaar
    catalog, so an attacker could pay us 0.001 USDC with a foreign `resource`
    and have THEIR url catalogued against OUR payout address -- cheap, and it
    borrows our settlement history. (2) Our own url was RELATIVE
    ("/v1/forecast-payment"), which is why we are not indexed: an indexer cannot
    invent our host from a path.

    One change fixes both: the ORIGIN comes from our config and the PATH is all
    that is taken from the request.
    """

    ORIGIN = "https://blackwall-free.onrender.com"

    def test_a_client_supplied_ORIGIN_is_DISCARDED(self):
        # THE ATTACK. MUTATION: honouring an absolute request url. A caller
        # would then choose what our 402 -- and the Bazaar entry bound to our
        # payTo -- points at.
        for hostile in ("https://evil.example/owned",
                        "http://evil.example/v1/forecast-payment",
                        "//evil.example/protocol-relative",
                        "https://user:pw@evil.example/x"):
            got = X.canonical_resource_url(self.ORIGIN, hostile)
            self.assertTrue(got.startswith(self.ORIGIN + "/"),
                            "%r -> %r escaped our origin" % (hostile, got))
            self.assertNotIn("evil.example", got)

    def test_a_non_http_scheme_cannot_survive(self):
        # MUTATION: passing the scheme through. `javascript:` in a field that
        # gets rendered by a catalog UI is an XSS primitive we would be
        # publishing ourselves.
        for bad in ("javascript:alert(1)", "data:text/html,<script>",
                    "file:///etc/passwd"):
            got = X.canonical_resource_url(self.ORIGIN, bad)
            self.assertTrue(got.startswith(self.ORIGIN + "/"), got)
            for scheme in ("javascript", "data:", "file:"):
                self.assertNotIn(scheme, got.lower())

    def test_the_PATH_is_kept_because_it_identifies_the_priced_resource(self):
        # Different paths are different priced resources, so the path must
        # survive -- this is not a "replace everything with a constant" fix.
        self.assertEqual(X.canonical_resource_url(self.ORIGIN, "/v1/forecast-payment"),
                         self.ORIGIN + "/v1/forecast-payment")
        self.assertEqual(
            X.canonical_resource_url(self.ORIGIN, "https://evil.example/v1/x?a=b"),
            self.ORIGIN + "/v1/x?a=b")

    def test_a_relative_path_becomes_ABSOLUTE(self):
        # The listing half. MUTATION: leaving it relative -- measured 2000/2000
        # catalogued entries carry an absolute url.
        got = X.canonical_resource_url(self.ORIGIN, "v1/forecast-payment")
        self.assertEqual(got, self.ORIGIN + "/v1/forecast-payment")

    def test_PERCENT_ENCODED_separators_are_treated_as_separators(self):
        # FUZZ FINDING (low). `/..%2f..` and `/v1/x%2f..%2f..%2fetc` survived
        # normalization: %2f is not a literal "/", so segment splitting saw ONE
        # segment that merely CONTAINS "..", and the traversal text reached the
        # advertised url.
        #
        # NOT a host escape -- verified, every case stayed on our own origin, so
        # nothing could be redirected. The residual risk is a CONSUMER that
        # percent-decodes and then resolves, landing outside the path space we
        # serve. Cheap to close, so closed.
        # MUTATION: dropping the pre-decode of the encoded separators.
        # THE PROPERTY IS "NO TRAVERSAL SEGMENT", NOT "NO `..` SUBSTRING", and
        # the difference is real: `/a%255c..` is DOUBLE-encoded, so after the
        # single decode round `..` remains as literal TEXT inside a segment
        # (`a%5c..`) and resolves nowhere. Asserting the substring flagged that
        # as a failure -- the third time today an assertion was wrong rather
        # than the code, which is worth saying out loud.
        #
        # Decoding to a FIXED POINT would "fix" it and be worse: the number of
        # rounds would be the attacker's choice, and each round can synthesize
        # separators the previous one did not have.
        from urllib.parse import urlsplit
        for probe in ("/..%2f..", "/v1/x%2f..%2f..%2fetc", "/a%2F..%2F..%2Fb",
                      "/a%5c..%5c..", "/a%255c.."):
            got = X.canonical_resource_url(self.ORIGIN, probe)
            self.assertTrue(got.startswith(self.ORIGIN + "/"), got)
            segments = urlsplit(got).path.split("/")
            self.assertNotIn("..", segments,
                             "a traversal SEGMENT survived in %r -> %r"
                             % (probe, got))
            # AND no encoded separator may remain, or a consumer that decodes
            # once reconstitutes the traversal we just normalized away. Both
            # halves are needed: mutation testing showed the segment check
            # ALONE passes with the decode deleted, because `..%2f..` is one
            # segment that merely contains "..". Loosening an assertion to fix
            # a false positive can walk straight past the true one.
            for enc in ("%2f", "%2F", "%5c", "%5C"):
                self.assertNotIn(enc, got,
                                 "encoded separator %s survived in %r -> %r"
                                 % (enc, probe, got))

    def test_the_ORIGIN_is_checked_as_a_HOST_not_as_a_substring(self):
        # THIS TEST EXISTS BECAUSE MY OWN FUZZ ASSERTION WAS WRONG. It grepped
        # the output for "evil.example" and flagged `https:///evil.example/x`
        # and `\\evil.example\x` as host leaks -- but both land as a PATH on
        # our origin, which is correct and harmless. A substring check on a url
        # cannot tell a host from a path, and a wrong assertion is how a suite
        # grows a false sense of coverage.
        # The property is about netloc, so assert netloc.
        from urllib.parse import urlsplit
        mine = urlsplit(self.ORIGIN).netloc
        bs = chr(92)  # literal backslash, built from chr() so no escaping layer
        for probe in ("https:///evil.example/x",
                      bs + bs + "evil.example" + bs + "x",
                      "//evil.example/x", "https://evil.example:8080/x",
                      "http://user:pw@evil.example/x",
                      "https://evil.example" + bs + "@ours/x"):
            got = X.canonical_resource_url(self.ORIGIN, probe)
            self.assertEqual(urlsplit(got).netloc, mine,
                             "%r -> %r has netloc %r"
                             % (probe, got, urlsplit(got).netloc))

    def test_traversal_is_normalized_away(self):
        # MUTATION: naive concatenation. `..` segments would let a caller
        # advertise a url outside the path space we serve.
        for probe in ("/a/../../../etc/passwd", "../../secret", "/./x/../y"):
            got = X.canonical_resource_url(self.ORIGIN, probe)
            self.assertTrue(got.startswith(self.ORIGIN + "/"), got)
            self.assertNotIn("..", got)

    def test_NO_ORIGIN_configured_leaves_the_path_alone(self):
        # RESTRAINT CONTROL. An operator with no BLACKWALL_ORIGIN keeps today's
        # behaviour for their own path -- so this change cannot break a working
        # deploy -- but a client-supplied ORIGIN is still discarded, because that
        # was never legitimate.
        self.assertEqual(X.canonical_resource_url(None, "/v1/forecast-payment"),
                         "/v1/forecast-payment")
        self.assertEqual(X.canonical_resource_url("", "https://evil.example/x"),
                         "/x")

    def test_it_is_bounded(self):
        # MUTATION: dropping the cap. The url goes into a base64 response
        # header; an unbounded one produces a header proxies silently drop.
        got = X.canonical_resource_url(self.ORIGIN, "/" + "x" * 9000)
        self.assertLessEqual(len(got), X.MAX_RESOURCE_URL)

    def test_junk_never_raises(self):
        for junk in (None, 7, b"x", [], {}, "", "   "):
            got = X.canonical_resource_url(self.ORIGIN, junk)
            self.assertIsInstance(got, str)
            self.assertTrue(got.startswith(self.ORIGIN), got)

    def test_control_characters_are_stripped(self):
        # The url is echoed into a header and into a public catalog; a newline
        # would forge header structure. Same untrusted-echo class as
        # payee_syntax's hint and approvals' decided_by.
        # MUTATION TESTING CAUGHT THIS TEST, not the code: the string here was
        # written through a heredoc and contained LITERAL backslash-r-n rather
        # than real control characters, so removing the strip left it passing.
        # Built from chr() now so there is no escaping layer to get wrong.
        hostile = "/v1/x" + chr(13) + chr(10) + "X-Injected: 1"
        got = X.canonical_resource_url(self.ORIGIN, hostile)
        for ch in (chr(13), chr(10), chr(0), chr(9), " "):
            self.assertNotIn(ch, got, "control char %r survived" % ch)


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

    def test_half_set_cdp_creds_are_a_boot_ERROR_not_a_silent_fallback(self):
        # THE SILENT-CUTOVER BUG. This test previously asserted the OPPOSITE --
        # that one-of-the-pair "does not select CDP" -- which is true and is not
        # the point: it fell back to `facilitator_url`, and on mainnet that is a
        # keyless facilitator that settles perfectly well. So an operator who
        # pasted CDP_API_KEY_ID and fumbled the secret got a service that
        # settled real USDC through the OLD facilitator while they believed they
        # had cut over. A successful settlement is then indistinguishable from a
        # successful CUTOVER -- the failure mode is not "it doesn't work", it is
        # "it works and proves the wrong thing".
        #
        # Setting either variable states the operator's intent. Honouring half of
        # it is answering a different question from the one they asked. Same rule
        # receipt_signer.py already applies to a malformed BLACKWALL_SIGNING_SEED:
        # set-but-bad means they intended the feature, so fail LOUD.
        #
        # Mutation: `and` -> `or` in the guard, or dropping the raise entirely;
        # either restores the silent fallback and this test fails.
        for cid, secret in (("kid", None), (None, self.SECRET),
                            ("kid", ""), ("", self.SECRET)):
            with self.assertRaises(X.FacilitatorConfigError) as caught:
                X.choose_facilitator("https://facilitator.x402.rs", cid, secret)
            msg = str(caught.exception)
            # It must name BOTH variables and which one is missing -- an operator
            # reading a crash-looped deploy log has only this string.
            self.assertIn("CDP_API_KEY_ID", msg)
            self.assertIn("CDP_API_KEY_SECRET", msg)

    def test_the_error_names_the_variable_that_is_actually_missing(self):
        # Mutation: always naming the same side. Getting this backwards sends the
        # operator to re-paste the field that was already correct.
        with self.assertRaises(X.FacilitatorConfigError) as c:
            X.choose_facilitator(None, "kid", None)
        self.assertIn("CDP_API_KEY_SECRET is missing", str(c.exception))
        with self.assertRaises(X.FacilitatorConfigError) as c:
            X.choose_facilitator(None, None, self.SECRET)
        self.assertIn("CDP_API_KEY_ID is missing", str(c.exception))

    def test_neither_set_is_still_a_clean_keyless_fallback(self):
        # RESTRAINT CONTROL. The guard must fire ONLY on a HALF-set pair. An
        # operator running deliberately keyless has set neither, and that is a
        # supported configuration -- turning it into a boot failure would take
        # the free public deploy down.
        fac, note = X.choose_facilitator("https://facilitator.x402.rs", None, None)
        self.assertIsInstance(fac, X.HttpFacilitator)
        self.assertNotIsInstance(fac, X.CdpFacilitator)
        fac2, _ = X.choose_facilitator(None, None, None)
        self.assertIsNone(fac2)

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
