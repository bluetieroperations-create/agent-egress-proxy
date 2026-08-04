#!/usr/bin/env python3
"""
mcp_server.py -- Blackwall as an MCP server (spec step 4).

Wraps the verdict engine as a Model Context Protocol server over stdio, so any
MCP-capable agent can discover and call Blackwall self-serve. It is a THIN
transport wrapper: the tools delegate straight to blackwall.forecast() /
ledger.record_outcome() -- no decision logic lives here.

stdlib-only (no `mcp` pip SDK): a minimal JSON-RPC 2.0 loop over newline-
delimited stdio, implementing the MCP methods an agent needs (initialize,
tools/list, tools/call, ping). The dispatch core `handle()` is PURE (dict in ->
dict|None out) and unit-tested without any stdio.

NOTE on transport vs billing: MCP stdio is the LOCAL self-serve interface and is
unbilled here; monetized/remote access is the x402 HTTP endpoints (x402.py).
All chatter stays on stdout as JSON-RPC; logs go to STDERR (stdout must be clean).
"""
from __future__ import annotations

import json
import sys

from blackwall import MockReputationSource, forecast, verify_report_token, _env_flag

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "blackwall", "version": "0.1"}

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_FORECAST_SCHEMA = {
    "type": "object",
    "properties": {
        "counterparty": {"type": "string",
                         "description": "recipient wallet from the 402"},
        "amount": {"type": "string",
                   "description": "amount as a decimal string, e.g. \"0.09\""},
        "asset": {"type": "string", "description": "e.g. USDC"},
        "chain": {"type": "string", "description": "e.g. base"},
        "payer": {"type": "string",
                  "description": "agent's EVM wallet (optional; binds settlement)"},
        "payment_authorization": {
            "type": "string",
            "description": "optional: the actual signed X-PAYMENT (base64) you're "
                           "about to send the counterparty. If given, it's "
                           "cross-checked against the claim -- a mismatch is a hard "
                           "STOP (you'd be signing a different payment than scored)."},
        "transaction": {
            "type": "object",
            "description": "optional: a raw contract-call payment {to, data, value} "
                           "you're about to sign. Calldata is screened for drainer "
                           "patterns (unlimited approval, setApprovalForAll, transfer "
                           "to the wrong recipient) -- a hit is a hard STOP."},
        "resource": {"type": "string", "description": "what's being paid for"},
        "agent_id": {"type": "string", "description": "caller DID/identity"},
        "context": {"type": "object",
                    "description": "{quoted_price_history, expected_recipient}"},
    },
    "required": ["counterparty", "amount", "asset", "chain"],
}

_OUTCOME_SCHEMA = {
    "type": "object",
    "properties": {
        "receipt_id": {"type": "string", "description": "receipt from a prior forecast"},
        "report_token": {"type": "string",
                         "description": "report_token returned with that forecast (authorizes this report)"},
        "outcome": {"type": "string",
                    "description": "settled|delivered|underdelivered|disputed|refunded|abandoned"},
        "observed_amount": {"type": "string"},
        "settlement_tx": {"type": "string"},
    },
    "required": ["receipt_id", "report_token", "outcome"],
}

_SCREEN_SCHEMA = {
    "type": "object",
    "properties": {
        "payer": {"type": "string",
                  "description": "the PAYER wallet address to screen (0x-hex)"},
    },
    "required": ["payer"],
}


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_text(resp):
    """Human-readable one-liner for the verdict (the text content block)."""
    head = "%s (score %s)" % (resp["verdict"], resp["score"])
    reasons = "; ".join(resp.get("reasons", [])[:3])
    return "Blackwall verdict: %s\n%s\nreceipt: %s" % (head, reasons, resp["receipt_id"])


