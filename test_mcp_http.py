"""
Tests for the MCP Streamable-HTTP transport (mcp_server.serve_http / http_handler_class).

The transport is a thin wrapper over the PURE `handle()` (already covered by
test_mcp_server), so these focus on the HTTP contract: JSON-RPC over POST /mcp,
202 for notifications, 405 for GET on the MCP path, health, size cap, Origin guard,
and that a real tools/call flows through to a verdict.
"""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

import mcp_server as M


def _serve(mcp, **kw):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), mcp.http_handler_class(**kw))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, httpd.server_address[1]


class _Base(unittest.TestCase):
    handler_kw = {}

    def setUp(self):
        self.mcp = M.BlackwallMCP()               # mock reputation source
        self.httpd, self.port = _serve(self.mcp, **self.handler_kw)

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()

    def _post(self, obj, path="/mcp", headers=None, raw=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = raw if raw is not None else json.dumps(obj)
        c.request("POST", path, body=body,
                  headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        r = c.getresponse(); data = r.read(); c.close()
        parsed = json.loads(data) if data else None
        return r.status, parsed

    def _get(self, path):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path)
        r = c.getresponse(); data = r.read(); c.close()
        return r.status, (json.loads(data) if data else None), r.getheader("Allow")


class TestJsonRpc(_Base):
    def test_initialize(self):
        st, body = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": M.PROTOCOL_VERSION}})
        self.assertEqual(st, 200)
        self.assertEqual(body["id"], 1)
        self.assertIn("capabilities", body["result"])
        self.assertEqual(body["result"]["serverInfo"], M.SERVER_INFO)

    def test_tools_list(self):
        st, body = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(st, 200)
        names = [t["name"] for t in body["result"]["tools"]]
        self.assertIn("forecast_payment", names)

    def test_tools_call_forecast_returns_verdict(self):
        st, body = self._post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "forecast_payment", "arguments": {
                                   "counterparty": "0x" + "1" * 40, "amount": "0.09",
                                   "asset": "USDC", "chain": "base"}}})
        self.assertEqual(st, 200)
        sc = body["result"]["structuredContent"]
        self.assertIn(sc["verdict"], ("GO", "HOLD", "STOP"))
        self.assertFalse(body["result"]["isError"])

    def test_notification_gets_202_no_body(self):
        st, body = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(st, 202)
        self.assertIsNone(body)

    def test_unknown_method_is_jsonrpc_error(self):
        st, body = self._post({"jsonrpc": "2.0", "id": 9, "method": "no/such"})
        self.assertEqual(st, 200)                       # JSON-RPC error rides a 200
        self.assertEqual(body["error"]["code"], M.METHOD_NOT_FOUND)

    def test_batch_returns_array(self):
        st, body = self._post([
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},  # no response
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
        self.assertEqual(st, 200)
        self.assertEqual(sorted(r["id"] for r in body), [1, 2])   # notification dropped

    def test_bad_json_is_400_parse_error(self):
        st, body = self._post(None, raw="{not json")
        self.assertEqual(st, 400)
        self.assertEqual(body["error"]["code"], M.PARSE_ERROR)


class TestTransportContract(_Base):
    def test_get_healthz(self):
        st, body, _ = self._get("/healthz")
        self.assertEqual(st, 200)
        self.assertEqual(body["status"], "ok")

    def test_get_mcp_is_405(self):
        st, _, allow = self._get("/mcp")
        self.assertEqual(st, 405)
        self.assertEqual(allow, "POST")

    def test_wrong_path_404(self):
        st, body = self._post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, path="/nope")
        self.assertEqual(st, 404)

    def test_oversize_body_413(self):
        big = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"x": "A" * 5}}
        # force a large declared Content-Length by padding a real field past the cap
        big["params"]["x"] = "A" * (M.MAX_MCP_BODY + 10)
        st, body = self._post(big)
        self.assertEqual(st, 413)


class TestHardening(_Base):
    def test_chunked_body_rejected_411(self):
        # AUDIT regression: a chunked body was read as empty AND its bytes polluted the
        # keep-alive connection. Now it's a clean 411 (Content-Length required) + close.
        import socket
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        chunk = ("%x\r\n" % len(body)).encode() + body + b"\r\n0\r\n\r\n"
        s.sendall(b"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n" + chunk)
        resp = b""
        s.settimeout(5)
        while True:
            b = s.recv(4096)
            if not b:
                break
            resp += b
        s.close()
        self.assertIn(b"411", resp.split(b"\r\n", 1)[0])
        self.assertIn(b"Connection: close", resp)

    def test_handler_has_read_timeout(self):
        # AUDIT regression: no socket timeout meant a slowloris (partial body, stalled)
        # pinned a thread forever. The handler must set a finite timeout. Mutation:
        # remove `timeout` -> this FAILS. (We assert the config, not a 30s wall-clock.)
        h = self.mcp.http_handler_class()
        self.assertIsNotNone(getattr(h, "timeout", None))
        self.assertLessEqual(h.timeout, 120)

    def test_source_crash_never_returns_go(self):
        # fail-safe: if the reputation source raises, the tool call must surface an error,
        # never a GO. (Same posture as the main server's fail-closed 503.)
        class _Boom:
            def lookup(self, cp):
                raise RuntimeError("store locked")
        boom = M.BlackwallMCP(reputation_source=_Boom())
        httpd, port = _serve(boom)
        try:
            import http.client
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", "/mcp", body=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": "forecast_payment", "arguments": {
                        "counterparty": "0x" + "1" * 40, "amount": "0.09",
                        "asset": "USDC", "chain": "base"}}}),
                headers={"Content-Type": "application/json"})
            r = c.getresponse(); body = json.loads(r.read()); c.close()
        finally:
            httpd.shutdown(); httpd.server_close()
        blob = json.dumps(body)
        # either a JSON-RPC error, or a tool result flagged isError -- never a GO verdict
        self.assertNotIn('"verdict": "GO"', blob)
        self.assertNotIn('"verdict":"GO"', blob)
        self.assertTrue("error" in body or body.get("result", {}).get("isError"))


class TestOriginGuard(_Base):
    handler_kw = {"allowed_origins": {"https://sapiom.ai"}}

    def test_allowed_origin_passes(self):
        st, body = self._post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                              headers={"Origin": "https://sapiom.ai"})
        self.assertEqual(st, 200)

    def test_foreign_origin_rejected(self):
        st, body = self._post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                              headers={"Origin": "https://evil.example"})
        self.assertEqual(st, 403)

    def test_no_origin_header_allowed(self):
        # a non-browser client (curl/Sapiom server) sends no Origin -> allowed
        st, body = self._post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(st, 200)


if __name__ == "__main__":
    unittest.main()
