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


class TestRoutePath(unittest.TestCase):
    """Query-string normalization for GET/HEAD route matching.

    self.path carries the query string, and matching is exact, so a probe with a
    cache-buster (/openapi.json?v=2) read as 404 -- a monitor or discovery
    crawler would score our discovery documents DEAD. This repo has already been
    burned by the same rigidity pointed outward: directory_liveness counted 405s
    as dead endpoints and undercounted live hosts by 14.

    Mutation notes:
      - return the raw path unchanged -> test_strips_query FAILS.
      - split on the LAST '?' -> test_strips_query_with_embedded_marker FAILS.
      - strip a trailing slash too -> test_does_not_touch_trailing_slash FAILS.
    """
    def test_bare_path_unchanged(self):
        self.assertEqual(D.route_path("/healthz"), "/healthz")

    def test_strips_query(self):
        self.assertEqual(D.route_path("/openapi.json?v=2"), "/openapi.json")
        self.assertEqual(D.route_path("/v1/price-index?a=1&b=2"), "/v1/price-index")

    def test_strips_bare_marker(self):
        self.assertEqual(D.route_path("/healthz?"), "/healthz")

    def test_strips_query_with_embedded_marker(self):
        # split on the FIRST '?': everything after it is the query, even another '?'
        self.assertEqual(D.route_path("/healthz?a=1?b=2"), "/healthz")

    def test_does_not_touch_trailing_slash(self):
        # deliberately out of scope -- /healthz/ is a different question with its
        # own blast radius, and widening routing was not the job.
        self.assertEqual(D.route_path("/healthz/"), "/healthz/")

    def test_query_only_and_junk(self):
        self.assertEqual(D.route_path("?x=1"), "")
        self.assertEqual(D.route_path(""), "")
        self.assertEqual(D.route_path(None), "")
        self.assertEqual(D.route_path(123), "")


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
            # do_GET/do_HEAD route on a normalized LOCAL (`path = route_path(
            # self.path)`, query string stripped); do_POST still compares
            # `self.path` directly. Accept both spellings, and ONLY those two --
            # matching any name would let an unrelated comparison masquerade as
            # a route and quietly weaken every assertion built on this.
            is_self_path = isinstance(tgt, ast.Attribute) and tgt.attr == "path"
            is_local_path = isinstance(tgt, ast.Name) and tgt.id == "path"
            if not (is_self_path or is_local_path):
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
        # /stats and anything else in UNADVERTISED_ROUTES is served on purpose
        # without being catalogued -- see test_unadvertised_routes_*.
        billing_only = set(D.BILLING_POST_ROUTES) | set(D.UNADVERTISED_ROUTES)
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

    def test_get_and_head_tolerate_a_query_string(self):
        """A cache-buster must not read as a dead endpoint. Measured live before
        the fix: every GET route 404'd with any query string attached, so a
        monitor or discovery crawler probing /openapi.json?v=2 scored our
        discovery documents dead. Mutation: route on the raw self.path -> FAILS."""
        for path in D.PUBLIC_GET_ROUTES:
            for q in ("?v=2", "?cb=123&x=1", "?"):
                self.assertEqual(self._get(path + q)[0], 200,
                                 "GET %s%s should serve" % (path, q))
                req = urllib.request.Request(self.base + path + q, method="HEAD")
                with urllib.request.urlopen(req, timeout=3) as r:
                    self.assertEqual(r.status, 200,
                                     "HEAD %s%s should serve" % (path, q))

    def test_query_string_does_not_invent_routes(self):
        """Stripping the query must not make unknown paths resolve. Mutation:
        return "" or a prefix from route_path -> this FAILS with 200s."""
        for bad in ("/v1/nope?x=1", "/nope", "/healthz-not-really?cb=1",
                    "/?x=1", "/v1/price-index-extra?a=1"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self._get(bad)
            self.assertEqual(cm.exception.code, 404, "%s should 404" % bad)

    def _raw_status(self, request_target):
        """Send a request line VERBATIM over a socket. urllib normalizes the URL
        client-side (it strips fragments and trailing spaces), so a test that
        goes through urllib measures urllib, not our routing."""
        import socket
        s = socket.create_connection(("127.0.0.1", self.srv.port), 3)
        try:
            s.sendall(("GET %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
                       % request_target).encode())
            buf = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        finally:
            s.close()
        return int(buf.split(b"\r\n")[0].split()[1])

    def test_path_matching_stays_exact_beyond_the_query_string(self):
        """Stripping the query must not widen routing in any OTHER direction.

        Checked over a raw socket because urllib rewrites the URL before it is
        sent. Mutation: casefold the path, or rstrip('/'), or split on '#' in
        route_path -> the corresponding case FAILS.
        """
        self.assertEqual(self._raw_status("/healthz"), 200)      # control
        self.assertEqual(self._raw_status("/HEALTHZ"), 404)      # not case-insensitive
        self.assertEqual(self._raw_status("/healthz/"), 404)     # no trailing-slash widening
        self.assertEqual(self._raw_status("/healthz#frag"), 404)  # '#' is not a delimiter here
        self.assertEqual(self._raw_status("/healthz-not-really"), 404)

    def test_leading_double_slash_is_collapsed_by_the_stdlib(self):
        """`//healthz` serves 200, and that is NOT our code.

        http.server's parse_request collapses a leading '//' to '/' before any
        handler runs, because HTTP clients read `//path` as a scheme-relative
        URI. Pinned deliberately: it surprised us during the audit, and it is
        path-normalization behavior worth noticing if a future Python changes
        it. It normalizes TOWARD the canonical path, so it cannot reach a route
        that is absent from the table -- `//nope` still 404s.
        """
        self.assertEqual(self._raw_status("//healthz"), 200)
        self.assertEqual(self._raw_status("///healthz"), 200)
        self.assertEqual(self._raw_status("//nope"), 404)

    def test_get_handler_failure_returns_503_not_a_dropped_connection(self):
        """A raising GET handler must answer cleanly, like do_POST already does.

        do_POST wraps its dispatch and returns 503 ("fail CLOSED + gracefully").
        do_GET had NO try/except at all, so an exception dropped the connection
        mid-response: the client saw RemoteDisconnected rather than any status
        code, and the traceback went to stderr. Reproduced with a receipt_signer
        whose jwks() raises -- a plausible misconfiguration, and /jwks.json is
        now advertised in openapi.json so crawlers reach it.

        Mutation: remove the try/except from do_GET -> this FAILS with a
        connection error instead of 503.
        """
        class BoomSigner:
            def jwks(self):
                raise RuntimeError("signer exploded")
        srv = BlackwallServer(port=0, receipt_signer=BoomSigner())
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start(); time.sleep(0.3)
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/jwks.json" % srv.port, timeout=4)
            self.assertEqual(cm.exception.code, 503)
        finally:
            srv.shutdown()

    def test_get_handler_failure_does_not_leak_internals(self):
        """The 503 body must not carry the exception message or a traceback --
        a GET route is unauthenticated. Mutation: put str(e) in the body ->
        this FAILS."""
        class BoomSigner:
            def jwks(self):
                raise RuntimeError("secret-internal-detail-9f3a")
        srv = BlackwallServer(port=0, receipt_signer=BoomSigner())
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start(); time.sleep(0.3)
        try:
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/jwks.json" % srv.port, timeout=4)
                self.fail("expected an error status")
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
            self.assertNotIn("secret-internal-detail-9f3a", body)
            self.assertNotIn("Traceback", body)
            self.assertNotIn("RuntimeError", body)
        finally:
            srv.shutdown()

    def test_post_routes_stay_strictly_matched(self):
        """POST is deliberately NOT normalized: those routes carry payments,
        self.path feeds the x402 billing RESOURCE key, and 404ing before any
        handler runs is fail-closed. Mutation: normalize in do_POST too -> this
        FAILS, and that change needs its own audit, not a tack-on."""
        req = urllib.request.Request(
            self.base + "/v1/forecast-payment?x=1", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(cm.exception.code, 404)

    def test_route_table_matches_the_get_dispatch(self):
        """PUBLIC_GET_ROUTES must equal what do_GET dispatches on -- the table now
        drives do_HEAD and the spec, so a stale table silently 404s HEAD and
        under-documents the API. Mutation: add/remove either side -> this FAILS."""
        self.assertEqual(self._dispatched_paths("do_GET"),
                         set(D.PUBLIC_GET_ROUTES))

    def test_unadvertised_routes_serve_but_stay_out_of_the_spec(self):
        """/stats is served and HEAD-able, but deliberately NOT advertised.

        It exposes only aggregate counters -- no addresses, no PII -- but it does
        publish traffic volume and the GO/HOLD/STOP mix, and openapi.json is
        indexed by x402scan and other crawlers. Being served is not the same as
        being catalogued, and that is an operator decision, not a routing one.

        It stays in PUBLIC_GET_ROUTES because that table also drives do_HEAD and
        the dispatch-parity check: dropping it there would 404 HEAD /stats and
        reintroduce exactly the GET/HEAD drift this table was built to kill.

        Mutation: remove /stats from UNADVERTISED_ROUTES -> this FAILS.
        Mutation: drop it from PUBLIC_GET_ROUTES instead -> the HEAD-parity and
        dispatch tests FAIL.
        """
        spec = self._get("/openapi.json")[1]["paths"]
        self.assertTrue(D.UNADVERTISED_ROUTES, "nothing marked unadvertised")
        for path in D.UNADVERTISED_ROUTES:
            self.assertIn(path, D.PUBLIC_GET_ROUTES,
                          "%s must stay a known route" % path)
            self.assertEqual(self._get(path)[0], 200,
                             "%s must still serve" % path)
            req = urllib.request.Request(self.base + path, method="HEAD")
            with urllib.request.urlopen(req, timeout=3) as r:
                self.assertEqual(r.status, 200, "HEAD %s must still serve" % path)
            self.assertNotIn(path, spec,
                             "%s must not be advertised in openapi.json" % path)

    def test_every_advertised_route_actually_serves(self):
        """The converse: nothing in the spec may 404. An advertised-but-absent
        route is the same defect pointed the other way -- it sends a caller to a
        dead path. Mutation: advertise /v1/nope -> this FAILS."""
        for path in D.PUBLIC_GET_ROUTES:
            self.assertEqual(self._get(path)[0], 200, "GET %s 404s" % path)
            if path not in D.UNADVERTISED_ROUTES:
                self.assertIn(path, self._get("/openapi.json")[1]["paths"])

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


class TestIdentityDiscriminators(unittest.TestCase):
    """The descriptor must state METHOD, not just auth and scope.

    WHY. Two products share the Blackwall name (docs/REGISTRIES.md, "The identity
    split"). Measured 2026-08-30: a third-party AI summary credited THIS engine
    with a "remote safety LLM forecasting loop" -- the other product's
    architecture and the exact inverse of this one's -- and rated its token safety
    as "basic context checking" while holder_concentration.py and dex_price.py
    implement the checks it awarded to a competitor.

    The blend was possible because nothing published stated the method, and the
    signal list advertised 2 of the ~25 gates actually implemented. These pin both
    halves.
    """

    def test_method_is_machine_readable(self):
        # kills: dropping the method/modelInVerdictPath fields. A crawler reads
        # fields before prose; leaving method implicit is what let one be invented.
        d = D.build_descriptor()
        self.assertEqual(d["method"], "deterministic")
        self.assertIs(d["modelInVerdictPath"], False)

    def test_description_leads_with_the_method(self):
        # kills: reverting to a description that does not distinguish this engine
        # from a model-as-judge product
        text = D.build_descriptor()["description"].lower()
        self.assertIn("deterministic", text)
        self.assertIn("no model", text)

    def test_always_on_signals_are_advertised(self):
        """Each of these fires in a bare verdict with NO optional source wired --
        verified end to end, not assumed. Advertising 2 of them understated the
        engine by an order of magnitude and left a comparison-shopper nothing to
        compare.

        kills: trimming the always-on list back.
        """
        signals = D.build_descriptor()["signals"]
        for s in ("counterparty-reputation", "price-anomaly", "sybil-structure",
                  "payload-simulation", "permit2-allowance", "calldata-drainer",
                  "secret-exfiltration", "payee-syntax", "evidence-confidence"):
            self.assertIn(s, signals)

    def test_every_advertised_always_on_gate_really_runs_unconfigured(self):
        """The list above is hand-maintained, so it drifts the moment two branches
        land in parallel -- which is exactly how `payee-syntax` came to be missing
        from it. This derives the answer instead of restating it.

        SCOPE, stated precisely because an earlier version of this test claimed
        more than it checked. "Always-on" means the GATE RUNS with no optional
        source wired. It does NOT mean a `signals` key always appears: measured,
        only 4 of the 9 advertised labels emit a key unconditionally,
        `evidence-confidence` lands in the top-level `confidence` field, and
        `permit2-allowance` / `calldata-drainer` correctly say nothing at all on
        a payment that carries neither an allowance nor a transaction. So each
        label is checked by the evidence that actually exists for it: a key when
        it always emits one, and a FIRED VERDICT when it only speaks up on input.

        `sybil-structure` has no dedicated key or standalone trigger here -- it
        is folded into the reputation gate -- and is covered by test_redteam's
        "Sybil: <3 distinct payers" scenario instead.

        kills: advertising a gate this deployment does not run, in either the
        key-emitting or the input-triggered half.
        """
        import blackwall

        class _Empty:
            def lookup(self, counterparty):
                return {}

        class _Reputable:
            def lookup(self, counterparty):
                return {"settlement_count": 500, "dispute_rate": 0.0,
                        "distinct_payers": 30, "price_history": ["0.05"] * 20}

        def verdict(source, **extra):
            payload = {"counterparty": "0x" + "1" * 40, "amount": "0.05",
                       "asset": "USDC", "chain": "eip155:8453",
                       "payer": "0x" + "2" * 40}
            payload.update(extra)
            resp, err = blackwall.forecast(payload, source)
            self.assertIsNone(err)
            return resp

        advertised = D.build_descriptor()["signals"]
        bare = verdict(_Empty())

        # (a) the labels that emit a signal key on EVERY verdict.
        for label, key in (("counterparty-reputation", "counterparty_reputation"),
                           ("price-anomaly", "price_anomaly"),
                           ("payload-simulation", "payload_signer_status"),
                           ("secret-exfiltration", "secret_scan"),
                           ("payee-syntax", "payee_syntax")):
            self.assertIn(label, advertised)
            self.assertIn(key, bare["signals"],
                          "advertised always-on but absent from a bare verdict: %s"
                          % label)

        # (b) evidence-confidence is emitted, just not under `signals`.
        self.assertIn("evidence-confidence", advertised)
        self.assertIn("confidence", bare)

        # (c) the two that correctly say nothing until given their input. A key
        # check would pass vacuously here, so assert the gate FIRES instead --
        # which is the property being advertised anyway.
        approve = "0x095ea7b3" + "0" * 24 + "9" * 40 + "f" * 64
        for label, extra in (
                ("permit2-allowance",
                 {"scheme": "exact", "permit2AllowanceLimit": str((1 << 256) - 1),
                  "accepts": [{"scheme": "exact", "maxAmountRequired": "50000",
                               "extra": {"assetTransferMethod": "permit2-exact"}}]}),
                ("calldata-drainer",
                 {"transaction": {"to": "0x" + "8" * 40, "data": approve,
                                  "value": "0"}})):
            self.assertIn(label, advertised)
            fired = verdict(_Reputable(), **extra)
            self.assertEqual(fired["verdict"], "STOP",
                             "advertised always-on but inert unconfigured: %s" % label)

    def test_configured_signals_are_not_claimed_when_off(self):
        # The restraint half: claiming a gate this deployment does not run is the
        # same defect in the other direction.
        # kills: advertising opt-in gates unconditionally
        off = D.build_descriptor()["signals"]
        for s in ("settlement-simulation", "honeypot-exit-check",
                  "transfer-restriction-readiness", "dex-market-peg",
                  "holder-concentration"):
            self.assertNotIn(s, off)

    def test_configured_signals_appear_when_wired(self):
        # kills: adding the parameters but never reading them -- the wired-and-inert
        # shape that made honeypot.py's source silently do nothing
        on = D.build_descriptor(settlement_simulation=True, honeypot_check=True,
                                rwa_readiness=True, market_peg=True,
                                holder_concentration=True)["signals"]
        for s in ("settlement-simulation", "honeypot-exit-check",
                  "transfer-restriction-readiness", "dex-market-peg",
                  "holder-concentration"):
            self.assertIn(s, on)

    def test_handler_advertises_only_what_actually_constructed(self):
        """The descriptor reads the wired SOURCES, not the env flags.

        A flag set with a missing RPC builds no source; advertising the gate then
        would be a claim about a check that cannot run.

        kills: reading os.environ in _descriptor instead of the handler attributes.
        """
        import ast
        import inspect

        import blackwall
        src = inspect.getsource(blackwall._Handler._descriptor)
        self.assertNotIn("environ", src,
                         "_descriptor must not read env flags -- a flag can be set "
                         "while the source failed to construct")
        tree = ast.parse(src.strip())
        reads = {n.attr for n in ast.walk(tree)
                 if isinstance(n, ast.Attribute) and n.attr.endswith("_source")}
        for attr in ("settlement_sim_source", "honeypot_source", "rwa_source",
                     "dex_source", "holder_source"):
            self.assertIn(attr, reads)