class BlackwallMCP:
    def __init__(self, reputation_source=None, ledger=None, graph_source=None,
                 velocity_source=None, category_index=None, divergence_index=None):
        self.src = reputation_source or MockReputationSource()
        self.ledger = ledger
        self.graph_source = graph_source
        self.velocity_source = velocity_source
        self.category_index = category_index
        self.divergence_index = divergence_index

    # ---- tool catalog ----
    def _tools(self):
        tools = [{
            "name": "forecast_payment",
            "description": ("Pre-signature x402 payment verdict: GO / HOLD / STOP "
                            "for paying a counterparty, with reputation + "
                            "price-anomaly signals and a signed receipt."),
            "inputSchema": _FORECAST_SCHEMA,
        }]
        if self.ledger is not None:
            tools.append({
                "name": "report_outcome",
                "description": ("Report what a prior verdict's payment did "
                                "(settled/delivered/disputed/...), keyed by "
                                "receipt_id -- feeds Blackwall's reputation."),
                "inputSchema": _OUTCOME_SCHEMA,
            })
        if self.graph_source is not None and hasattr(self.graph_source, "screen"):
            tools.append({
                "name": "screen_payer",
                "description": ("Reputation profile for a PAYER wallet (WHO is "
                                "paying): tier established/emerging/unknown, trusted "
                                "anchors paid, ecosystem breadth -- so a facilitator "
                                "or wallet can fast-track a proven agent. Unknown is "
                                "NEUTRAL (cold start), never a block."),
                "inputSchema": _SCREEN_SCHEMA,
            })
        return tools

    # ---- pure dispatch ----
    def handle(self, msg):
        """Dispatch one JSON-RPC message. Returns a response dict, or None for
        notifications (no `id`). PURE -- no stdio."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _err(None, INVALID_REQUEST, "invalid JSON-RPC 2.0 message")
        method = msg.get("method")
        is_notification = "id" not in msg
        mid = msg.get("id")
        if not isinstance(method, str):
            return None if is_notification else _err(mid, INVALID_REQUEST, "missing method")

        try:
            if method == "initialize":
                result = self._initialize(msg.get("params") or {})
            elif method == "notifications/initialized":
                return None  # ack-only notification
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self._tools()}
            elif method == "tools/call":
                result = self._call_tool(msg.get("params") or {})
            else:
                return None if is_notification \
                    else _err(mid, METHOD_NOT_FOUND, "method not found: %s" % method)
        except Exception as e:  # never let a handler crash the loop
            return None if is_notification \
                else _err(mid, INTERNAL_ERROR, "%s: %s" % (type(e).__name__, e))

        return None if is_notification else _ok(mid, result)

    def _initialize(self, params):
        # Echo the client's protocol version when given (per spec), else ours.
        version = params.get("protocolVersion") or PROTOCOL_VERSION
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }

    def _call_tool(self, params):
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return self._tool_error("arguments must be an object")

        if name == "forecast_payment":
            resp, err = forecast(args, self.src, self.ledger,
                                  graph_source=self.graph_source,
                                  velocity_source=self.velocity_source,
                                  category_index=self.category_index,
                                  divergence_index=self.divergence_index)
            if err is not None:
                return self._tool_error(err)
            return {
                "content": [{"type": "text", "text": _tool_text(resp)}],
                "structuredContent": resp,
                "isError": False,
            }

        if name == "screen_payer":
            if self.graph_source is None or not hasattr(self.graph_source, "screen"):
                return self._tool_error(
                    "screen_payer unavailable: no payer-reputation source configured")
            payer = args.get("payer")
            if not payer or not isinstance(payer, str):
                return self._tool_error("payer address is required")
            from addresses import is_evm_address
            if not is_evm_address(payer):
                return self._tool_error("payer must be a valid EVM address")
            profile = self.graph_source.screen(payer)
            return {
                "content": [{"type": "text", "text": profile["summary"]}],
                "structuredContent": profile,
                "isError": False,
            }

        if name == "report_outcome":
            if self.ledger is None:
                return self._tool_error("report_outcome unavailable: no ledger configured")
            rid = args.get("receipt_id")
            if not rid or not isinstance(rid, str):
                return self._tool_error("receipt_id is required")
            if not verify_report_token(rid, args.get("report_token")):
                return self._tool_error("invalid or missing report_token")
            try:
                self.ledger.record_outcome(
                    receipt_id=args.get("receipt_id"),
                    outcome=args.get("outcome"),
                    observed_amount=args.get("observed_amount"),
                    settlement_tx=args.get("settlement_tx"))
            except (ValueError, TypeError) as e:
                return self._tool_error(str(e))
            return {
                "content": [{"type": "text", "text": "recorded"}],
                "isError": False,
            }

        return self._tool_error("unknown tool: %s" % name)

    @staticmethod
    def _tool_error(message):
        # MCP convention: tool failures are a normal result with isError=true,
        # not a JSON-RPC protocol error.
        return {"content": [{"type": "text", "text": message}], "isError": True}

    # ---- stdio loop ----
    def serve_stdio(self, stdin=None, stdout=None):
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self._write(stdout, _err(None, PARSE_ERROR, "parse error"))
                continue
            resp = self.handle(msg)
            if resp is not None:
                self._write(stdout, resp)

    @staticmethod
    def _write(stdout, obj):
        stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        stdout.flush()


def main(argv=None):
    import argparse
    import os
    p = argparse.ArgumentParser(description="Blackwall MCP server (stdio).")
    p.add_argument("--ledger", default=os.environ.get("BLACKWALL_LEDGER"),
                   help="ledger path; enables the report_outcome tool")
    p.add_argument("--store", default=os.environ.get("BLACKWALL_STORE"),
                   help="SQLite reputation store path; uses REAL on-chain "
                        "reputation instead of the mock source")
    p.add_argument("--ingest", action="store_true",
                   default=_env_flag("BLACKWALL_INGEST"),
                   help="with --store, self-populate from chain on first sight")
    args = p.parse_args(argv)

    ledger = None
    if args.ledger:
        from ledger import EventLedger
        ledger = EventLedger(args.ledger)

    source = graph_source = velocity_source = None
    if args.store:
        from reputation_store import production_source
        source = production_source(args.store, ledger=ledger, ingest=args.ingest)
        # Same store -> the cross-counterparty payer graph + propagated payer
        # reputation (built once, cached). Conservative, fail-open Sybil
        # corroboration (captive_sybil + sybil_ring) on top of the verdict.
        from reputation_store import ReputationStore
        from payer_reputation import PayerReputationSource
        graph_source = PayerReputationSource.from_store(ReputationStore(args.store))
        # Temporal axis off the same store: `stale` gates, burst is diagnostic.
        from settlement_velocity import VelocitySource
        velocity_source = VelocitySource(ReputationStore(args.store))

    # Per-category price baseline + advertised-vs-settled divergence, same as the HTTP
    # path: precomputed JSON loaded from BLACKWALL_CATEGORY_INDEX / _DIVERGENCE_INDEX.
    from category_pricing import load_category_index, load_index_json
    category_index, _cat_err = load_category_index(
        os.environ.get("BLACKWALL_CATEGORY_INDEX"))
    if _cat_err:
        sys.stderr.write("mcp: category index unusable (%s) -- signal OFF\n" % _cat_err)
    divergence_index, _div_err = load_index_json(
        os.environ.get("BLACKWALL_DIVERGENCE_INDEX"))
    if _div_err:
        sys.stderr.write("mcp: divergence index unusable (%s) -- signal OFF\n" % _div_err)

    sys.stderr.write("blackwall MCP server on stdio (reputation: %s, ledger: %s, "
                     "graph: %s, temporal: %s)\n"
                     % ("MOCK" if source is None else type(source).__name__,
                        "on" if ledger else "off", "on" if graph_source else "off",
                        "on" if velocity_source else "off"))
    sys.stderr.flush()
    BlackwallMCP(reputation_source=source, ledger=ledger,
                 graph_source=graph_source,
                 velocity_source=velocity_source,
                 category_index=category_index,
                 divergence_index=divergence_index).serve_stdio()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
