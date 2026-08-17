#!/usr/bin/env python3
"""
rpc_node.py -- OUR OWN JSON-RPC endpoint: the single, controlled front door for every
on-chain read Blackwall makes.

WHY. The simulation gates (`transfer_sim.py`, `settlement_sim.py`) are a real capability
win, but they put a QUERY LEAK on the hot path: each check tells whichever public RPC we
use "this payer is about to pay this payee this amount". That is exactly the leak
`readiness.py` was built to avoid ("no third-party call, no query leak"), so shipping the
simulation without addressing it would quietly regress our own privacy posture.

WHAT THIS IS (and is not). It is NOT an Ethereum node -- syncing one is terabytes of state
and days of work, and it must be operated, not imported. It IS the piece that makes running
your own node a ONE-LINE config change while immediately reducing the leak either way:

  * SINGLE EGRESS POINT. All chain reads go through one process we control and can audit,
    instead of scattered direct calls to a public provider (the `egress_proxy.py` idea,
    applied to chain reads).
  * CACHE. A repeat check for the same (method, params) is served locally and NEVER
    re-leaks. Identical payer/payee pairs are common (retries, multi-step agent flows), so
    this removes most repeat exposure and most of the latency.
  * METHOD ALLOWLIST. Only read-only methods we actually need are forwarded. A compromised
    or over-eager caller cannot use our endpoint as a general-purpose RPC (no
    eth_sendRawTransaction, no account/admin/personal namespaces).
  * SELF-HOST SWITCH. Point `--upstream` at your own node and the third-party leak is
    ZERO, with no change to any Blackwall code -- the env vars already accept any URL.

STALENESS IS A SAFETY TRADEOFF, SO THE TTL IS SHORT. These reads back a security decision:
a payee blacklisted 5 minutes ago must not be served from a stale "fine" answer. TTL
defaults to 30s (roughly a couple of blocks) and `--ttl 0` disables caching entirely.
Never raise it without deciding how stale a compliance answer may be.

Stdlib only. Pure helpers first (cache key / allowlist / request validation), then the
server.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Read-only methods Blackwall actually uses. Everything else is refused -- this endpoint
# is a narrow tool, not a general RPC. Adding a WRITE method here would let a caller
# broadcast transactions through our credentials/egress; don't.
ALLOWED_METHODS = frozenset({
    "eth_call",
    "eth_blockNumber",
    "eth_chainId",
    "eth_getBalance",
    "eth_getCode",
    "eth_getTransactionReceipt",
    "eth_getTransactionByHash",
    "net_version",
})

DEFAULT_TTL = 30.0            # seconds; short on purpose (see module docstring)
DEFAULT_MAX_ENTRIES = 4096
DEFAULT_MAX_BODY = 1 << 20    # 1 MiB request cap


def is_allowed(method):
    """PURE: may this JSON-RPC method be forwarded? NEVER raises."""
    return isinstance(method, str) and method in ALLOWED_METHODS


def cache_key(method, params):
    """PURE: a stable digest for (method, params). Uses sorted-key JSON so that
    semantically identical requests collide regardless of dict ordering. NEVER raises."""
    try:
        blob = json.dumps([method, params], sort_keys=True, separators=(",", ":"),
                          default=str)
    except Exception:
        blob = "%r|%r" % (method, params)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def validate_request(body):
    """PURE: parsed JSON body -> (ok, error_message). Accepts a single JSON-RPC call
    object; batches are refused so one request cannot fan out into many upstream calls.
    NEVER raises."""
    if not isinstance(body, dict):
        return (False, "expected a single JSON-RPC object (batches are not supported)")
    if body.get("jsonrpc") not in (None, "2.0"):
        return (False, "unsupported jsonrpc version")
    method = body.get("method")
    if not isinstance(method, str) or not method:
        return (False, "missing method")
    if not is_allowed(method):
        return (False, "method not allowed: %s" % method)
    params = body.get("params")
    if params is not None and not isinstance(params, list):
        return (False, "params must be an array")
    return (True, None)


class RpcCache:
    """TTL cache with a bounded size. Thread-safe. `ttl<=0` disables caching entirely."""

    def __init__(self, ttl=DEFAULT_TTL, max_entries=DEFAULT_MAX_ENTRIES, clock=None):
        self.ttl = ttl
        self.max_entries = max_entries
        self._clock = clock or time.monotonic
        self._data = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key):
        if self.ttl <= 0:
            return None
        now = self._clock()
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                self.misses += 1
                return None
            expires, value = hit
            if expires <= now:
                self._data.pop(key, None)
                self.misses += 1
                return None
            self.hits += 1
            return value

    def put(self, key, value):
        if self.ttl <= 0:
            return
        now = self._clock()
        with self._lock:
            if len(self._data) >= self.max_entries:
                # Drop expired first; if still full, evict an arbitrary entry (the cache
                # is a latency/privacy optimization, not a correctness store).
                for k, (exp, _) in list(self._data.items()):
                    if exp <= now:
                        self._data.pop(k, None)
                while len(self._data) >= self.max_entries:
                    self._data.pop(next(iter(self._data)), None)
            self._data[key] = (now + self.ttl, value)

    def __len__(self):
        with self._lock:
            return len(self._data)


def forward_upstream(url, body, timeout=8.0, opener=None):
    """POST a JSON-RPC body upstream and return the parsed response. Raises on transport
    failure (the caller converts it into a JSON-RPC error)."""
    import urllib.request
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "blackwall-rpc/1.0"})
    op = opener or urllib.request.urlopen
    with op(req, timeout=timeout) as r:
        return json.loads(r.read())


class _Handler(BaseHTTPRequestHandler):
    upstream = None
    cache = None
    stats = None
    timeout = 8.0
    forwarder = None          # injectable for tests
    quiet = False

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if not self.quiet:
            import sys
            sys.stderr.write("rpc_node: " + (fmt % args) + "\n")

    def _send(self, code, payload):
        blob = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _err(self, rid, code, message, http=200):
        self._send(http, {"jsonrpc": "2.0", "id": rid,
                          "error": {"code": code, "message": message}})

    def do_GET(self):
        if self.path.rstrip("/") in ("/healthz", "/health"):
            self._send(200, {"ok": True,
                             "cached": len(self.cache) if self.cache is not None else 0,
                             "hits": self.stats.get("hits", 0) if self.stats else 0,
                             "misses": self.stats.get("misses", 0) if self.stats else 0})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > DEFAULT_MAX_BODY:
            self._err(None, -32600, "invalid or oversized request body", http=400)
            return
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._err(None, -32700, "parse error", http=400)
            return

        rid = body.get("id") if isinstance(body, dict) else None
        ok, err = validate_request(body)
        if not ok:
            self._err(rid, -32601, err)
            return

        method, params = body["method"], body.get("params") or []
        key = cache_key(method, params)
        # `is not None`, NOT truthiness: RpcCache defines __len__, so an EMPTY cache is
        # FALSY -- `if self.cache` silently skipped every lookup until something was
        # stored. Caught by a live run showing misses=0 with entries=1.
        cached = self.cache.get(key) if self.cache is not None else None
        if cached is not None:
            out = dict(cached)
            out["id"] = rid
            self._send(200, out)
            return

        fwd = self.forwarder or (lambda b: forward_upstream(self.upstream, b,
                                                            timeout=self.timeout))
        try:
            resp = fwd({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except Exception as e:
            self._err(rid, -32603, "upstream unavailable: %s" % type(e).__name__)
            return
        if not isinstance(resp, dict):
            self._err(rid, -32603, "malformed upstream response")
            return
        # Cache DETERMINISTIC answers only:
        #   * a `result`               -> the call succeeded;
        #   * an error carrying `data` -> a real EVM REVERT, which is a deterministic
        #     answer for this block, not a failure. Caching it matters for privacy: a
        #     blacklisted payee is exactly the case that reverts, so skipping it would
        #     re-leak the most sensitive query on every single check.
        # A bare error with no `data` (rate limit, node hiccup) is TRANSIENT and must not
        # be pinned for the TTL.
        if self.cache is not None:
            err = resp.get("error")
            if "result" in resp and not err:
                self.cache.put(key, {"jsonrpc": "2.0", "result": resp.get("result")})
            elif isinstance(err, dict) and err.get("data") is not None:
                self.cache.put(key, {"jsonrpc": "2.0", "error": dict(err)})
        out = dict(resp)
        out["id"] = rid
        self._send(200, out)


class LocalRpcNode:
    """Our JSON-RPC front door. Point BLACKWALL_*_RPC_URL at it; point IT at your own
    node (or, until you have one, a provider) via `upstream`."""

    def __init__(self, upstream, host="127.0.0.1", port=8599, ttl=DEFAULT_TTL,
                 timeout=8.0, forwarder=None, quiet=False):
        self.upstream = upstream
        self.host = host
        self.port = port
        self.cache = RpcCache(ttl=ttl)
        self.timeout = timeout
        self.forwarder = forwarder
        self.quiet = quiet
        self._httpd = None

    def handler_class(self):
        cache = self.cache
        # staticmethod() is REQUIRED: a plain function stored as a class attribute becomes
        # a descriptor, so `self.forwarder` would bind the handler as its first argument
        # and every forward would raise -- silently degrading to "upstream unavailable".
        fwd = staticmethod(self.forwarder) if self.forwarder is not None else None
        return type("_BoundRpcHandler", (_Handler,), {
            "upstream": self.upstream, "cache": cache, "timeout": self.timeout,
            "forwarder": fwd, "quiet": self.quiet,
            "stats": {"hits": 0, "misses": 0},
        })

    def serve_forever(self):
        self._httpd = ThreadingHTTPServer((self.host, self.port), self.handler_class())
        self.port = self._httpd.server_address[1]
        import sys
        sys.stdout.write(
            "blackwall rpc node on http://%s:%d -> upstream %s (cache ttl %.0fs, %d "
            "allowed methods)\nSet BLACKWALL_RWA_RPC_URL / BLACKWALL_BASE_RPC_URL to "
            "this address.\n"
            % (self.host, self.port, self.upstream or "(none)", self.cache.ttl,
               len(ALLOWED_METHODS)))
        sys.stdout.flush()
        self._httpd.serve_forever()

    def shutdown(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Blackwall's own JSON-RPC endpoint (cache + method allowlist).")
    ap.add_argument("--upstream", required=True,
                    help="URL to forward cache misses to (YOUR node, ideally)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8599)
    ap.add_argument("--ttl", type=float, default=DEFAULT_TTL,
                    help="cache TTL in seconds; 0 disables caching (default 30)")
    args = ap.parse_args(argv)
    node = LocalRpcNode(args.upstream, host=args.host, port=args.port, ttl=args.ttl)
    try:
        node.serve_forever()
    except KeyboardInterrupt:
        node.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
