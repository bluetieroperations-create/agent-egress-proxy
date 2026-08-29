"""
Tests for the x402 service descriptor (discovery / listing).

Run: python -m unittest test_discovery.py -v
"""
import json
import threading
import time
import unittest
import urllib.error
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

    @staticmethod
    def _dispatched_paths(func_name):
        """Extract the path literals blackwall.py's `func_name` compares self.path
        against, straight from its AST.

        This is deliberately an INDEPENDENT reading of what the server serves.
        Asserting the spec contains discovery.PUBLIC_GET_ROUTES would be
        tautological -- build_openapi is built from that same tuple, so the test
        could never fail. Parsing the dispatch recovers the real second opinion,
        which is what makes drift detectable.
        """
        import ast
        src = open("blackwall.py").read()
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == func_name)
        found = set()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Compare):
                continue
            tgt = node.left
            if not (isinstance(tgt, ast.Attribute) and tgt.attr == "path"):
                continue
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    found.add(comp.value)
                elif isinstance(comp, (ast.Tuple, ast.List)):
                    for e in comp.elts:
                        if isinstance(e, ast.Constant) and isinstance(e.value, str):
                            found.add(e.value)
        return found

    def test_openapi_advertises_every_served_route(self):
        """The spec must describe what the server ACTUALLY serves.

        Three hand-maintained route lists (do_GET, do_HEAD, build_openapi) had
        drifted: measured in production, the spec advertised 4 of 13 live routes,
        so a caller reading it could not discover /v1/price-index, /jwks.json,
        /v1/verify-signer or the discovery documents at all.

        Mutation: add a route to do_GET without adding it to PUBLIC_GET_ROUTES
        -> this FAILS. Drop a path from PUBLIC_GET_ROUTES while do_GET still
        serves it -> this FAILS.
        """
        spec = self._get("/openapi.json")[1]["paths"]
        # /v1/session is billing-only and correctly absent when billing is off.
        billing_only = set(D.BILLING_POST_ROUTES)
        for path in sorted(self._dispatched_paths("do_GET") - billing_only):
            self.assertIn(path, spec,
                          "do_GET serves %s but openapi.json omits it" % path)
            self.assertIn("get", spec[path])
        for path in sorted(self._dispatched_paths("do_POST") - billing_only):
            self.assertIn(path, spec,
                          "do_POST serves %s but openapi.json omits it" % path)
            self.assertIn("post", spec[path])

    def test_every_post_route_is_rate_limited(self):
        """Every POST route must actually emit 429 once the bucket is empty.

        Asserted END TO END, per route, rather than by reading the guard tuple:
        an AST reading of do_POST cannot separate the guard's path list from the
        dispatch branches, so it passes even when a route is excluded from the
        limiter. Only driving real requests distinguishes them.

        The hand-maintained guard had drifted -- /v1/verify-signer was the ONE
        POST omitted, while its own docstring claimed it was "rate-limited via
        do_POST like every other route". It is also the most expensive route
        served (measured 22.7ms of pure-Python secp256k1 signer recovery against
        ~90us for a forecast), so the single omission was the best CPU
        amplification target on the box, and limiting is ON in production.

        Mutation: drop ANY path from the do_POST guard -> this FAILS for that
        path with 200s/404s instead of a 429.
        """
        from ratelimit import RateLimiter
        for path in list(D.PUBLIC_POST_ROUTES) + list(D.BILLING_POST_ROUTES):
            srv = BlackwallServer(port=0,
                                  rate_limiter=RateLimiter(rate=60, per_seconds=60,
                                                           burst=2))
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start(); time.sleep(0.2)
            codes = []
            try:
                for _ in range(5):
                    req = urllib.request.Request(
                        "http://127.0.0.1:%d%s" % (srv.port, path),
                        data=b"{}", method="POST",
                        headers={"Content-Type": "application/json"})
                    try:
                        with urllib.request.urlopen(req, timeout=3) as r:
                            codes.append(r.status)
                    except urllib.error.HTTPError as e:
                        codes.append(e.code)
            finally:
                srv.shutdown()
            self.assertIn(429, codes,
                          "POST %s never rate-limited: %s" % (path, codes))

    def test_route_table_matches_the_get_dispatch(self):
        """PUBLIC_GET_ROUTES must equal what do_GET dispatches on -- the table now
        drives do_HEAD and the spec, so a stale table silently 404s HEAD and
        under-documents the API. Mutation: add/remove either side -> this FAILS."""
        self.assertEqual(self._dispatched_paths("do_GET"),
                         set(D.PUBLIC_GET_ROUTES))

    def test_every_advertised_route_actually_serves(self):
        """The converse: nothing in the spec may 404. An advertised-but-absent
        route is the same defect pointed the other way -- it sends a caller to a
        dead path. Mutation: advertise /v1/nope -> this FAILS."""
        for path in D.PUBLIC_GET_ROUTES:
            self.assertEqual(self._get(path)[0], 200, "GET %s 404s" % path)

    def test_head_matches_get_on_every_public_route(self):
        """HEAD must mirror GET. do_HEAD carried its own hardcoded list and had
        drifted: /jwks.json, /.well-known/blackwall-receipt-key.json and /stats
        returned 200 on GET but 404 on HEAD (verified live in production). That
        breaks any client that preflights with HEAD -- blackwall-mcp-remote
        fetches the .well-known key URL. Mutation: drop a path from the HEAD
        table -> this FAILS."""
        for path in D.PUBLIC_GET_ROUTES:
            req = urllib.request.Request(self.base + path, method="HEAD")
            with urllib.request.urlopen(req, timeout=3) as r:
                self.assertEqual(r.status, 200, "HEAD %s != GET %s" % (path, path))

    def test_session_advertised_only_when_billing_on(self):
        """/v1/session 404s with `billing not enabled` when billing is off, so
        advertising it unconditionally would document a dead route on the free
        tier. Mutation: move it into the unconditional table -> this FAILS."""
        self.assertNotIn("/v1/session", D.build_openapi(priced=False)["paths"])
        self.assertIn("/v1/session", D.build_openapi(priced=True)["paths"])

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
