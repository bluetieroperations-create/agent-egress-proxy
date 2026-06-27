"""
Tests for the Blackwall MCP server (JSON-RPC dispatch + stdio loop).

Run: python -m unittest test_mcp_server.py -v

handle() is pure (dict -> dict|None), so the protocol is tested without stdio.
Mutation notes per class.
"""
import io
import json
import os
import tempfile
import unittest

import ledger as L
import mcp_server as M

GOOD = "0xKNOWNGOOD000000000000000000000000000001"
SANC = "0xSANCTIONED00000000000000000000000000003"


def req(method, params=None, mid=1):
    m = {"jsonrpc": "2.0", "method": method, "id": mid}
    if params is not None:
        m["params"] = params
    return m


class TestProtocol(unittest.TestCase):
    """
    Mutation notes:
      - drop the jsonrpc check -> test_invalid_envelope FAILS.
      - return result for notifications -> test_notification_no_response FAILS.
      - wrong error code for unknown method -> test_unknown_method FAILS.
    """

    def setUp(self):
        self.s = M.BlackwallMCP()

    def test_initialize(self):
        r = self.s.handle(req("initialize", {"protocolVersion": "2024-11-05"}))
        self.assertEqual(r["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertEqual(r["result"]["serverInfo"]["name"], "blackwall")

    def test_initialize_defaults_version(self):
        r = self.s.handle(req("initialize", {}))
        self.assertEqual(r["result"]["protocolVersion"], M.PROTOCOL_VERSION)

    def test_ping(self):
        self.assertEqual(self.s.handle(req("ping"))["result"], {})

    def test_unknown_method(self):
        r = self.s.handle(req("does/not/exist"))
        self.assertEqual(r["error"]["code"], M.METHOD_NOT_FOUND)

    def test_invalid_envelope(self):
        r = self.s.handle({"method": "ping", "id": 1})  # missing jsonrpc
        self.assertEqual(r["error"]["code"], M.INVALID_REQUEST)

    def test_notification_no_response(self):
        # No "id" => notification => no response.
        self.assertIsNone(self.s.handle({"jsonrpc": "2.0",
                                         "method": "notifications/initialized"}))

    def test_unknown_notification_silent(self):
        self.assertIsNone(self.s.handle({"jsonrpc": "2.0", "method": "whatever"}))


class TestToolsList(unittest.TestCase):
    def test_forecast_tool_present(self):
        s = M.BlackwallMCP()
        tools = s.handle(req("tools/list"))["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertIn("forecast_payment", names)
        # report_outcome only appears WITH a ledger.
        self.assertNotIn("report_outcome", names)

    def test_report_outcome_tool_with_ledger(self):
        path = os.path.join(tempfile.mkdtemp(), "l.jsonl")
        s = M.BlackwallMCP(ledger=L.EventLedger(path))
        names = [t["name"] for t in s.handle(req("tools/list"))["result"]["tools"]]
        self.assertIn("report_outcome", names)


class TestToolsCall(unittest.TestCase):
    def setUp(self):
        self.s = M.BlackwallMCP()

    def _call(self, name, arguments):
        return self.s.handle(req("tools/call", {"name": name, "arguments": arguments}))["result"]

    def test_forecast_go(self):
        r = self._call("forecast_payment",
                       {"counterparty": GOOD, "amount": "0.09",
                        "asset": "USDC", "chain": "base"})
        self.assertFalse(r["isError"])
        self.assertEqual(r["structuredContent"]["verdict"], "GO")
        self.assertEqual(r["content"][0]["type"], "text")

    def test_forecast_stop(self):
        r = self._call("forecast_payment",
                       {"counterparty": SANC, "amount": "0.09",
                        "asset": "USDC", "chain": "base"})
        self.assertEqual(r["structuredContent"]["verdict"], "STOP")

    def test_forecast_validation_error_is_tool_error(self):
        # missing required field -> isError True (NOT a JSON-RPC protocol error).
        r = self._call("forecast_payment", {"counterparty": GOOD})
        self.assertTrue(r["isError"])

    def test_bad_payer_is_tool_error(self):
        r = self._call("forecast_payment",
                       {"counterparty": GOOD, "amount": "0.09", "asset": "USDC",
                        "chain": "base", "payer": "0xNOPE"})
        self.assertTrue(r["isError"])

    def test_unknown_tool(self):
        r = self._call("nope", {})
        self.assertTrue(r["isError"])

    def test_arguments_must_be_object(self):
        r = self.s.handle(req("tools/call", {"name": "forecast_payment",
                                             "arguments": "notdict"}))["result"]
        self.assertTrue(r["isError"])


class TestReportOutcomeTool(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "l.jsonl")
        self.led = L.EventLedger(self.path)
        self.s = M.BlackwallMCP(ledger=self.led)

    def test_records_outcome(self):
        # first make a verdict so the receipt exists to attribute against
        f = self.s.handle(req("tools/call", {"name": "forecast_payment",
              "arguments": {"counterparty": GOOD, "amount": "0.09",
                            "asset": "USDC", "chain": "base"}}))["result"]
        rid = f["structuredContent"]["receipt_id"]
        tok = f["structuredContent"]["report_token"]  # capability from the verdict
        r = self.s.handle(req("tools/call", {"name": "report_outcome",
              "arguments": {"receipt_id": rid, "report_token": tok,
                            "outcome": "delivered"}}))["result"]
        self.assertFalse(r["isError"])
        self.assertEqual(self.led.aggregate()[GOOD]["settlement_count"], 1)

    def test_report_without_token_is_rejected(self):
        f = self.s.handle(req("tools/call", {"name": "forecast_payment",
              "arguments": {"counterparty": GOOD, "amount": "0.09",
                            "asset": "USDC", "chain": "base"}}))["result"]
        rid = f["structuredContent"]["receipt_id"]
        # Knowing the receipt_id is NOT enough -- need the report_token.
        r = self.s.handle(req("tools/call", {"name": "report_outcome",
              "arguments": {"receipt_id": rid, "outcome": "delivered"}}))["result"]
        self.assertTrue(r["isError"])
        # verdict was recorded by the forecast, but NO outcome -> 0 settlements.
        self.assertEqual(self.led.aggregate()[GOOD]["settlement_count"], 0)

    def test_invalid_outcome_is_tool_error(self):
        # valid token, but bad outcome -> still rejected (need a real receipt+token)
        f = self.s.handle(req("tools/call", {"name": "forecast_payment",
              "arguments": {"counterparty": GOOD, "amount": "0.09",
                            "asset": "USDC", "chain": "base"}}))["result"]
        rid = f["structuredContent"]["receipt_id"]
        tok = f["structuredContent"]["report_token"]
        r = self.s.handle(req("tools/call", {"name": "report_outcome",
              "arguments": {"receipt_id": rid, "report_token": tok,
                            "outcome": "exploded"}}))["result"]
        self.assertTrue(r["isError"])

    def test_missing_receipt_id_is_tool_error(self):
        r = self.s.handle(req("tools/call", {"name": "report_outcome",
              "arguments": {"outcome": "delivered"}}))["result"]
        self.assertTrue(r["isError"])


class TestStdioLoop(unittest.TestCase):
    def test_roundtrip_over_stdio(self):
        s = M.BlackwallMCP()
        inp = io.StringIO(
            json.dumps(req("initialize", {})) + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
            + json.dumps(req("tools/list", mid=2)) + "\n")
        out = io.StringIO()
        s.serve_stdio(stdin=inp, stdout=out)
        lines = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        # initialize response + tools/list response; the notification produced none.
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["id"], 1)
        self.assertEqual(lines[1]["id"], 2)
        self.assertIn("tools", lines[1]["result"])

    def test_parse_error_line(self):
        s = M.BlackwallMCP()
        out = io.StringIO()
        s.serve_stdio(stdin=io.StringIO("{bad json\n"), stdout=out)
        line = json.loads(out.getvalue())
        self.assertEqual(line["error"]["code"], M.PARSE_ERROR)


if __name__ == "__main__":
    unittest.main()
