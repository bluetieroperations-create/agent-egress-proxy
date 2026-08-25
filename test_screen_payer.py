import json, threading, unittest, urllib.request, urllib.error
from http.server import ThreadingHTTPServer
import blackwall as bw
import payer_reputation as PR

PAYEE_A = "0x" + "a1" * 20
PAYEE_B = "0x" + "b2" * 20
ANCHORLESS = "0x" + "c3" * 20
PROVEN = "0x" + "d4" * 20
UNKNOWN = "0x" + "e5" * 20


def _edges():
    """Two anchor payees (>=20 distinct payers each) plus a payer that pays both."""
    edges = []
    for i in range(25):
        p = "0x%040x" % (0x1000 + i)
        edges.append((p, PAYEE_A))
        edges.append((p, PAYEE_B))
    edges.append((PROVEN, PAYEE_A))
    edges.append((PROVEN, PAYEE_B))
    edges.append((ANCHORLESS, "0x" + "f6" * 20))
    return edges


class ScreenPayerHTTP(unittest.TestCase):
    """The payer-side screen must be reachable over HTTP.

    It already existed as an MCP tool, but the buyer for it is a facilitator or
    wallet deciding whether to settle an inbound payment -- and those integrate
    over HTTP, not MCP stdio. Mutation: drop the route -> every test here 404s.
    """

    def setUp(self):
        source = PR.PayerReputationSource(_edges())
        handler = type("_H", (bw._Handler,),
                       {"reputation_source": bw.MockReputationSource(),
                        "graph_source": source})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()

    def _post(self, body, path="/v1/screen-payer"):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_proven_payer_is_established(self):
        st, body = self._post({"payer": PROVEN})
        self.assertEqual(st, 200)
        self.assertEqual(body["tier"], "established")
        self.assertGreaterEqual(body["anchors_paid"], 2)

    def test_unknown_payer_is_neutral_not_a_block(self):
        # The whole posture: most real end-users are unknown. Cold-start must be
        # NEUTRAL. Mutation: returning a blocking verdict for unknown payers would
        # make the product reject legitimate first-time buyers.
        st, body = self._post({"payer": UNKNOWN})
        self.assertEqual(st, 200)
        self.assertEqual(body["tier"], "unknown")
        blob = json.dumps(body).lower()
        for word in ("stop", "block", "deny", "reject"):
            self.assertNotIn(word, blob, "unknown payer must not read as a block")

    def test_address_is_validated(self):
        for bad in ("not-an-address", "0x123", "", None, 42):
            st, body = self._post({"payer": bad})
            self.assertEqual(st, 400, repr(bad))

    def test_missing_source_is_service_unavailable_not_a_crash(self):
        handler = type("_H2", (bw._Handler,),
                       {"reputation_source": bw.MockReputationSource(),
                        "graph_source": None})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/v1/screen-payer" % port,
                data=json.dumps({"payer": PROVEN}).encode(),
                headers={"content-type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    st = r.status
            except urllib.error.HTTPError as e:
                st = e.code
        finally:
            httpd.shutdown(); httpd.server_close()
        self.assertEqual(st, 503)

    def test_matches_the_mcp_tool_exactly(self):
        # One implementation, two transports. Mutation: a divergent HTTP copy
        # would let the MCP and HTTP answers drift for the same wallet.
        source = PR.PayerReputationSource(_edges())
        st, http_body = self._post({"payer": PROVEN})
        self.assertEqual(http_body, source.screen(PROVEN))


if __name__ == "__main__":
    unittest.main()


class PriceIndexIsPublic(unittest.TestCase):
    """The category price index is the cheapest distribution asset we have:
    a CPI for agent services computed from SETTLED reality, not advertised prices.
    It is only an asset if it is reachable and machine-readable."""

    INDEX = {"ai-agents": "0.018999", "finance": "0.005"}

    def setUp(self):
        handler = type("_HP", (bw._Handler,),
                       {"reputation_source": bw.MockReputationSource(),
                        "category_index": dict(self.INDEX)})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()

    def _get(self, path="/v1/price-index"):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d%s" % (self.port, path), timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_serves_the_index(self):
        # Mutation: no route -> 404, and the index stays invisible.
        st, body = self._get()
        self.assertEqual(st, 200)
        self.assertEqual(body["index"], self.INDEX)
        self.assertEqual(body["categories"], 2)

    def test_states_its_unit_and_method(self):
        # Mutation: returning the bare dict. A number with no unit is not citable
        # -- a reader cannot tell USDC-per-call from atomic units.
        st, body = self._get()
        self.assertIn("USDC", body["unit"])
        self.assertIn("median", body["method"])

    def test_absent_index_is_an_empty_index_not_an_error(self):
        # Mutation: raising or 500ing when unconfigured. Fail-open matches every
        # other optional signal on this server.
        handler = type("_HP2", (bw._Handler,),
                       {"reputation_source": bw.MockReputationSource(),
                        "category_index": None})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/v1/price-index" % port, timeout=5) as r:
                body = json.loads(r.read()); st = r.status
        finally:
            httpd.shutdown(); httpd.server_close()
        self.assertEqual(st, 200)
        self.assertEqual(body["index"], {})
        self.assertEqual(body["categories"], 0)
