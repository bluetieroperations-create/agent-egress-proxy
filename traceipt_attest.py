#!/usr/bin/env python3
"""
traceipt_attest.py -- anchor a Black_Wall verdict as a Traceipt attestation.

Black_Wall issues the pre-payment VERDICT; Traceipt (traceipt.xyz) issues the
post-payment RECEIPT and offers anchoring-as-a-service (POST /attest, RFC 6962
Merkle) so an external digest becomes a trustless, time-stamped proof. This
bridges the two: compute a canonical digest of a verdict and fold it into
Traceipt's Merkle batch, so "Black_Wall issued verdict X at time T" is provable
by anyone -- PROOFS, never the private reputation corpus (see docs/STRATEGY_REVIEW.md).

Pure helpers (digest, request shape) are stdlib and unit-tested against Traceipt's
own `/attest` contract. `anchor_verdict()` does the HTTP POST **fail-open**:
anchoring must NEVER break or delay a verdict, so any error returns a benign dict,
never an exception. `/attest` is x402-paid -- pass a payment header via `x_payment`
(produce one with clients/x402_pay.py) or the gate returns 402 and we fail open.
Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request

ATTEST_TYPE = "blackwall-verdict"  # Traceipt attestation `type` (<=40 chars)


def canonical_json(obj) -> bytes:
    """Deterministic JSON bytes (sorted keys, no whitespace) -- so the same
    verdict always yields the same digest."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verdict_digest(verdict_obj) -> str:
    """Traceipt-format digest of a verdict: `sha256:<64 lowercase hex>`
    (matches Traceipt's schema `^sha256:[0-9a-f]{64}$`)."""
    return "sha256:" + hashlib.sha256(canonical_json(verdict_obj)).hexdigest()


def build_attest_request(digest, ref=None, type_=ATTEST_TYPE):
    """The POST /attest body. Conforms to Traceipt's validate_attestation_request:
    only {hash, type, ref}; `hash` = sha256:<64 hex>; `type` <=40; `ref` <=300."""
    body = {"hash": digest, "type": type_[:40]}
    if ref is not None:
        body["ref"] = str(ref)[:300]
    return body


def anchor_verdict(base_url, verdict_obj, *, x_payment=None, timeout=10,
                   _transport=None):
    """Anchor a verdict via Traceipt `POST /attest`. FAIL-OPEN: returns a dict
    and NEVER raises -- anchoring is best-effort and must not affect the verdict.

    Returns {ok, digest, attestation_id?, proof_url?, status?, reason?}.
    `_transport(url, data, headers, timeout) -> (status_code, dict)` is injectable
    for tests; defaults to urllib.
    """
    try:
        digest = verdict_digest(verdict_obj)
        ref = verdict_obj.get("receipt_id") if isinstance(verdict_obj, dict) else None
        body = build_attest_request(digest, ref=ref)
        url = base_url.rstrip("/") + "/attest"
        headers = {"Content-Type": "application/json"}
        if x_payment:
            headers["X-PAYMENT"] = x_payment
        post = _transport or _urllib_post
        status, resp = post(url, canonical_json(body), headers, timeout)
        resp = resp or {}
        if status in (200, 201):
            att = resp.get("attestation") or {}
            return {"ok": True, "digest": digest,
                    "attestation_id": att.get("attestation_id"),
                    "proof_url": resp.get("proof_url"),
                    "status": att.get("status")}
        if status == 402:
            return {"ok": False, "digest": digest, "reason": "payment_required"}
        return {"ok": False, "digest": digest, "reason": "http_%s" % status}
    except Exception as e:  # fail-open: anchoring never breaks the verdict
        return {"ok": False, "reason": "%s: %s" % (type(e).__name__, e)}


def _urllib_post(url, data, headers, timeout):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
