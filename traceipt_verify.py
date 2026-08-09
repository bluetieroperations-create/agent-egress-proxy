#!/usr/bin/env python3
"""
traceipt_verify.py -- verify Traceipt's Ed25519-signed receipt envelopes, stdlib-only.

Traceipt (traceipt.xyz) issues receipts wrapped in a JWS-like envelope
(`traceipt/traceipt/signing.py`):

    {
      "protected": {"alg": "EdDSA", "kid": "<key id>", "typ": "x402-receipt+json"},
      "payload":   {...receipt...},
      "signature": "<base64url, no padding>"
    }

The signature is Ed25519 over the CANONICAL JSON of `{"payload": ..., "protected": ...}`
(sorted keys, no whitespace, `ensure_ascii=False`), so the algorithm + key id are
covered (no alg-substitution). This module verifies that signature against the
issuer's published JWKS and returns the *authenticated* receipt payload -- the gate
`traceipt_ingest` needs before a receipt is allowed to touch reputation.

Ed25519 VERIFICATION (RFC 8032 §5.1.7) is implemented here in pure Python: the
curve field math and constants are REUSED from `cdp_auth.py` (already a tested,
stdlib-only Ed25519), and this module adds only what signing didn't need -- point
DECODING (decompression) and the group-equation check `[S]B == R + [k]A`. No
third-party crypto dependency (the repo's `cryptography` binding is optional and,
in some containers, broken -- see test_cdp_auth.py).
"""
from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request

# Reuse the tested curve primitives + constants from cdp_auth (single source of
# truth for the field math -- a fix there fixes both sign and verify).
from cdp_auth import (
    _P, _L, _D, _xrecover, _edwards_add, _scalarmult, _sha512_int, _B,
)

ALG = "EdDSA"
TYP = "x402-receipt+json"


# ---------------------------------------------------------------------------
# Ed25519 verification (the part cdp_auth's sign-only path never needed).
# ---------------------------------------------------------------------------
def _is_on_curve(point) -> bool:
    x, y = point
    # Twisted Edwards: -x^2 + y^2 == 1 + d*x^2*y^2  (mod p)
    return (-x * x + y * y - 1 - _D * x * x * y * y) % _P == 0


def _decodepoint(s: bytes):
    """Decompress a 32-byte Ed25519 point encoding -> (x, y). Raises ValueError on
    a non-canonical y, an x that can't be recovered, or an off-curve point."""
    if len(s) != 32:
        raise ValueError("point must be 32 bytes")
    val = int.from_bytes(s, "little")
    sign = (val >> 255) & 1
    y = val & ((1 << 255) - 1)
    if y >= _P:                      # non-canonical encoding
        raise ValueError("y coordinate out of range")
    x = _xrecover(y)                 # returns the EVEN root
    if x & 1 != sign:
        x = _P - x                   # pick the root whose parity matches the sign bit
    if x == 0 and sign:
        raise ValueError("invalid (0, y) point with sign bit set")
    point = (x, y)
    if not _is_on_curve(point):
        raise ValueError("point not on curve")
    return point


def ed25519_verify(public_key: bytes, msg: bytes, sig: bytes) -> bool:
    """RFC 8032 Ed25519 verify. True iff `sig` is a valid signature of `msg` under
    `public_key`. Never raises -- any malformed input returns False."""
    try:
        if len(public_key) != 32 or len(sig) != 64:
            return False
        R_enc = sig[:32]
        s = int.from_bytes(sig[32:], "little")
        if s >= _L:                  # reject non-canonical / malleable S (S must be < L)
            return False
        A = _decodepoint(public_key)
        R = _decodepoint(R_enc)
        k = _sha512_int(R_enc + public_key + msg) % _L
        # Non-cofactored check (sufficient per RFC 8032, and stricter): [S]B == R + [k]A
        return _scalarmult(_B, s) == _edwards_add(R, _scalarmult(A, k))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Envelope verification against a JWKS.
# ---------------------------------------------------------------------------
def _canonical(obj) -> bytes:
    """Byte-for-byte match of Traceipt's canonical_json (ensure_ascii=False)."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _b64url_decode(s) -> bytes:
    if isinstance(s, str):
        s = s.encode("ascii")
    return base64.urlsafe_b64decode(s + b"=" * (-len(s) % 4))


def _jwk_public_key(jwk) -> bytes | None:
    """Extract the 32-byte raw Ed25519 public key from an OKP JWK, or None."""
    if not isinstance(jwk, dict):
        return None
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        return None
    x = jwk.get("x")
    if not isinstance(x, str):
        return None
    try:
        raw = _b64url_decode(x)
    except Exception:
        return None
    return raw if len(raw) == 32 else None


def verify_envelope(envelope, jwks):
    """Verify a Traceipt signed envelope against a JWKS (`{"keys": [...]}`).

    Returns the AUTHENTICATED payload (the receipt dict) on success, or None if the
    envelope is malformed, uses an unexpected alg, names a kid absent from the JWKS,
    or the Ed25519 signature does not verify. NEVER raises -- a bad envelope is
    simply not authenticated, never a crash in the ingest path.

    Matches `traceipt/signing.py::verify_envelope` (kid + kty + crv lookup, then
    Ed25519 over canonical({payload, protected})), but returns None instead of
    raising so it drops cleanly into `traceipt_ingest`'s fail-closed gate.
    """
    try:
        if not isinstance(envelope, dict):
            return None
        protected = envelope.get("protected")
        payload = envelope.get("payload")
        sig_b64 = envelope.get("signature")
        if not isinstance(protected, dict) or not isinstance(payload, dict) \
                or not isinstance(sig_b64, str):
            return None
        if protected.get("alg") != ALG:          # no alg-substitution
            return None
        kid = protected.get("kid")
        if not kid:
            return None
        try:
            sig = _b64url_decode(sig_b64)
        except Exception:
            return None
        signing_input = _canonical({"payload": payload, "protected": protected})
        # Try EVERY published key advertising this kid and accept if ANY verifies.
        # (Honest kids are key-derived so can't collide, but this way a poisoned
        # JWKS entry sharing a kid can never shadow the genuine key.)
        for jwk in (jwks or {}).get("keys", []) or []:
            if not isinstance(jwk, dict) or jwk.get("kid") != kid:
                continue
            pub = _jwk_public_key(jwk)
            if pub is not None and ed25519_verify(pub, signing_input, sig):
                return payload
        return None
    except Exception:
        return None


def make_verifier(jwks):
    """Return `f(envelope) -> payload|None` bound to `jwks` -- convenient to hand to
    `traceipt_ingest.ingest_envelopes` or to close over a fetched JWKS."""
    def _verify(envelope):
        return verify_envelope(envelope, jwks)
    return _verify


def fetch_jwks(base_url, *, path="/jwks.json", timeout=10, _transport=None):
    """Fetch a JWKS document over HTTPS. Returns the parsed dict, or None on any
    error (fail-closed at the call site: no JWKS -> nothing verifies). `_transport`
    is injectable for tests."""
    try:
        url = base_url.rstrip("/") + path
        get = _transport or _urllib_get_json
        doc = get(url, timeout)
        return doc if isinstance(doc, dict) and "keys" in doc else None
    except Exception:
        return None


def _urllib_get_json(url, timeout):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))
