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

from urllib.parse import unquote, urlsplit

from .ledger import Ledger
from .schema import build_receipt, receipt_hash, validate_commerce, validate_settlement
from .settlement import USDC_CONTRACTS, make_verifier
from .signing import load_or_create_signer, verify_envelope

MAX_BODY = 64 * 1024
BODY_READ_TIMEOUT = 15.0  # seconds a worker will wait on a slow client


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class App:
    """Holds config + collaborators; the handler delegates here."""

    def __init__(self, *, signer, ledger: Ledger, verifier, gate: str,
                 price_base_units: str, pay_to: str, chain: str, base_url: str,
                 seller_id: str | None = None):
        self.signer = signer
        self.ledger = ledger
        self.verifier = verifier
        self.gate = gate
        self.price_base_units = price_base_units
        self.pay_to = pay_to
        self.chain = chain
        self.base_url = base_url.rstrip("/")
        # Seller-hosted model: the operator IS the payee. When seller_id is
        # configured, the caller cannot spoof another identity — the receipt
        # is always issued under the operator's id.
        self.seller_id = seller_id
        # The zero address is the "unconfigured" sentinel; a real pay_to
        # switches on payee binding.
        self._pay_to_configured = (
            pay_to and pay_to.lower() != "0x" + "0" * 40
        )
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
        for field in ("settlement", "commerce"):
            if field not in body:
                return 400, {"error": f"missing field {field!r}"}
        # Seller identity: operator config wins. If the service pins a
        # seller_id, the caller cannot write into a different seller's chain.
        if self.seller_id is not None:
            seller_id = self.seller_id
        elif "seller_id" in body:
            seller_id = body["seller_id"]
        else:
            return 400, {"error": "missing field 'seller_id'"}

        settlement = dict(body["settlement"])
        settlement["asset"] = "USDC"
        # Do NOT trust a caller-supplied chain/contract: the service verifies
        # against exactly one chain, so the receipt must record that chain.
        if "chain" in settlement and settlement["chain"] != self.chain:
            return 400, {"error": f"settlement.chain must be {self.chain!r} "
                                  "(this service only verifies that chain)"}
        settlement["chain"] = self.chain
        settlement["asset_contract"] = USDC_CONTRACTS[self.chain]

        # Validate structure BEFORE touching the chain, so a malformed request
        # is a clean 400 — never a 500 from an unguarded field access.
        try:
            _pre = dict(settlement)
            _pre.setdefault("verified", False)
            _pre.setdefault("verification_method", "unverified")
            validate_settlement(_pre)
            validate_commerce(body["commerce"])
        except ValueError as e:
            return 400, {"error": str(e)}

        # Seller-hosted binding: the settlement must have been paid TO the
        # operator. Without this, a caller could mint a receipt for any
        # third party's transfer. (Payer binding still requires the caller
        # to prove control of the payer address — see THREAT MODEL in README.)
        if self._pay_to_configured and \
                settlement.get("payee", "").lower() != self.pay_to.lower():
            return 403, {"error": "settlement.payee does not match this "
                                  "service's configured pay_to address"}

        result = self.verifier.verify(settlement)
        if not result.ok:
            return 422, {"error": "settlement verification failed", "reason": result.reason}
        settlement["verified"] = True
        settlement["verification_method"] = result.method

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

    def _jwks(self) -> dict:
        return {"keys": [self.signer.jwk()]}

    def _envelope_verifier(self):
        """A callback that raises unless an envelope's signature verifies
        against this issuer's published keys."""
        jwks = self._jwks()
        return lambda env: verify_envelope(env, jwks)

    def chain_report(self, seller_id: str) -> list[str]:
        return self.ledger.verify_chain(seller_id, self._envelope_verifier())

    def verify_report(self, receipt_id: str) -> tuple[int, dict]:
        envelope = self.ledger.get(receipt_id)
        if envelope is None:
            return 404, {"error": "unknown receipt_id"}
        report = {"receipt_id": receipt_id}
        try:
            payload = verify_envelope(envelope, self._jwks())
            report["signature"] = "valid"
        except ValueError as e:
            return 200, {**report, "signature": f"INVALID: {e}", "verdict": "FAIL"}
        report["seller_id"] = payload["seller_id"]
        report["sequence"] = payload["sequence"]
        report["receipt_hash"] = receipt_hash(payload)
        report["settlement_tx"] = payload["settlement"]["tx_hash"]
        report["settlement_verification"] = payload["settlement"]["verification_method"]
        # Chain verdict includes a signature check on every receipt in the
        # chain, not just a keyless hash re-derivation.
        problems = self.chain_report(payload["seller_id"])
        report["chain"] = "intact" if not problems else problems
        report["verdict"] = "PASS" if not problems else "FAIL"
        return 200, report


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "x402-receipts/0.1"
        timeout = BODY_READ_TIMEOUT  # cap a slow client holding a worker thread

        def _send(self, code: int, obj: dict):
            data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            # Strip query string, then percent-decode the path segment so
            # /verify/rcpt_x?foo=1 and /chain/a%2Eb resolve correctly.
            path = urlsplit(self.path).path
            if path == "/health":
                return self._send(200, {"ok": True, "gate": app.gate, "chain": app.chain})
            if path == "/jwks.json":
                return self._send(200, app._jwks())
            if path.startswith("/receipts/"):
                envelope = app.ledger.get(unquote(path[len("/receipts/"):]))
                if envelope is None:
                    return self._send(404, {"error": "unknown receipt_id"})
                return self._send(200, envelope)
            if path.startswith("/verify/"):
                code, obj = app.verify_report(unquote(path[len("/verify/"):]))
                return self._send(code, obj)
            if path.startswith("/chain/"):
                problems = app.chain_report(unquote(path[len("/chain/"):]))
                return self._send(200, {"chain": "intact" if not problems else problems})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if urlsplit(self.path).path != "/receipts":
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
                   "0x0000000000000000000000000000000000000000"),
                   help="operator's USDC receiving address; when set, only "
                        "settlements paid to it can be receipted")
    p.add_argument("--seller-id", default=os.environ.get("RECEIPTS_SELLER_ID"),
                   help="pin the seller identity (seller-hosted mode); "
                        "callers cannot then spoof another seller_id")
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
        seller_id=args.seller_id,
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
