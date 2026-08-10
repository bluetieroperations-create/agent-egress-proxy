#!/usr/bin/env python3
"""
mcp_server.py -- Blackwall as an MCP server (spec step 4).

Wraps the verdict engine as a Model Context Protocol server, so any MCP-capable
agent can discover and call Blackwall self-serve. It is a THIN transport wrapper:
the tools delegate straight to blackwall.forecast() / ledger.record_outcome() --
no decision logic lives here.

stdlib-only (no `mcp` pip SDK): a minimal JSON-RPC 2.0 core implementing the MCP
methods an agent needs (initialize, tools/list, tools/call, ping). The dispatch
core `handle()` is PURE (dict in -> dict|None out) and unit-tested without any I/O.

TWO transports over the same `handle()`:
  * `serve_stdio()` -- the LOCAL self-serve interface (default; unbilled). stdout is
    JSON-RPC only, logs go to STDERR (stdout must stay clean).
  * `serve_http()` -- the REMOTE, `mcp add`-able interface: MCP Streamable HTTP, a
    single POST /mcp endpoint returning `application/json`. STATELESS (no session id),
    notifications get 202, no server-initiated SSE (GET -> 405), with a body cap and an
    optional Origin allowlist (DNS-rebinding guard). This is what lets hosted-MCP
    platforms (Sapiom, Claude Code/Cursor/ChatGPT over HTTP) and the MCP registries
    (Smithery/Glama) reach Blackwall. Monetized access still rides the x402 HTTP
    endpoints (x402.py); this transport is the free self-serve verdict tool.
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

MAX_MCP_BODY = 256 * 1024   # request-body cap for the HTTP transport (oversize guard)

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

    # ---- Streamable-HTTP transport (remote / `mcp add`-able) ----
    def http_handler_class(self, *, allowed_origins=None, path="/mcp",
                           max_body=MAX_MCP_BODY):
        """A BaseHTTPRequestHandler subclass bound to this MCP instance, implementing
        the MCP Streamable-HTTP transport: a single POST endpoint (`path`) that takes a
        JSON-RPC message and returns the JSON-RPC response as `application/json`.

        STATELESS (each request is independent -- no session id needed; every tool call
        is self-contained), so it scales horizontally behind a load balancer. Notifications
        (no `id`) get HTTP 202 with no body, per spec. No server-initiated SSE, so GET on
        the MCP path returns 405. `allowed_origins` (set/None) is the DNS-rebinding guard
        the MCP security guidance calls for: when set, a foreign `Origin` is rejected 403.

        Exposed (rather than inlined in serve_http) so a test or an existing server can
        mount the same handler on its own ThreadingHTTPServer.

        DEPLOY HARDENING (not done here -- do it at the edge): this transport is
        unauthenticated (the free self-serve verdict tool) and has NO rate limit, only a
        body cap. `forecast_payment` with a `payment_authorization` runs the ~30ms signer
        recovery (see BENCHMARKS.md), so a public deployment should sit behind a
        rate-limiter / gateway (e.g. the same posture as blackwall.py's HTTP server) and
        set `allowed_origins` if browsers will reach it."""
        from http.server import BaseHTTPRequestHandler
        mcp = self

        class _MCPHTTPHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            # Socket timeout: a stalled/partial body (slowloris) must not pin a thread
            # forever, and an idle keep-alive connection is reclaimed. This is a backstop
            # -- a public deploy still wants a real rate-limiter/gateway in front.
            timeout = 30

            def log_message(self, fmt, *a):   # keep stdout clean; quiet logs to stderr
                sys.stderr.write("mcp-http: " + (fmt % a) + "\n")

            def _send(self, code, obj=None, close=False):
                # `close`: for early rejects (wrong path / bad origin / oversize) we
                # respond WITHOUT draining the request body, so on a keep-alive
                # connection the server would parse the leftover body as the next
                # request. Signal Connection: close so it doesn't.
                body = b"" if obj is None else json.dumps(
                    obj, separators=(",", ":")).encode()
                self.send_response(code)
                if body:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if close:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                if self.path.split("?", 1)[0] == "/healthz":
                    self._send(200, {"status": "ok", "server": SERVER_INFO,
                                     "protocolVersion": PROTOCOL_VERSION})
                else:   # no server-initiated stream on this transport
                    self.send_response(405)
                    self.send_header("Allow", "POST")
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def do_POST(self):
                if self.path.split("?", 1)[0] != path:
                    self._send(404, _err(None, INVALID_REQUEST, "not found"), close=True)
                    return
                if allowed_origins is not None:
                    origin = self.headers.get("Origin")
                    if origin is not None and origin not in allowed_origins:
                        self._send(403, _err(None, INVALID_REQUEST, "origin not allowed"),
                                   close=True)
                        return
                # We require Content-Length (no streaming body). A chunked body would
                # otherwise be read as empty AND leave undrained bytes that pollute the
                # keep-alive connection -- so reject it cleanly and close.
                if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                    self._send(411, _err(None, INVALID_REQUEST,
                                         "Content-Length required (chunked bodies "
                                         "unsupported)"), close=True)
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length > max_body:
                    self._send(413, _err(None, INVALID_REQUEST, "request too large"),
                               close=True)
                    return
                raw = self.rfile.read(length) if length else b""
                try:
                    msg = json.loads(raw or b"null")
                except ValueError:
                    self._send(400, _err(None, PARSE_ERROR, "parse error"))
                    return
                # A batch (array) returns an array of the non-notification responses;
                # a single message returns its response, or 202 if it was a notification.
                if isinstance(msg, list):
                    out = [r for r in (mcp.handle(m) for m in msg) if r is not None]
                    self._send(202) if not out else self._send(200, out)
                    return
                resp = mcp.handle(msg)
                self._send(202) if resp is None else self._send(200, resp)

        return _MCPHTTPHandler

    def serve_http(self, host="127.0.0.1", port=8403, *, allowed_origins=None,
                   path="/mcp"):
        """Serve the MCP Streamable-HTTP transport forever. Returns the httpd (for tests
        it's easier to mount `http_handler_class()` on your own server)."""
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer(
            (host, port), self.http_handler_class(allowed_origins=allowed_origins,
                                                  path=path))
        sys.stderr.write("blackwall MCP over HTTP on %s:%d (POST %s)\n"
                         % (host, httpd.server_address[1], path))
        sys.stderr.flush()
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()
        return httpd


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
    p.add_argument("--http", metavar="[HOST:]PORT",
                   default=os.environ.get("BLACKWALL_MCP_HTTP"),
                   help="serve MCP over HTTP (Streamable HTTP, `mcp add`-able) at "
                        "[HOST:]PORT instead of stdio, e.g. 0.0.0.0:8403")
    p.add_argument("--allow-origin", action="append", default=None,
                   help="restrict HTTP POST to these Origin headers (repeatable; "
                        "DNS-rebinding guard). Omit to allow any origin.")
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

    transport = ("HTTP " + args.http) if args.http else "stdio"
    sys.stderr.write("blackwall MCP server on %s (reputation: %s, ledger: %s, "
                     "graph: %s, temporal: %s)\n"
                     % (transport, "MOCK" if source is None else type(source).__name__,
                        "on" if ledger else "off", "on" if graph_source else "off",
                        "on" if velocity_source else "off"))
    sys.stderr.flush()
    mcp = BlackwallMCP(reputation_source=source, ledger=ledger,
                       graph_source=graph_source,
                       velocity_source=velocity_source,
                       category_index=category_index,
                       divergence_index=divergence_index)
    if args.http:
        host, sep, port = args.http.rpartition(":")
        host = host if (sep and host) else "127.0.0.1"
        try:
            port_n = int(port)
        except ValueError:
            p.error("--http expects [HOST:]PORT, got %r" % args.http)
        allowed = set(args.allow_origin) if args.allow_origin else None
        mcp.serve_http(host, port_n, allowed_origins=allowed)
    else:
        mcp.serve_stdio()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
