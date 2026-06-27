#!/usr/bin/env python3
"""
facilitator_sim.py -- a reference x402 facilitator, for local dev + tests.

There is no live x402 facilitator in this environment, so this stands in for one
to exercise the REAL HttpFacilitator HTTP adapter end-to-end. It implements the
x402 facilitator API shape:

    POST /verify  {x402Version, paymentPayload, paymentRequirements}
                  -> {isValid: bool, invalidReason: str|null}
    POST /settle  {...}
                  -> {success: bool, transaction: str|null, errorReason: str|null}
    GET  /supported -> {kinds:[{scheme, network}]}

In production you point `x402.HttpFacilitator` at a real facilitator instead
(e.g. a hosted x402 facilitator). This sim does NOT verify signatures or move
funds -- it returns configurable verdicts so the integration path is testable.

Localhost-only, stdlib only.
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    approve = True       # injected by the server
    settle_ok = True

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except (ValueError, UnicodeDecodeError):
            return None

    def do_GET(self):
        if self.path == "/supported":
            self._json(200, {"kinds": [{"scheme": "exact", "network": "base"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        payload = self._read()
        if payload is None:
            self._json(400, {"error": "bad json"})
            return
        if self.path == "/verify":
            self._json(200, {"isValid": bool(self.approve),
                             "invalidReason": None if self.approve else "sim-rejected"})
        elif self.path == "/settle":
            self._json(200, {"success": bool(self.settle_ok),
                             "transaction": "0xsimsettle" if self.settle_ok else None,
                             "errorReason": None if self.settle_ok else "sim-settle-failed"})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, *a):
        pass  # quiet


class FacilitatorSim:
    """Localhost reference facilitator. Toggle approve / settle_ok for tests."""

    def __init__(self, host="127.0.0.1", port=0, approve=True, settle_ok=True):
        self.host = host
        self.port = port
        handler = type("_H", (_Handler,), {"approve": approve, "settle_ok": settle_ok})
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self.port = self._httpd.server_address[1]

    @property
    def url(self):
        return "http://%s:%d" % (self.host, self.port)

    def serve_forever(self):
        self._httpd.serve_forever()

    def start_background(self):
        import threading
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return self

    def shutdown(self):
        self._httpd.shutdown()
        self._httpd.server_close()


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="Reference x402 facilitator (dev/test).")
    p.add_argument("--port", type=int, default=8403)
    p.add_argument("--reject", action="store_true", help="verify returns isValid=false")
    p.add_argument("--fail-settle", action="store_true", help="settle returns success=false")
    args = p.parse_args(argv)
    sim = FacilitatorSim(port=args.port, approve=not args.reject,
                         settle_ok=not args.fail_settle)
    sys.stdout.write("facilitator-sim on %s\n" % sim.url)
    sys.stdout.flush()
    try:
        sim.serve_forever()
    except KeyboardInterrupt:
        sim.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
