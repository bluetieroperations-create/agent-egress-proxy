"""
mcp.py -- MCP transport (stdio, newline-delimited JSON-RPC 2.0).

Exposes every product as an MCP tool so Claude/Cursor/other agents discover and
call it directly -- this is the *distribution* half of the strategy (agents
self-discover; no human marketing). Implements the minimum MCP surface:
`initialize`, `tools/list`, `tools/call`.

Payment over MCP: a tool call may carry an `x_payment` argument (the x402-over-
MCP pattern). Without it, and when the platform requires payment, the tool
returns the x402 offer as its result so an x402-aware client can pay and retry.
Full facilitator settlement reuses Platform.serve_call unchanged.

This is a compact, dependency-free implementation of the stdio transport; for
production you may prefer the official MCP SDK, but the tool<->serve_call
mapping stays identical.
"""
from __future__ import annotations

import json
import sys

PROTOCOL_VERSION = "2024-11-05"


class MCPServer:
    def __init__(self, platform):
        self.platform = platform
        self._tools = {}                    # tool_name -> product
        for p in platform.registry.all():
            self._tools[p.tool_schema()["name"]] = p

    # -- JSON-RPC plumbing ---------------------------------------------------
    def serve(self, stdin=None, stdout=None):
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                continue
            resp = self.handle(req)
            if resp is not None:            # notifications get no response
                stdout.write(json.dumps(resp) + "\n")
                stdout.flush()

    def handle(self, req):
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            return self._ok(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agentdata", "version": "0.1"}})
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return self._ok(rid, {"tools": [p.tool_schema()
                                            for p in self.platform.registry.all()]})
        if method == "tools/call":
            return self._tools_call(rid, req.get("params") or {})
        if rid is None:
            return None
        return self._err(rid, -32601, "method not found: %s" % method)

    def _tools_call(self, rid, params):
        tool = params.get("name")
        args = dict(params.get("arguments") or {})
        product = self._tools.get(tool)
        if product is None:
            return self._err(rid, -32602, "unknown tool: %s" % tool)
        x_payment = args.pop("x_payment", None)
        outcome = self.platform.serve_call(
            product.name, product.action, args, x_payment)
        # Map the pipeline outcome to an MCP tool result.
        if outcome.kind == "payment_required":
            text = json.dumps({"payment_required": True, "offer": outcome.body},
                              indent=2)
            return self._ok(rid, {"content": [{"type": "text", "text": text}],
                                  "isError": True})
        if outcome.status != 200:
            return self._ok(rid, {"content": [{"type": "text",
                                               "text": json.dumps(outcome.body)}],
                                  "isError": True})
        return self._ok(rid, {"content": [{"type": "text",
                                           "text": json.dumps(outcome.body, indent=2)}]})

    def _ok(self, rid, result):
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _err(self, rid, code, message):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def run(platform):
    MCPServer(platform).serve()
