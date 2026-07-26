"""
x402-receipts HTTP service (stdlib http.server, threaded).

Endpoints:
  POST /receipts        (x402-gated) verify settlement -> sign -> chain -> return envelope
  GET  /receipts/{id}   fetch a stored envelope
  GET  /verify/{id}     re-verify signature + chain link, return a report
  GET  /chain/{seller}  full-chain integrity check for a seller
  GET  /jwks.json       issuer public keys (for offline verification)
  GET  /health

x402 gate (X402_GATE env / --gate):
  off  : no payment required (local development)
  dev  : respond 402 with an x402-shaped payment-requirements JSON unless an
         X-PAYMENT header is present; any non-empty header value is accepted.
         This exercises the client-side 402 flow WITHOUT settling money.
  A real facilitator-verified gate is the next milestone (see README).
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .ledger import Ledger
from .schema import build_receipt, receipt_hash
from .settlement import USDC_CONTRACTS, make_verifier
from .signing import load_or_create_signer, verify_envelope

MAX_BODY = 64 * 1024


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class App:
    """Holds config + collaborators; the handler delegates here."""

    def __init__(self, *, signer, ledger: Ledger, verifier, gate: str,
                 price_base_units: str, pay_to: str, chain: str, base_url: str):
        self.signer = signer
        self.ledger = ledger
        self.verifier = verifier
        self.gate = gate
        self.price_base_units = price_base_units
        self.pay_to = pay_to
        self.chain = chain
        self.base_url = base_url.rstrip("/")
        # Serializes the read-head -> build -> sign -> append critical section.
        # The server is threaded; without this, two concurrent issuances for
        # the same seller both read the same chain head and one dies on a
        # chain-break error.
        self._issue_lock = threading.Lock()

    # -- x402 gate ---------------------------------------------------------
    def payment_required_body(self, resource: str) -> dict:
        """x402-shaped payment requirements for the 402 response."""
        return {
            "x402Version": 1,
            "error": "X-PAYMENT header is required",
            "accepts": [{
                "scheme": "exact",
                "network": self.chain,
                "maxAmountRequired": self.price_base_units,
                "asset": USDC_CONTRACTS[self.chain],
                "payTo": self.pay_to,
                "resource": self.base_url + resource,
                "description": "issue one signed x402 receipt",
                "mimeType": "application/json",
                "maxTimeoutSeconds": 60,
            }],
        }

    def gate_allows(self, headers) -> bool:
        if self.gate == "off":
            return True
        if self.gate == "dev":
            return bool(headers.get("X-PAYMENT", "").strip())
        raise ValueError(f"unknown gate mode {self.gate!r}")

    # -- core operation ----------------------------------------------------
    def issue(self, body: dict) -> tuple[int, dict]:
        for field in ("seller_id", "settlement", "commerce"):
            if field not in body:
                return 400, {"error": f"missing field {field!r}"}
        settlement = dict(body["settlement"])
        settlement.setdefault("asset", "USDC")
        settlement.setdefault("asset_contract", USDC_CONTRACTS.get(self.chain, ""))

        result = self.verifier.verify(settlement)
        if not result.ok:
            return 422, {"error": "settlement verification failed", "reason": result.reason}
        settlement["verified"] = True
        settlement["verification_method"] = result.method

        seller_id = body["seller_id"]
        tx_hash = settlement.get("tx_hash", "")
        with self._issue_lock:
            # One settlement, one receipt: re-submitting the same tx returns
            # the ORIGINAL receipt (idempotent), never a second one.
            existing = self.ledger.find_by_settlement(seller_id, tx_hash)
            if existing is not None:
                rid = existing["payload"]["receipt_id"]
                return 200, {
                    "receipt": existing,
                    "verify_url": f"{self.base_url}/verify/{rid}",
                    "idempotent": True,
                }
            seq, prev = self.ledger.next_link(seller_id)
            try:
                payload = build_receipt(
                    seller_id=seller_id, sequence=seq, prev_receipt_hash=prev,
                    issued_at=utc_now(), settlement=settlement, commerce=body["commerce"],
                )
            except ValueError as e:
                return 400, {"error": str(e)}
            envelope = self.signer.sign_envelope(payload)
            try:
                self.ledger.append(envelope)
            except ValueError as e:
                return 409, {"error": f"could not append to chain: {e}"}
        return 201, {
            "receipt": envelope,
            "verify_url": f"{self.base_url}/verify/{payload['receipt_id']}",
        }

    def verify_report(self, receipt_id: str) -> tuple[int, dict]:
        envelope = self.ledger.get(receipt_id)
        if envelope is None:
            return 404, {"error": "unknown receipt_id"}
        report = {"receipt_id": receipt_id}
        jwks = {"keys": [self.signer.jwk()]}
        try:
            payload = verify_envelope(envelope, jwks)
            report["signature"] = "valid"
        except ValueError as e:
            return 200, {**report, "signature": f"INVALID: {e}", "verdict": "FAIL"}
        report["seller_id"] = payload["seller_id"]
        report["sequence"] = payload["sequence"]
        report["receipt_hash"] = receipt_hash(payload)
        report["settlement_tx"] = payload["settlement"]["tx_hash"]
        report["settlement_verification"] = payload["settlement"]["verification_method"]
        problems = self.ledger.verify_chain(payload["seller_id"])
        report["chain"] = "intact" if not problems else problems
        report["verdict"] = "PASS" if not problems else "FAIL"
        return 200, report


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "x402-receipts/0.1"

        def _send(self, code: int, obj: dict):
            data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True, "gate": app.gate, "chain": app.chain})
            if self.path == "/jwks.json":
                return self._send(200, {"keys": [app.signer.jwk()]})
            if self.path.startswith("/receipts/"):
                envelope = app.ledger.get(self.path[len("/receipts/"):])
                if envelope is None:
                    return self._send(404, {"error": "unknown receipt_id"})
                return self._send(200, envelope)
            if self.path.startswith("/verify/"):
                code, obj = app.verify_report(self.path[len("/verify/"):])
                return self._send(code, obj)
            if self.path.startswith("/chain/"):
                problems = app.ledger.verify_chain(self.path[len("/chain/"):])
                return self._send(200, {"chain": "intact" if not problems else problems})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/receipts":
                return self._send(404, {"error": "not found"})
            if not app.gate_allows(self.headers):
                return self._send(402, app.payment_required_body(self.path))
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._send(400, {"error": "bad Content-Length"})
            if length <= 0 or length > MAX_BODY:
                return self._send(400, {"error": f"body must be 1..{MAX_BODY} bytes"})
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._send(400, {"error": "body must be valid JSON"})
            try:
                code, obj = app.issue(body)
            except Exception as e:
                # Never let an exception kill the connection without a response.
                return self._send(500, {"error": f"internal error: {type(e).__name__}"})
            return self._send(code, obj)

        def log_message(self, fmt, *args):  # quiet; the ledger is the record
            pass

    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description="x402-receipts service")
    p.add_argument("--port", type=int, default=int(os.environ.get("RECEIPTS_PORT", "8402")))
    p.add_argument("--db", default=os.environ.get("RECEIPTS_DB", "receipts.db"))
    p.add_argument("--key", default=os.environ.get("RECEIPTS_KEY", "issuer_ed25519.pem"))
    p.add_argument("--gate", choices=["off", "dev"],
                   default=os.environ.get("X402_GATE", "dev"))
    p.add_argument("--chain", choices=["base", "base-sepolia"],
                   default=os.environ.get("RECEIPTS_CHAIN", "base-sepolia"))
    p.add_argument("--settlement", choices=["rpc", "mock"],
                   default=os.environ.get("RECEIPTS_SETTLEMENT", "rpc"))
    p.add_argument("--rpc-url", default=os.environ.get("RECEIPTS_RPC_URL"))
    p.add_argument("--price", default=os.environ.get("RECEIPTS_PRICE_BASE_UNITS", "2000"))
    p.add_argument("--pay-to", default=os.environ.get("RECEIPTS_PAY_TO",
                   "0x0000000000000000000000000000000000000000"))
    args = p.parse_args(argv)

    app = App(
        signer=load_or_create_signer(args.key),
        ledger=Ledger(args.db),
        verifier=make_verifier(args.settlement, args.chain, args.rpc_url),
        gate=args.gate,
        price_base_units=args.price,
        pay_to=args.pay_to,
        chain=args.chain,
        base_url=f"http://127.0.0.1:{args.port}",
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app))
    print(f"x402-receipts listening on 127.0.0.1:{args.port} "
          f"(gate={args.gate}, chain={args.chain}, settlement={args.settlement}, "
          f"kid={app.signer.kid})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
