"""
server.py -- HTTP transport (stdlib only).

Exposes each product at `POST /v1/{name}/{action}` behind the x402 gate, plus
`GET /` (product catalog) and `GET /healthz`. Thin: it only translates HTTP
<-> Platform.serve_call. Production can swap this for FastAPI/uvicorn without
touching any product or the pipeline.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 256 * 1024


def make_handler(platform):
    class Handler(BaseHTTPRequestHandler):
        server_version = "agentdata/0.1"

        def log_message(self, *a):        # quiet default logging
            pass

        def _send(self, status, body, headers=None):
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path.rstrip("/") == "/healthz":
                return self._send(200, {"ok": True})
            if self.path.rstrip("/") in ("", "/"):
                catalog = [{
                    "endpoint": "/v1/%s/%s" % (p.name, p.action),
                    "price": p.price, "asset": platform.config.asset,
                    "description": p.description,
                    "input": p.input_fields,
                } for p in platform.registry.all()]
                return self._send(200, {"products": catalog,
                                        "network": platform.config.network})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            parts = [s for s in self.path.split("?")[0].split("/") if s]
            if len(parts) != 3 or parts[0] != "v1":
                return self._send(404, {"error": "route must be /v1/{name}/{action}"})
            _, name, action = parts

            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"error": "body too large"})
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                return self._send(400, {"error": "invalid JSON body"})

            x_payment = self.headers.get("X-PAYMENT")
            outcome = platform.serve_call(name, action, payload, x_payment)
            self._send(outcome.status, outcome.body, outcome.headers)

    return Handler


def run(platform):
    cfg = platform.config
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(platform))
    mode = "PAID" if cfg.require_payment else "FREE(dev)"
    print("agentdata HTTP on http://%s:%d  facilitator=%s  gate=%s"
          % (cfg.host, cfg.port, cfg.facilitator, mode))
    for p in platform.registry.all():
        print("  POST /v1/%s/%s  %s %s" % (p.name, p.action, p.price, cfg.asset))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        httpd.shutdown()
