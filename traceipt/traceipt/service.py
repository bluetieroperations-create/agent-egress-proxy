"""
Traceipt HTTP service (stdlib http.server, threaded).

Endpoints:
  POST /receipts        (x402-gated) verify settlement -> sign -> chain -> return envelope
  GET  /receipts/{id}   fetch a stored envelope
  GET  /receipts/{id}/vc  the receipt as a W3C Verifiable Credential (AP2-ready)
  GET  /attest/{id}/vc    the anchoring attestation (e.g. a verdict) as a W3C VC
  POST /verify          neutral verifier: verify any VC / VP / lifecycle bundle (public)
  POST /receipts/{id}/disclose  (admin) signed redacted disclosure of chosen fields
  GET  /verify/{id}     re-verify signature + chain link, return a report
  GET  /chain/{seller}  full-chain integrity check for a seller
  GET  /chain/{seller}/completeness  signed proof the chain is WHOLE (nothing hidden)
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

from .canonical import GENESIS_HASH
from .completeness import build_checkpoint
from .disclosure import commit as disclosure_commit, disclose as disclosure_disclose
from .bundle import verify_lifecycle, verify_presentation
from .openapi import spec as openapi_spec
from .signing import b64url_decode
from .vc import (
    JWT_TYP, attestation_vc as build_attestation_vc, context_document,
    did_web_document, enveloped_vc, receipt_vc as build_receipt_vc,
    receipt_vc_jwt as build_receipt_vc_jwt, verify_jwt, verify_vc,
)
from .invoice import render_invoice
from .ledger import Ledger
from .merkle import verify_inclusion
from .publisher import make_publisher
from .schema import (
    build_receipt, receipt_hash, validate_attestation_request,
    validate_commerce, validate_credit, validate_settlement,
)
from .settlement import CAIP2, EIP712_DOMAINS, USDC_CONTRACTS, make_verifier
from .signing import Signer, load_or_create_signer, verify_envelope
from .x402_gate import (
    Facilitator, GateDecision, _proceed, _reject, encode_payment_response,
    gate_settle, gate_verify,
)

MAX_BODY = 64 * 1024
BODY_READ_TIMEOUT = 15.0  # seconds a worker will wait on a slow client


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def select_signer(key_pem: str | None, key_path: str) -> Signer:
    """Choose the issuer signer: an inline PEM secret (RECEIPTS_KEY_PEM) when
    present -- so the key survives on hosts without a persistent disk -- else a
    key file (created on first run). Factored out and referencing Signer at
    module scope on purpose: a missing `Signer` import here previously stayed
    latent (the file path never touches Signer) and crash-looped every deploy
    the first time RECEIPTS_KEY_PEM was set. Unit-tested so it cannot regress."""
    if key_pem:
        return Signer.from_pem(key_pem.encode())
    return load_or_create_signer(key_path)


class App:
    """Holds config + collaborators; the handler delegates here."""

    def __init__(self, *, signer, ledger: Ledger, verifier, gate: str,
                 price_base_units: str, pay_to: str, chain: str, base_url: str,
                 seller_id: str | None = None, facilitator=None,
                 bind_payer: bool = False, extra_public_jwks=None,
                 admin_token: str | None = None, publisher=None):
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
        # Optional on-chain anchor publisher: f(root_hex) -> (network, tx) | None.
        # None records roots locally only (onchain_tx stays null).
        self.publisher = publisher
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

    # -- x402 gate (protocol v2) ------------------------------------------
    def requirements(self, resource: str) -> dict:
        """The single x402 v2 `accepts` entry this service advertises.

        v2 vs v1: the amount field is `amount` (was `maxAmountRequired`), the
        network is CAIP-2 (`eip155:84532`, not the bare `base-sepolia`), and the
        asset's EIP-712 domain rides in `extra` so the facilitator can verify the
        EIP-3009 signature. The resource/description/mimeType move OUT to a
        top-level ResourceInfo (see payment_required_body)."""
        req = {
            "scheme": "exact",
            "network": CAIP2.get(self.chain, self.chain),
            "amount": self.price_base_units,
            "asset": USDC_CONTRACTS[self.chain],
            "payTo": self.pay_to,
            "maxTimeoutSeconds": 60,
        }
        domain = EIP712_DOMAINS.get(self.chain)
        if domain:
            req["extra"] = dict(domain)
        return req

    def _bazaar_extension(self, resource: str) -> dict:
        """x402 Bazaar discovery block. Advertising `extensions.bazaar.info` in
        the 402 response is the ONLY thing a resource must do to be discoverable
        -- any facilitator that implements a discovery layer catalogs it on
        settlement (no separate signup). Facilitator-agnostic; see MCP.md/DEPLOY
        for pointing at a facilitator whose bazaar the ecosystem reads."""
        if resource == "/attest":
            info = {
                "description": "Anchor an external digest (e.g. a risk/sanctions "
                               "verdict) in the next on-chain Merkle batch.",
                "input": {"type": "http", "method": "POST",
                          "bodyExample": {"hash": "sha256:<64 hex>",
                                          "type": "sanctions-verdict",
                                          "ref": "policy:ofac-sanctions-v1"}},
                "output": {"type": "json",
                           "example": {"attestation": {"attestation_id": "att_…",
                                                       "status": "pending"},
                                       "proof_url": self.base_url + "/attest/att_…/proof"}},
                "tags": ["attestation", "anchoring", "compliance", "audit"],
            }
        else:  # /receipts
            info = {
                "description": "Turn a settled x402 USDC payment into a signed, "
                               "on-chain-verified, independently-verifiable receipt "
                               "(W3C VC; optional compliance-verdict binding).",
                "input": {"type": "http", "method": "POST",
                          "bodyExample": {"settlement": {"chain": self.chain,
                                                         "tx_hash": "0x…"},
                                          "commerce": {"resource": "https://…",
                                                       "description": "…",
                                                       "quoted_amount_base_units": "…"}}},
                "output": {"type": "json",
                           "example": {"receipt": {"payload": {"receipt_id": "rcpt_…"},
                                                   "signature": "…"},
                                       "verify_url": self.base_url + "/verify/rcpt_…"}},
                "tags": ["receipts", "compliance", "verifiable-credentials", "audit"],
            }
        return {"bazaar": {"info": info}}

    def payment_required_body(self, resource: str) -> dict:
        """x402 v2 Payment Required body. v2 shape: {x402Version:2, error?,
        resource:{ResourceInfo}, accepts:[...], extensions:{bazaar:…}}. The
        bazaar extension makes this resource discoverable in the x402 Bazaar."""
        desc = ("anchor one external digest in the next Merkle batch"
                if resource == "/attest" else "issue one signed x402 receipt")
        return {
            "x402Version": 2,
            "error": "X-PAYMENT header is required",
            "resource": {
                "url": self.base_url + resource,
                "description": desc,
                "mimeType": "application/json",
                "serviceName": "Traceipt",
            },
            "accepts": [self.requirements(resource)],
            "extensions": self._bazaar_extension(resource),
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

    def _sign_receipt(self, payload: dict) -> dict:
        """Fold in the selective-disclosure field commitment, then sign. The
        `disclosure` block (a Merkle root over the receipt's fields) is thus
        signed and chained/anchored like the rest of the receipt, so a later
        redacted disclosure can be proved against it. See disclosure.py."""
        payload["disclosure"] = disclosure_commit(
            payload, self.signer.disclosure_secret())
        return self.signer.sign_envelope(payload)

    def receipt_vc(self, receipt_id: str) -> tuple[int, dict]:
        """Re-express a stored receipt as a signed W3C Verifiable Credential
        (eddsa-jcs-2022), so it drops into AP2 / VC verifiers alongside the
        payment-instruction Mandates. The VC is independently signed (did:key)
        and verifies fully offline. See vc.py."""
        envelope = self.ledger.get(receipt_id)
        if envelope is None:
            return 404, {"error": "unknown receipt_id"}
        return 200, build_receipt_vc(
            envelope["payload"], self.signer, self.base_url, utc_now())

    def did_document(self) -> dict:
        """The did:web DID document for this issuer, served at
        /.well-known/did.json. Lists the active (and any retired) signing keys
        as Multikey verification methods and links to the did:key identity."""
        host = urlsplit(self.base_url).hostname or "traceipt.xyz"
        retired = []
        for jwk in self.extra_public_jwks:
            try:
                retired.append(b64url_decode(jwk["x"]))
            except Exception:
                pass
        return did_web_document(host, self.signer.public_bytes(), retired)

    def openapi(self) -> dict:
        return openapi_spec(self.base_url, self.price_base_units, self.chain)

    def verify_document(self, body) -> tuple[int, dict]:
        """Neutral verifier: verify ANY submitted W3C VC, VerifiablePresentation,
        or lifecycle bundle -- offline, with zero client-side crypto. Neutral
        because it checks each credential's own embedded did:key signature, so it
        verifies credentials from ANY issuer, not only Traceipt's. Public and
        ungated; verification is a read-only public good."""
        if not isinstance(body, dict):
            return 400, {"error": "body must be a JSON object: a VC, a "
                                  "VerifiablePresentation, or {receipt_vc,...}"}
        # VC-JWT, either as {"jwt": "..."} or an EnvelopedVerifiableCredential.
        token = None
        if isinstance(body.get("jwt"), str):
            token = body["jwt"]
        elif "EnvelopedVerifiableCredential" in (body.get("type") or []):
            vid = body.get("id", "")
            if isinstance(vid, str) and vid.startswith("data:") and "," in vid:
                token = vid.split(",", 1)[1]
        if token is not None:
            payload = verify_jwt(token)
            return 200, {"ok": payload is not None, "kind": "credential-jwt",
                         "issuer": (payload or {}).get("issuer"),
                         "types": (payload or {}).get("type")}
        if any(k in body for k in ("receipt_vc", "verdict_vc", "attestation_vc")):
            ok, report = verify_lifecycle(
                receipt_vc=body.get("receipt_vc"),
                verdict_vc=body.get("verdict_vc"),
                attestation_vc=body.get("attestation_vc"),
                settlement_time=body.get("settlement_time"),
                expected_issuers=body.get("expected_issuers"))
            return 200, {"ok": ok, "kind": "lifecycle", **report}
        types = body.get("type") or []
        if "VerifiablePresentation" in types:
            ok, report = verify_presentation(body)
            return 200, {"ok": ok, "kind": "presentation", **report}
        if "proof" in body:
            return 200, {"ok": verify_vc(body), "kind": "credential",
                         "issuer": body.get("issuer"), "types": types}
        return 400, {"error": "unrecognized document: expected a VC (with a "
                     "proof), a VerifiablePresentation, or a bundle "
                     "{receipt_vc, verdict_vc, attestation_vc}"}

    def receipt_vc_jwt(self, receipt_id: str) -> tuple[int, dict]:
        """The receipt as a compact VC-JWT (W3C VC-JOSE-COSE), for JWT-native
        verifiers, plus the VC-2.0 EnvelopedVerifiableCredential wrapper."""
        envelope = self.ledger.get(receipt_id)
        if envelope is None:
            return 404, {"error": "unknown receipt_id"}
        token = build_receipt_vc_jwt(envelope["payload"], self.signer, self.base_url)
        return 200, {"format": JWT_TYP, "jwt": token,
                     "envelopedVerifiableCredential": enveloped_vc(token)}

    def attestation_vc(self, att_id: str) -> tuple[int, dict]:
        """Traceipt's anchoring attestation (e.g. of a Black_Wall verdict digest)
        as a signed W3C VC: the trustless TIMESTAMP that a verdict was anchored,
        which turns 'screened before paid' from self-asserted into provable when
        paired with the settlement block time. See vc.attestation_to_credential."""
        rec = self.ledger.get_attestation(att_id)
        if rec is None:
            return 404, {"error": "unknown attestation_id"}
        inclusion = self.ledger.attestation_inclusion(att_id)
        return 200, build_attestation_vc(
            rec, inclusion, self.signer, self.base_url, utc_now())

    def disclose_receipt(self, receipt_id: str, reveal_paths) -> tuple[int, dict]:
        """Issue a SIGNED, redacted disclosure of a receipt: reveal only
        `reveal_paths`, prove they are the issuer's originals, hide the rest.
        Operator-gated (the seller hands disclosures to auditors/counterparties).
        The full receipt itself is never exposed."""
        envelope = self.ledger.get(receipt_id)
        if envelope is None:
            return 404, {"error": "unknown receipt_id"}
        payload = envelope["payload"]
        if "disclosure" not in payload:
            return 409, {"error": "receipt predates selective disclosure "
                                  "(no field commitment); reissue to enable it"}
        body = disclosure_disclose(
            payload, self.signer.disclosure_secret(), reveal_paths)
        return 200, self.signer.sign_envelope(body)

    # -- core operation ----------------------------------------------------
    def issue(self, body: dict, gate_payer: str | None = None,
              settle=None) -> tuple[int, dict]:
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
        # Idempotency FAST-PATH (before charging): a re-submitted settlement
        # returns the ORIGINAL receipt and is NOT charged a second time.
        existing = self.ledger.find_by_settlement(seller_id, tx_hash)
        if existing is not None:
            rid = existing["payload"]["receipt_id"]
            return 200, {
                "receipt": existing,
                "verify_url": f"{self.base_url}/verify/{rid}",
                "idempotent": True,
            }

        # Settle the fee BEFORE persisting: a signed receipt is never durably
        # issued unless its fee actually settled ("no settled fee => no
        # receipt"). The settlement/commerce were fully validated above, so
        # the build below cannot fail after the charge. Settle runs OUTSIDE
        # _issue_lock so a slow facilitator can't stall other sellers'
        # issuance; on failure nothing is persisted and the caller is not
        # billed a receipt they didn't get.
        ok, settle_response, err = settle() if settle is not None else (True, None, "")
        if not ok:
            return 402, {"error": "payment settlement failed", "reason": err}

        with self._issue_lock:
            # Re-check under the lock: a concurrent request for the SAME
            # settlement may have issued between the fast-path and here. Rare;
            # the fee just settled is the documented cost of that race.
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
            envelope = self._sign_receipt(payload)
            try:
                self.ledger.append(envelope)
            except ValueError as e:
                return 409, {"error": f"could not append to chain: {e}"}
        resp = {
            "receipt": envelope,
            "verify_url": f"{self.base_url}/verify/{payload['receipt_id']}",
        }
        if settle_response is not None:
            resp["_payment_response"] = settle_response
        return 201, resp

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
            envelope = self._sign_receipt(payload)
            try:
                self.ledger.append(envelope)
            except ValueError as e:
                return 409, {"error": f"could not append to chain: {e}"}
        return 201, {
            "receipt": envelope,
            "verify_url": f"{self.base_url}/verify/{payload['receipt_id']}",
        }

    def _attestation_out(self, rec: dict) -> dict:
        att_id = rec["attestation_id"]
        return {
            "attestation": {
                "attestation_id": att_id,
                "hash": rec["hash"],
                "type": rec["type"],
                "ref": rec["ref"],
                "submitted_at": rec["submitted_at"],
                "status": "anchored" if rec["anchor_id"] else "pending",
            },
            "proof_url": f"{self.base_url}/attest/{att_id}/proof",
        }

    def issue_attestation(self, body: dict, gate_payer: str | None = None,
                          settle=None) -> tuple[int, dict]:
        """Anchoring-as-a-service: accept an external digest (e.g. an
        AAR/acta receipt-chain head) into the next Merkle batch. Paid via
        the x402 gate like receipt issuance. Returns 201 on a new
        submission, 200 when an identical digest is already queued."""
        try:
            validate_attestation_request(body)
        except ValueError as e:
            return 400, {"error": str(e)}
        digest = body["hash"]
        # Idempotency FAST-PATH (before charging): an already-queued digest is
        # returned without settling a second fee.
        existing = self.ledger.find_pending_attestation(digest)
        if existing is not None:
            return 200, {**self._attestation_out(existing), "idempotent": True}

        # Settle the fee BEFORE queuing: no settled fee => no attestation. On
        # failure nothing is queued and the caller is not billed.
        ok, settle_response, err = settle() if settle is not None else (True, None, "")
        if not ok:
            return 402, {"error": "payment settlement failed", "reason": err}

        rec, idem = self.ledger.submit_attestation(
            digest=digest, type_=body.get("type", "digest"),
            ref=body.get("ref"), submitted_at=utc_now(), payer=gate_payer)
        out = self._attestation_out(rec)
        if idem:
            # Rare race: an identical digest was queued concurrently between
            # the fast-path and here. The fee just settled is the documented
            # cost of that race.
            out["idempotent"] = True
            return 200, out
        if settle_response is not None:
            out["_payment_response"] = settle_response
        return 201, out

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

    def completeness_report(self, seller_id: str) -> tuple[int, dict]:
        """A signed, third-party-verifiable proof that this seller's chain is
        COMPLETE: every receipt, in order, nothing hidden. The issuer signs a
        checkpoint over {count, head_hash}; the manifest lets anyone check
        density + linkage from genesis to that signed head. See
        completeness.py and completeness.verify_completeness."""
        manifest = self.ledger.chain_manifest(seller_id)
        count = len(manifest)
        head_hash = manifest[-1]["receipt_hash"] if manifest else GENESIS_HASH
        checkpoint = self.signer.sign_envelope(
            build_checkpoint(seller_id, count, head_hash, utc_now()))
        head_anchor = (self.ledger.inclusion_for(manifest[-1]["receipt_id"])
                       if manifest else None)
        return 200, {
            "seller_id": seller_id,
            "count": count,
            "genesis": GENESIS_HASH,
            "head": ({"sequence": count, "receipt_hash": head_hash}
                     if manifest else None),
            "checkpoint": checkpoint,
            "manifest": manifest,
            "head_anchor": head_anchor,
            "how_to_verify": (
                "1) verify the checkpoint signature against /jwks.json; "
                "2) check the manifest is dense (sequence 1..count) and each "
                "prev_receipt_hash links to the previous receipt_hash, from "
                "genesis through to the signed head; 3) for full authenticity, "
                "fetch each receipt at /receipts/{id} and verify its own "
                "signature. See completeness.verify_completeness."),
        }

    def admin_ok(self, headers) -> bool:
        if self.admin_token is None:
            return True
        return headers.get("Authorization", "") == f"Bearer {self.admin_token}"

    def create_anchor(self) -> tuple[int, dict]:
        try:
            anchor = self.ledger.create_anchor(utc_now(), publisher=self.publisher)
        except Exception as e:
            # a broken/underfunded publisher must not lose the batch; the
            # ledger only commits when the publisher returns, so on failure
            # nothing was sealed and the operator can retry.
            return 502, {"error": f"anchor publish failed, nothing sealed: {e}"}
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
            if path == "/.well-known/did.json":
                return self._send(200, app.did_document())
            if path == "/openapi.json":
                return self._send(200, app.openapi())
            if path == "/credentials/v1":
                return self._send(200, context_document())
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
            if path.startswith("/receipts/") and path.endswith("/vc.jwt"):
                rid = unquote(path[len("/receipts/"):-len("/vc.jwt")])
                code, obj = app.receipt_vc_jwt(rid)
                return self._send(code, obj)
            if path.startswith("/receipts/") and path.endswith("/vc"):
                rid = unquote(path[len("/receipts/"):-len("/vc")])
                code, obj = app.receipt_vc(rid)
                return self._send(code, obj)
            if path.startswith("/receipts/"):
                envelope = app.ledger.get(unquote(path[len("/receipts/"):]))
                if envelope is None:
                    return self._send(404, {"error": "unknown receipt_id"})
                return self._send(200, envelope)
            if path.startswith("/attest/") and path.endswith("/proof"):
                aid = unquote(path[len("/attest/"):-len("/proof")])
                if app.ledger.get_attestation(aid) is None:
                    return self._send(404, {"error": "unknown attestation_id"})
                proof = app.ledger.attestation_inclusion(aid)
                if proof is None:
                    return self._send(409, {"error": "not yet anchored; the next "
                                            "anchor batch will include it"})
                return self._send(200, proof)
            if path.startswith("/attest/") and path.endswith("/vc"):
                aid = unquote(path[len("/attest/"):-len("/vc")])
                code, obj = app.attestation_vc(aid)
                return self._send(code, obj)
            if path.startswith("/attest/"):
                rec = app.ledger.get_attestation(unquote(path[len("/attest/"):]))
                if rec is None:
                    return self._send(404, {"error": "unknown attestation_id"})
                rec["status"] = "anchored" if rec["anchor_id"] else "pending"
                return self._send(200, rec)
            if path.startswith("/verify/"):
                code, obj = app.verify_report(unquote(path[len("/verify/"):]))
                return self._send(code, obj)
            if path.startswith("/chain/") and path.endswith("/completeness"):
                seller = unquote(path[len("/chain/"):-len("/completeness")])
                code, obj = app.completeness_report(seller)
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
            if resource == "/verify":
                # Public, ungated neutral verifier: verify any submitted VC / VP
                # / lifecycle bundle. No payment, no auth -- verification is a
                # read-only public good.
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > MAX_BODY:
                        return self._send(400, {"error": f"body must be 1..{MAX_BODY} bytes"})
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return self._send(400, {"error": "body must be valid JSON"})
                try:
                    code, obj = app.verify_document(body)
                except Exception as e:
                    return self._send(500, {"error": f"internal error: {type(e).__name__}"})
                return self._send(code, obj)
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
            if resource.startswith("/receipts/") and resource.endswith("/disclose"):
                # Operator op: hand out a redacted, signed disclosure of a
                # receipt (reveal a subset, prove it, hide the rest).
                if not app.admin_ok(self.headers):
                    return self._send(403, {"error": "admin token required"})
                rid = unquote(resource[len("/receipts/"):-len("/disclose")])
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = (json.loads(self.rfile.read(length).decode("utf-8"))
                            if 0 < length <= MAX_BODY else {})
                except (ValueError, UnicodeDecodeError):
                    return self._send(400, {"error": "body must be valid JSON"})
                reveal = body.get("reveal", []) if isinstance(body, dict) else []
                if not isinstance(reveal, list):
                    return self._send(400, {"error": "'reveal' must be a list of paths"})
                code, obj = app.disclose_receipt(rid, reveal)
                return self._send(code, obj)
            if resource not in ("/receipts", "/attest"):
                return self._send(404, {"error": "not found"})

            # Paid endpoints share one money-fair flow:
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

            # 3. Perform the operation. The fee is settled INSIDE the op —
            #    after idempotency is resolved (so replays are never charged)
            #    but BEFORE the receipt/attestation is persisted, so a signed
            #    artifact is never durably issued unless its fee settled. On a
            #    settle failure the op returns non-201 and nothing persists.
            def _settle():
                if decision.payment is None:
                    return True, None, ""
                return app.settle_payment(decision.payment, resource)

            try:
                if resource == "/attest":
                    code, obj = app.issue_attestation(
                        body, gate_payer=decision.payer, settle=_settle)
                else:
                    code, obj = app.issue(
                        body, gate_payer=decision.payer, settle=_settle)
            except Exception as e:
                return self._send(500, {"error": f"internal error: {type(e).__name__}"})

            # The op stashes the facilitator's settle response for the
            # X-PAYMENT-RESPONSE header; pop it so it never leaks into the body.
            payment_response = (obj.pop("_payment_response", None)
                                if isinstance(obj, dict) else None)
            if code == 201 and payment_response is not None:
                return self._send_with_payment_response(201, obj, payment_response)
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
    p.add_argument("--min-confirmations", type=int,
                   default=int(os.environ.get("RECEIPTS_MIN_CONFIRMATIONS", "0")),
                   help="block confirmations required before a settlement is "
                        "attested (rpc mode). 0 = as soon as mined; set a few "
                        "for real mainnet money (reorg protection)")
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
                   help="bearer token required for admin ops (POST /anchor, /credits)")
    p.add_argument("--publisher", choices=["off", "mock", "onchain"],
                   default=os.environ.get("RECEIPTS_PUBLISHER", "off"),
                   help="on-chain anchor publisher. 'off' records roots locally; "
                        "'onchain' signs+broadcasts with the gas key")
    p.add_argument("--gas-key", default=os.environ.get("RECEIPTS_GAS_KEY"),
                   help="private key of a dedicated GAS wallet for --publisher "
                        "onchain (never the receiving wallet). Prefer the env var.")
    p.add_argument("--anchor-interval", type=int,
                   default=int(os.environ.get("RECEIPTS_ANCHOR_INTERVAL", "0")),
                   help="seconds between automatic anchor batches (0 = manual "
                        "only, via POST /anchor)")
    p.add_argument("--print-jwk", action="store_true",
                   help="print the active key's PUBLIC JWK and exit (capture "
                        "this into --jwks-history before rotating --key)")
    args = p.parse_args(argv)

    # Signing key: prefer an inline PEM secret (RECEIPTS_KEY_PEM) so the key
    # survives on hosts without a persistent disk; else a key file.
    signer = select_signer(os.environ.get("RECEIPTS_KEY_PEM"), args.key)
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

    if args.publisher == "onchain" and not (args.gas_key or
                                            os.environ.get("RECEIPTS_GAS_KEY")):
        p.error("--publisher onchain requires --gas-key / RECEIPTS_GAS_KEY")
    publisher = make_publisher(args.publisher, args.chain, rpc_url=args.rpc_url,
                               private_key=args.gas_key)

    base_url = args.base_url or f"http://{args.host}:{args.port}"
    app = App(
        signer=signer,
        ledger=Ledger(args.db),
        verifier=make_verifier(args.settlement, args.chain, args.rpc_url,
                               min_confirmations=args.min_confirmations),
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
        publisher=publisher,
    )
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    pub_addr = getattr(publisher, "address", None)
    print(f"Traceipt listening on {args.host}:{args.port} "
          f"(public={base_url}, gate={args.gate}, chain={args.chain}, "
          f"settlement={args.settlement}, bind_payer={app.bind_payer}, "
          f"publisher={args.publisher}"
          f"{' gas=' + pub_addr if pub_addr else ''}, "
          f"anchor_interval={args.anchor_interval}s, kid={app.signer.kid})")

    stop = threading.Event()
    if args.anchor_interval > 0:
        threading.Thread(target=_auto_anchor_loop,
                         args=(app, args.anchor_interval, stop),
                         daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop.set()
        server.shutdown()


def _auto_anchor_loop(app: "App", interval: int, stop: threading.Event):
    """Periodically seal any pending receipts/attestations into an anchor.
    Errors are swallowed (logged to stdout) so a transient publish failure
    doesn't kill the loop; the batch stays unsealed and retries next tick."""
    while not stop.wait(interval):
        try:
            code, obj = app.create_anchor()
            if code == 201:
                a = obj["anchor"]
                print(f"[auto-anchor] sealed {a['leaf_count']} leaves "
                      f"({a.get('receipts')} receipts, {a.get('attestations')} "
                      f"attestations) root={a['root'][:16]}... tx={a.get('onchain_tx')}")
            elif code == 502:
                print(f"[auto-anchor] publish failed: {obj.get('error')}")
        except Exception as e:  # never let the loop die
            print(f"[auto-anchor] error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
