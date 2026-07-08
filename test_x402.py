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
        # With a low free threshold the min floor binds: $0.50 * 0.1% = $0.0005
        # -> floored to min $0.01 = 10000 atomic.
        p = X.PricingPolicy(free_below="0.10", bps=10, min_fee="0.01", max_fee="1.00")
        self.assertEqual(p.fee_atomic("0.50"), 10000)

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
        # x402scan schema format (unchanged).
        ext = X.build_bazaar_extension({"type": "object"}, {"verdict": "GO"})
        self.assertEqual(ext["bazaar"]["schema"]["properties"]["input"]
                         ["properties"]["body"], {"type": "object"})
        self.assertEqual(ext["bazaar"]["schema"]["properties"]["output"]
                         ["properties"]["example"], {"verdict": "GO"})

    def test_build_bazaar_extension_cdp_info(self):
        # Mutation: dropping the CDP `info` block -> CDP Bazaar never catalogs us
        # (the whole reason a settled endpoint stayed unlisted). With a concrete
        # input_example, emit the declareDiscoveryExtension shape CDP validates.
        ext = X.build_bazaar_extension(
            {"type": "object"}, {"verdict": "GO"},
            {"counterparty": "0xabc", "amount": "2.50", "asset": "USDC", "chain": "base"})
        info = ext["bazaar"]["info"]
        self.assertEqual(info["input"]["method"], "POST")
        self.assertEqual(info["input"]["bodyType"], "json")
        self.assertEqual(info["input"]["type"], "http")
        self.assertEqual(info["input"]["body"]["asset"], "USDC")   # concrete example, not a schema
        self.assertEqual(info["output"]["example"], {"verdict": "GO"})
        # And the x402scan schema is still there (both catalogs satisfied).
        self.assertIn("schema", ext["bazaar"])

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
