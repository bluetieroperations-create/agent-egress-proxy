"""
Traceipt HTTP service (stdlib http.server, threaded).

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

from .invoice import render_invoice
from .ledger import Ledger
from .merkle import verify_inclusion
from .schema import (
    build_receipt, receipt_hash, validate_commerce, validate_credit,
    validate_settlement,
)
from .settlement import USDC_CONTRACTS, make_verifier
from .signing import load_or_create_signer, verify_envelope
from .x402_gate import (
    Facilitator, GateDecision, _proceed, _reject, encode_payment_response,
    gate_settle, gate_verify,
)

MAX_BODY = 64 * 1024
BODY_READ_TIMEOUT = 15.0  # seconds a worker will wait on a slow client


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class App:
    """Holds config + collaborators; the handler delegates here."""

    def __init__(self, *, signer, ledger: Ledger, verifier, gate: str,
                 price_base_units: str, pay_to: str, chain: str, base_url: str,
                 seller_id: str | None = None, facilitator=None,
                 bind_payer: bool = False, extra_public_jwks=None,
                 admin_token: str | None = None):
        self.signer = signer
        self.ledger = ledger
        self.verifier = verifier
        self.gate = gate
        self.price_base_units = price_base_units
        self.pay_to = pay_to
        self.chain = chain
        self.base_url = base_url.rstrip("/")
        # Retired public keys, still published so receipts signed before a key
        # rotation keep verifying. The active signer signs new receipts.
        self.extra_public_jwks = list(extra_public_jwks or [])
        # Optional bearer token gating admin ops (POST /anchor). When unset,
        # admin ops are open — acceptable behind a private reverse proxy, but
        # production should set it.
        self.admin_token = admin_token
        # Facilitator client for the real x402 gate (None in off/dev modes).
        self.facilitator = facilitator
        # When on, the wallet that pays for the receipt (verified by the
        # facilitator) must equal the settlement's payer — cryptographic
        # payer binding. Only meaningful with the facilitator gate.
        self.bind_payer = bind_payer
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
    def requirements(self, resource: str) -> dict:
        """The single x402 `accepts` entry this service advertises."""
        return {
            "scheme": "exact",
            "network": self.chain,
            "maxAmountRequired": self.price_base_units,
            "asset": USDC_CONTRACTS[self.chain],
            "payTo": self.pay_to,
            "resource": self.base_url + resource,
            "description": "issue one signed x402 receipt",
            "mimeType": "application/json",
            "maxTimeoutSeconds": 60,
        }

    def payment_required_body(self, resource: str) -> dict:
        """x402-shaped payment requirements for the 402 response."""
        return {
            "x402Version": 1,
            "error": "X-PAYMENT header is required",
            "accepts": [self.requirements(resource)],
        }

    def gate_payment(self, headers, resource: str) -> GateDecision:
        """Decide whether this request may proceed, and identify the payer.

        Returns a GateDecision: ok=True carries the verified payer (or None);
        ok=False carries the exact code/body to return to the client.
        """
        if self.gate == "off":
            return _proceed()
        if self.gate == "dev":
            if headers.get("X-PAYMENT", "").strip():
                return _proceed()
            return _reject(402, self.payment_required_body(resource))
        if self.gate == "facilitator":
            return gate_verify(
                self.facilitator, headers, self.requirements(resource),
                self.payment_required_body(resource),
            )
        raise ValueError(f"unknown gate mode {self.gate!r}")

    def settle_payment(self, payment: dict, resource: str):
        """Settle a verified payment after issuance. Returns
        (ok, settle_response, error)."""
        return gate_settle(self.facilitator, payment, self.requirements(resource))

    # -- core operation ----------------------------------------------------
    def issue(self, body: dict, gate_payer: str | None = None) -> tuple[int, dict]:
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
        # third party's transfer.
        if self._pay_to_configured and \
                settlement.get("payee", "").lower() != self.pay_to.lower():
            return 403, {"error": "settlement.payee does not match this "
                                  "service's configured pay_to address"}

        # Payer binding: the wallet that paid for THIS receipt (verified by
        # the facilitator gate) must be the settlement's payer. This is the
        # cryptographic proof that the caller is a party to the payment they
        # are documenting — closing the third-party-claim gap.
        if self.bind_payer and gate_payer is not None and \
                settlement.get("payer", "").lower() != gate_payer.lower():
            return 403, {"error": "settlement.payer does not match the wallet "
                                  "that paid for this receipt (payer binding)"}

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

    def issue_credit(self, body: dict) -> tuple[int, dict]:
        """Issue a credit note: a receipt documenting a VERIFIED refund of a
        prior receipt. The refund must flow in the exact reverse direction
        (original payee -> original payer) and is capped by the original
        amount minus what has already been credited. Credit notes join the
        same sequential per-seller chain — accounting-correct numbering."""
        for field in ("credits_receipt_id", "reason", "settlement"):
            if field not in body:
                return 400, {"error": f"missing field {field!r}"}

        original_env = self.ledger.get(body["credits_receipt_id"])
        if original_env is None:
            return 404, {"error": "unknown credits_receipt_id"}
        original = original_env["payload"]
        if original.get("kind", "payment") != "payment":
            return 400, {"error": "can only credit a payment receipt"}
        seller_id = original["seller_id"]
        if self.seller_id is not None and seller_id != self.seller_id:
            return 403, {"error": "receipt belongs to a different seller"}

        settlement = dict(body["settlement"])
        settlement["asset"] = "USDC"
        if "chain" in settlement and settlement["chain"] != self.chain:
            return 400, {"error": f"settlement.chain must be {self.chain!r}"}
        settlement["chain"] = self.chain
        settlement["asset_contract"] = USDC_CONTRACTS[self.chain]

        try:
            _pre = dict(settlement)
            _pre.setdefault("verified", False)
            _pre.setdefault("verification_method", "unverified")
            validate_settlement(_pre)
            validate_credit({"credits_receipt_id": body["credits_receipt_id"],
                             "reason": body["reason"]})
        except ValueError as e:
            return 400, {"error": str(e)}

        # Refund direction binding: money must flow back along the exact
        # reverse path of the original settlement.
        orig_s = original["settlement"]
        if settlement.get("payer", "").lower() != orig_s["payee"].lower() or \
                settlement.get("payee", "").lower() != orig_s["payer"].lower():
            return 403, {"error": "refund must flow from the original payee "
                                  "back to the original payer"}

        # Amount cap: cannot credit more than remains uncredited.
        already = self.ledger.credited_total(original["receipt_id"])
        remaining = int(orig_s["amount_base_units"]) - already
        if int(settlement["amount_base_units"]) > remaining:
            return 409, {"error": f"credit exceeds remaining creditable amount "
                                  f"({remaining} base units left)"}

        result = self.verifier.verify(settlement)
        if not result.ok:
            return 422, {"error": "refund settlement verification failed",
                         "reason": result.reason}
        settlement["verified"] = True
        settlement["verification_method"] = result.method

        with self._issue_lock:
            existing = self.ledger.find_by_settlement(seller_id,
                                                      settlement["tx_hash"])
            if existing is not None:
                rid = existing["payload"]["receipt_id"]
                return 200, {"receipt": existing,
                             "verify_url": f"{self.base_url}/verify/{rid}",
                             "idempotent": True}
            seq, prev = self.ledger.next_link(seller_id)
            try:
                payload = build_receipt(
                    seller_id=seller_id, sequence=seq, prev_receipt_hash=prev,
                    issued_at=utc_now(), settlement=settlement, kind="credit",
                    credit={"credits_receipt_id": body["credits_receipt_id"],
                            "reason": body["reason"]},
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
        # Active key first, then any retired keys (dedup by kid) so receipts
        # signed before a rotation still verify.
        keys = [self.signer.jwk()]
        seen = {keys[0]["kid"]}
        for jwk in self.extra_public_jwks:
            if jwk.get("kid") not in seen:
                keys.append(jwk)
                seen.add(jwk.get("kid"))
        return {"keys": keys}

    def _envelope_verifier(self):
        """A callback that raises unless an envelope's signature verifies
        against this issuer's published keys."""
        jwks = self._jwks()
        return lambda env: verify_envelope(env, jwks)

    def chain_report(self, seller_id: str) -> list[str]:
        return self.ledger.verify_chain(seller_id, self._envelope_verifier())

    def admin_ok(self, headers) -> bool:
        if self.admin_token is None:
            return True
        return headers.get("Authorization", "") == f"Bearer {self.admin_token}"

    def create_anchor(self) -> tuple[int, dict]:
        anchor = self.ledger.create_anchor(utc_now())
        if anchor is None:
            return 200, {"anchored": 0, "message": "nothing to anchor"}
        return 201, {"anchored": anchor["leaf_count"], "anchor": anchor}

    def invoice_pdf(self, receipt_id: str):
        """Render the stored receipt as an invoice PDF. Returns
        (bytes, filename) or (None, None) if unknown."""
        envelope = self.ledger.get(receipt_id)
        if envelope is None:
            return None, None
        payload = envelope["payload"]
        pdf = render_invoice(
            payload,
            verify_url=f"{self.base_url}/verify/{receipt_id}",
            issuer_kid=envelope.get("protected", {}).get("kid"),
        )
        return pdf, f"{receipt_id}.pdf"

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
        report["kind"] = payload.get("kind", "payment")
        report["seller_id"] = payload["seller_id"]
        report["sequence"] = payload["sequence"]
        report["receipt_hash"] = receipt_hash(payload)
        report["settlement_tx"] = payload["settlement"]["tx_hash"]
        report["settlement_verification"] = payload["settlement"]["verification_method"]
        if report["kind"] == "credit":
            report["credits_receipt_id"] = payload["credit"]["credits_receipt_id"]
        elif self.ledger.credited_total(receipt_id):
            report["credited_base_units"] = str(self.ledger.credited_total(receipt_id))
        # Chain verdict includes a signature check on every receipt in the
        # chain, not just a keyless hash re-derivation.
        problems = self.chain_report(payload["seller_id"])
        report["chain"] = "intact" if not problems else problems
        # Anchor status: if the receipt has been Merkle-anchored, re-verify
        # its inclusion proof against the recorded root.
        proof = self.ledger.inclusion_for(receipt_id)
        if proof is None:
            report["anchor"] = "not yet anchored"
        else:
            ok = verify_inclusion(
                proof["leaf_index"], proof["tree_size"],
                proof["leaf_data"].encode("utf-8"),
                [bytes.fromhex(h) for h in proof["audit_path"]],
                bytes.fromhex(proof["root"]),
            )
            report["anchor"] = {
                "anchor_id": proof["anchor_id"],
                "root": proof["root"],
                "onchain_tx": proof["onchain_tx"],
                "inclusion": "verified" if ok else "FAILED",
            }
            if not ok:
                problems = list(problems) + ["anchor inclusion proof failed"]
        report["verdict"] = "PASS" if not problems else "FAIL"
        return 200, report


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Traceipt/0.1"
        timeout = BODY_READ_TIMEOUT  # cap a slow client holding a worker thread

        def _send(self, code: int, obj: dict):
            data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_pdf(self, data: bytes, filename: str):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition",
                             f'inline; filename="{filename}"')
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
            if path == "/anchors":
                return self._send(200, {"anchors": app.ledger.list_anchors()})
            if path.startswith("/receipts/") and path.endswith("/invoice.pdf"):
                rid = unquote(path[len("/receipts/"):-len("/invoice.pdf")])
                pdf, filename = app.invoice_pdf(rid)
                if pdf is None:
                    return self._send(404, {"error": "unknown receipt_id"})
                return self._send_pdf(pdf, filename)
            if path.startswith("/receipts/") and path.endswith("/proof"):
                rid = unquote(path[len("/receipts/"):-len("/proof")])
                proof = app.ledger.inclusion_for(rid)
                if proof is None:
                    return self._send(404, {"error": "unknown or not-yet-anchored receipt"})
                return self._send(200, proof)
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

        def _send_with_payment_response(self, code, obj, settle_response):
            # x402 echoes the settle result back in an X-PAYMENT-RESPONSE header.
            data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if settle_response:
                self.send_header("X-PAYMENT-RESPONSE",
                                 encode_payment_response(settle_response))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            resource = urlsplit(self.path).path
            if resource == "/anchor":
                # Admin op: seal a Merkle batch. In production this must be
                # authenticated / run by the operator, not exposed publicly.
                if not app.admin_ok(self.headers):
                    return self._send(403, {"error": "admin token required"})
                code, obj = app.create_anchor()
                return self._send(code, obj)
            if resource == "/credits":
                # Operator op: issue a credit note for a verified refund.
                if not app.admin_ok(self.headers):
                    return self._send(403, {"error": "admin token required"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > MAX_BODY:
                        return self._send(400, {"error": f"body must be 1..{MAX_BODY} bytes"})
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return self._send(400, {"error": "body must be valid JSON"})
                try:
                    code, obj = app.issue_credit(body)
                except Exception as e:
                    return self._send(500, {"error": f"internal error: {type(e).__name__}"})
                return self._send(code, obj)
            if resource != "/receipts":
                return self._send(404, {"error": "not found"})

            # 1. Gate: verify payment (does NOT move money yet).
            decision = app.gate_payment(self.headers, resource)
            if not decision.ok:
                return self._send(decision.code, decision.body)

            # 2. Read + parse the request body.
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

            # 3. Issue the receipt (payer binding uses the gate's payer).
            try:
                code, obj = app.issue(body, gate_payer=decision.payer)
            except Exception as e:
                return self._send(500, {"error": f"internal error: {type(e).__name__}"})
            if code != 201:
                # Issuance failed → do NOT settle. The caller is not charged.
                return self._send(code, obj)

            # 4. Settle the payment now that the receipt exists.
            if decision.payment is not None:
                ok, settle_response, err = app.settle_payment(decision.payment, resource)
                if not ok:
                    obj = {**obj, "settlement_warning":
                           f"receipt issued but payment settlement failed: {err}"}
                    return self._send_with_payment_response(201, obj, settle_response or None)
                return self._send_with_payment_response(201, obj, settle_response)
            return self._send(code, obj)

        def log_message(self, fmt, *args):  # quiet; the ledger is the record
            pass

    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description="Traceipt — verifiable receipts "
                                "for x402 machine payments")
    p.add_argument("--port", type=int, default=int(os.environ.get("RECEIPTS_PORT", "8402")))
    p.add_argument("--host", default=os.environ.get("RECEIPTS_HOST", "127.0.0.1"),
                   help="bind address. Default 127.0.0.1 (safe). Use 0.0.0.0 ONLY "
                        "behind a TLS reverse proxy / platform edge that terminates "
                        "HTTPS and forwards to this port.")
    p.add_argument("--base-url", default=os.environ.get("RECEIPTS_BASE_URL"),
                   help="public HTTPS base URL embedded in verify links, the 402 "
                        "resource, and invoice QR (e.g. https://traceipt.xyz). "
                        "Defaults to http://<host>:<port> for local dev.")
    p.add_argument("--db", default=os.environ.get("RECEIPTS_DB", "receipts.db"))
    p.add_argument("--key", default=os.environ.get("RECEIPTS_KEY", "issuer_ed25519.pem"))
    p.add_argument("--gate", choices=["off", "dev", "facilitator"],
                   default=os.environ.get("X402_GATE", "dev"))
    p.add_argument("--facilitator-url",
                   default=os.environ.get("RECEIPTS_FACILITATOR_URL"),
                   help="x402 facilitator base URL (required for --gate facilitator)")
    p.add_argument("--bind-payer", action="store_true",
                   default=os.environ.get("RECEIPTS_BIND_PAYER", "") not in ("", "0"),
                   help="require the wallet paying for the receipt to equal the "
                        "settlement's payer (facilitator gate only)")
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
    p.add_argument("--jwks-history", default=os.environ.get("RECEIPTS_JWKS_HISTORY"),
                   help="JSON file {\"keys\":[...]} of RETIRED public JWKs to keep "
                        "publishing so pre-rotation receipts still verify")
    p.add_argument("--admin-token", default=os.environ.get("RECEIPTS_ADMIN_TOKEN"),
                   help="bearer token required for admin ops (POST /anchor)")
    p.add_argument("--print-jwk", action="store_true",
                   help="print the active key's PUBLIC JWK and exit (capture "
                        "this into --jwks-history before rotating --key)")
    args = p.parse_args(argv)

    signer = load_or_create_signer(args.key)
    if args.print_jwk:
        print(json.dumps(signer.jwk(), indent=2))
        return

    extra_public_jwks = []
    if args.jwks_history:
        with open(args.jwks_history) as f:
            extra_public_jwks = json.load(f).get("keys", [])

    facilitator = None
    if args.gate == "facilitator":
        if not args.facilitator_url:
            p.error("--gate facilitator requires --facilitator-url")
        facilitator = Facilitator(args.facilitator_url)

    base_url = args.base_url or f"http://{args.host}:{args.port}"
    app = App(
        signer=signer,
        ledger=Ledger(args.db),
        verifier=make_verifier(args.settlement, args.chain, args.rpc_url),
        gate=args.gate,
        price_base_units=args.price,
        pay_to=args.pay_to,
        chain=args.chain,
        base_url=base_url,
        seller_id=args.seller_id,
        facilitator=facilitator,
        bind_payer=args.bind_payer,
        extra_public_jwks=extra_public_jwks,
        admin_token=args.admin_token,
    )
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"Traceipt listening on {args.host}:{args.port} "
          f"(public={base_url}, gate={args.gate}, chain={args.chain}, "
          f"settlement={args.settlement}, bind_payer={app.bind_payer}, "
          f"kid={app.signer.kid})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
