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
never an exception.

`/attest` is x402-paid. Two ways to fund it:
  * pass a ready-made `x_payment` base64 header, or
  * pass a `pay(requirements) -> x_payment` callback: on the 402 challenge we pull
    `accepts[0]` and hand it to `pay` to MINT a payment, then retry the POST ONCE.
    Signing an EIP-3009 authorization needs a funded key + `eth-account`, so the
    `pay` callback lives in `clients/x402_pay.py` (`make_pay`, the one dep boundary)
    -- this module stays stdlib-only and never touches a private key.
If neither is given (or minting fails), the gate returns 402 and we fail open.
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


def _first_accepts(resp):
    """The chosen PaymentRequirements (`accepts[0]`) from a 402 body, or None."""
    if not isinstance(resp, dict):
        return None
    accepts = resp.get("accepts")
    if isinstance(accepts, list) and accepts and isinstance(accepts[0], dict):
        return accepts[0]
    return None


def _safe_pay(pay, requirements):
    """Mint an X-PAYMENT via the caller's signer. Fail-open: returns None (never
    raises) when there is nothing to satisfy, `pay` raises, or it yields no header."""
    if requirements is None:
        return None
    try:
        header = pay(requirements)
    except Exception:
        return None
    return header if isinstance(header, str) and header else None


def _challenge_amount_atomic(requirements):
    """Atomic amount a 402 challenge demands (x402 v2 `amount` or v1
    `maxAmountRequired`), or None if absent/unparseable."""
    if not isinstance(requirements, dict):
        return None
    raw = requirements.get("amount", requirements.get("maxAmountRequired"))
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def _within_cap(requirements, cap):
    """True if it is safe to auto-pay this challenge under `cap` atomic units.
    `cap` None = no cap. FAIL-SAFE: with a cap set, an amount we cannot parse is
    treated as OVER the cap -- never sign a payment whose size we can't bound."""
    if cap is None:
        return True
    amt = _challenge_amount_atomic(requirements)
    return amt is not None and amt <= cap


def anchor_verdict(base_url, verdict_obj, *, x_payment=None, pay=None,
                   max_amount_atomic=None, timeout=10, _transport=None):
    """Anchor a verdict via Traceipt `POST /attest`. FAIL-OPEN: returns a dict
    and NEVER raises -- anchoring is best-effort and must not affect the verdict.

    Payment: pass a ready `x_payment` header, OR a `pay(requirements) -> x_payment`
    callback that mints one from the 402 challenge (we retry the POST ONCE with it).
    See `clients/x402_pay.py::make_pay`.

    `max_amount_atomic` caps auto-pay: if the 402 challenge demands MORE than this
    many atomic units we refuse to sign (reason `amount_exceeds_cap`) -- so a
    spoofed/compromised `/attest` can't drain the signer by naming a huge amount or
    an attacker `payTo`. None = uncapped (only for a fully trusted endpoint).

    Returns {ok, digest, attestation_id?, proof_url?, status?, reason?}.
    `_transport(url, data, headers, timeout) -> (status_code, dict)` is injectable
    for tests; defaults to urllib.
    """
    try:
        digest = verdict_digest(verdict_obj)
        ref = verdict_obj.get("receipt_id") if isinstance(verdict_obj, dict) else None
        body = build_attest_request(digest, ref=ref)
        data = canonical_json(body)
        url = base_url.rstrip("/") + "/attest"
        post = _transport or _urllib_post

        def _post(xpay):
            headers = {"Content-Type": "application/json"}
            if xpay:
                headers["X-PAYMENT"] = xpay
            status, resp = post(url, data, headers, timeout)
            return status, (resp or {})

        status, resp = _post(x_payment)
        # Auto-pay: hit a 402, have a signer, and haven't already attached a
        # payment -> mint one from the challenge and retry exactly once.
        if status == 402 and pay is not None and not x_payment:
            req = _first_accepts(resp)
            if req is not None:
                if not _within_cap(req, max_amount_atomic):
                    return {"ok": False, "digest": digest,
                            "reason": "amount_exceeds_cap"}
                minted = _safe_pay(pay, req)
                if minted:
                    status, resp = _post(minted)

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
