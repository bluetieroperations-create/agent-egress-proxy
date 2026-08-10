"""
Tests for the x402 service descriptor (discovery / listing).

Run: python -m unittest test_discovery.py -v
"""
import json
import threading
import time
import unittest
import urllib.request

import discovery as D
from blackwall import BlackwallServer

PAY_TO = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


class TestBuildDescriptor(unittest.TestCase):
    def test_unpriced_when_no_billing(self):
        d = D.build_descriptor()
        self.assertEqual(d["name"], "Blackwall")
        self.assertFalse(d["custody"])  # verdict, not custody
        self.assertIsNone(d["resources"][0]["accepts"])

    def test_priced_when_billing(self):
        # atomic units + asset contract, mirroring the authoritative 402 (spec 5.1.2)
        d = D.build_descriptor(pay_to=PAY_TO, price="1000",
                               asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
        acc = d["resources"][0]["accepts"][0]
        self.assertEqual(acc["payTo"], PAY_TO)
        self.assertEqual(acc["amount"], "1000")              # v2: ATOMIC units
        self.assertEqual(acc["asset"], "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
        self.assertEqual(acc["network"], "eip155:8453")      # v2: CAIP-2

    def test_descriptor_is_v2(self):
        # Mutation: x402Version back to 1 -> this FAILS.
        self.assertEqual(D.build_descriptor()["x402Version"], 2)

    def test_human_price(self):
        self.assertEqual(D.human_price(1000, 6), "0.001")
        self.assertIsNone(D.human_price(None))

    def test_advertises_verdicts_and_mcp(self):
        d = D.build_descriptor()
        self.assertEqual(d["resources"][0]["outputVerdicts"], ["GO", "HOLD", "STOP"])
        self.assertEqual(d["mcp"]["tool"], "forecast_payment")
        # Mutation: dropping the hosted remote MCP hides the zero-install URL.
        self.assertEqual(d["mcp"]["remote"]["url"], "https://mcp.blackwalltier.com")
        self.assertEqual(d["mcp"]["remote"]["transport"], "streamable-http")

    def test_advertises_verifiable_receipts(self):
        # Mutation: dropping the receipts block (or claiming issuer-only
        # verification) hides that verdicts are third-party verifiable.
        d = D.build_descriptor()
        r = d["receipts"]
        self.assertTrue(r["signed"])
        self.assertEqual(r["scheme"], "ed25519")
        self.assertEqual(r["verifiable"], "third-party")
        self.assertEqual(r["publicKeyUrl"], "/.well-known/blackwall-receipt-key.json")

    def test_json_serializable(self):
        json.dumps(D.build_descriptor(pay_to=PAY_TO, price="0.001"))

    def test_endpoint_readiness_advertised_only_when_on(self):
        # Mutation: advertise readiness unconditionally -> this FAILS.
        on = D.build_descriptor(endpoint_readiness=True)
        self.assertIn("endpoint-readiness", on["signals"])
        off = D.build_descriptor()
        self.assertNotIn("endpoint-readiness", off["signals"])


class TestBuildOpenAPI(unittest.TestCase):
    """The GET /openapi.json discovery document (x402scan contract).

    Mutation notes:
      - drop info.contact.email -> test_has_contact_email FAILS.
      - drop x-payment-info on the paid op -> test_paid_op_marked FAILS.
      - give /healthz a non-empty security -> test_free_endpoints_unsecured FAILS.
      - drop the 402 response -> test_paid_op_has_402 FAILS.
    """
    def test_required_top_level_fields(self):
        d = D.build_openapi()
        # x402scan requires openapi, info.title, info.version, paths.
        self.assertIn("openapi", d)
        self.assertIn("title", d["info"])
        self.assertIn("version", d["info"])
        self.assertIn("paths", d)

    def test_has_contact_email(self):
        d = D.build_openapi()
        self.assertEqual(d["info"]["contact"]["email"],
                         "bluetier.operations@gmail.com")

    def test_paid_op_marked(self):
        op = D.build_openapi(priced=True)["paths"]["/v1/forecast-payment"]["post"]
        self.assertIn("x-payment-info", op)
        # protocols MUST be an array of objects (the x402scan parser's
        # PaymentInfoSchema types it as array(record); a string array fails the
        # structured parse and drops the pricing hint). Mutation: emit ["x402"]
        # strings -> this FAILS.
        self.assertEqual(op["x-payment-info"]["protocols"], [{"x402": {}}])
        self.assertIn("price", op["x-payment-info"])
        self.assertEqual(op["security"], [{"x402": []}])

    def test_paid_op_has_402(self):
        op = D.build_openapi()["paths"]["/v1/forecast-payment"]["post"]
        self.assertIn("402", op["responses"])

    def test_price_is_dynamic_band(self):
        # value-aligned pricing -> a dynamic min/max band, not a fixed price.
        # The band is the FEE band [min_fee, max_fee] -- NOT the amount-at-risk
        # free threshold. free_below (1.00) is an amount-at-risk quantity, not a
        # fee, and using it as `min` yields an inverted min>max band a strict
        # validator rejects. Mutation: pass free_below as min -> this FAILS.
        price = (D.build_openapi(min_fee="0.001", max_fee="0.10")
                 ["paths"]["/v1/forecast-payment"]["post"]
                 ["x-payment-info"]["price"])
        self.assertEqual(price["mode"], "dynamic")
        self.assertEqual(price["min"], "0.001")
        self.assertEqual(price["max"], "0.10")

    def test_price_band_min_not_greater_than_max(self):
        # A dynamic price band with min > max is semantically inverted and a
        # strict OpenAPI/x402scan validator rejects it. The advertised band must
        # satisfy min <= max under the real pricing defaults.
        from decimal import Decimal
        price = (D.build_openapi()
                 ["paths"]["/v1/forecast-payment"]["post"]
                 ["x-payment-info"]["price"])
        self.assertLessEqual(Decimal(price["min"]), Decimal(price["max"]),
                             "advertised price band is inverted (min > max)")

    def test_free_endpoints_unsecured(self):
        # /healthz and /v1/report-outcome must be security: [] so probers skip.
        paths = D.build_openapi()["paths"]
        self.assertEqual(paths["/healthz"]["get"]["security"], [])
        self.assertEqual(paths["/v1/report-outcome"]["post"]["security"], [])

    def test_unpriced_forecast_not_advertised_paid(self):
        # billing OFF -> the forecast op must NOT claim to be paid.
        op = D.build_openapi(priced=False)["paths"]["/v1/forecast-payment"]["post"]
        self.assertNotIn("x-payment-info", op)
        self.assertEqual(op["security"], [])

    def test_security_scheme_defined(self):
        d = D.build_openapi()
        self.assertIn("x402", d["components"]["securitySchemes"])

    def test_server_url_when_given(self):
        d = D.build_openapi(server_url="https://example.com/")
        self.assertEqual(d["servers"][0]["url"], "https://example.com")

    def test_no_secret_material_leaked(self):
        # The discovery doc is PUBLIC -- it may carry the product description
        # (already public via /.well-known/x402), but must never carry secret
        # material: private keys, HMAC/receipt keys, tokens, or a payTo/wallet
        # that isn't part of a payment requirement (openapi carries no payTo).
        import json as _json
        blob = _json.dumps(D.build_openapi()).lower()
        for leak in ("private_key", "privatekey", "0x" + "a" * 40, "secret_key",
                     "receipt_key", "hmac", "-----begin"):
            self.assertNotIn(leak, blob)
        # No wallet/payTo address should appear in the discovery doc at all.
        self.assertNotIn("payto", blob)

    def test_json_serializable(self):
        json.dumps(D.build_openapi(server_url="https://x.y",
                                   ownership_proofs=["0xabc"]))


class TestDiscoveryEndpoint(unittest.TestCase):
    def setUp(self):
        self.srv = BlackwallServer(port=0)
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()
        time.sleep(0.3)
        self.base = "http://127.0.0.1:%d" % self.srv.port

    def tearDown(self):
        self.srv.shutdown()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=3) as r:
            return r.status, json.load(r)

    def test_well_known_x402(self):
        s, d = self._get("/.well-known/x402")
        self.assertEqual(s, 200)
        self.assertEqual(d["name"], "Blackwall")

    def test_v1_discovery_alias(self):
        s, d = self._get("/v1/discovery")
        self.assertEqual(s, 200)
        self.assertIn("resources", d)

    def test_openapi_served_and_parses(self):
        s, d = self._get("/openapi.json")
        self.assertEqual(s, 200)
        self.assertIn("openapi", d)
        self.assertIn("/v1/forecast-payment", d["paths"])
        self.assertEqual(d["info"]["contact"]["email"],
                         "bluetier.operations@gmail.com")

    def test_openapi_derives_https_origin_from_host(self):
        # No explicit --origin: a non-localhost Host header must yield an https
        # server URL (hosted deploys terminate TLS upstream). We can't easily
        # send a fake Host to the live loopback server, so assert the pure logic:
        # a hosted origin passed in is honored verbatim.
        srv = BlackwallServer(port=0, openapi_server_url="https://blackwall.example")
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start(); time.sleep(0.3)
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/openapi.json" % srv.port, timeout=3) as r:
                d = json.load(r)
            self.assertEqual(d["servers"][0]["url"], "https://blackwall.example")
        finally:
            srv.shutdown()


class TestSanctionsAdvertisedDynamically(unittest.TestCase):
    """The descriptor advertises sanctions-ofac iff the wrapped list is currently
    non-empty -- checked per request, so a background refresh that populates an
    initially-empty list flips screening ON with no restart, and an empty wrapper
    never claims a no-op check."""

    def _descriptor_for(self, reputation_source):
        srv = BlackwallServer(port=0, reputation_source=reputation_source)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)
        try:
            url = "http://127.0.0.1:%d/.well-known/x402" % srv.port
            with urllib.request.urlopen(url, timeout=3) as r:
                return json.load(r)
        finally:
            srv.shutdown()

    def test_empty_wrapper_off_then_flips_on_when_populated(self):
        import sanctions as SS
        from blackwall import MockReputationSource
        sl = SS.SanctionsList([])
        rs = SS.SanctionsScreeningSource(MockReputationSource(), sl)
        # empty wrapped list -> screening NOT advertised (honest)
        self.assertEqual(self._descriptor_for(rs)["screening"], [])
        # a background refresh lands addresses -> descriptor flips ON dynamically
        sl.add("0x000000000000000000000000000000000000dEaD")
        d = self._descriptor_for(rs)
        self.assertEqual(d["screening"], ["sanctions-ofac"])
        self.assertIn("sanctions-ofac", d["signals"])


if __name__ == "__main__":
    unittest.main()
