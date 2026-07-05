#!/usr/bin/env python3
"""
cdp_auth.py -- CDP (Coinbase Developer Platform) Bearer-JWT auth for the x402
facilitator, so Blackwall can settle through the *CDP* facilitator. That is the
one facilitator whose settlements get catalogued by Coinbase Bazaar -- community
facilitators (x402.rs / xpay.sh) settle fine on Base mainnet but never list.

CDP auth (docs.cdp.coinbase.com/api-reference/v2/authentication): every request
carries `Authorization: Bearer <JWT>`, a fresh EdDSA (Ed25519) JWT that is BOUND
to the exact request via a `uri` claim of the form "METHOD host/path" and expires
120s after issue. So the token must be regenerated per call and per path
(/verify vs /settle) -- it is not reusable.

STDLIB-ONLY on purpose: the service ships with no pip deps (see Dockerfile), so
Ed25519 signing is implemented here from RFC 8032 primitives (hashlib only). This
is SIGN-ONLY with our own key -- we never verify attacker-supplied signatures --
so the blast radius of a bug is "CDP rejects our JWT", not a security hole. The
implementation is checked against the RFC 8032 public-key test vectors here and
cross-checked against the `cryptography` library in test_cdp_auth.py where it is
available (it is not in the container).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from urllib.parse import urlsplit

# ===========================================================================
# Ed25519 (RFC 8032) -- pure-Python, sign-only. hashlib is the only dependency.
# ===========================================================================
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493  # group order


def _inv(x):
    return pow(x, _P - 2, _P)


_D = (-121665 * _inv(121666)) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_By = (4 * _inv(5)) % _P
_B = (_xrecover(_By) % _P, _By % _P)


def _edwards_add(P, Q):
    x1, y1 = P
    x2, y2 = Q
    denom = _D * x1 * x2 * y1 * y2
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + denom) % _P
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - denom) % _P
    return (x3, y3)


def _scalarmult(P, e):
    # Double-and-add over the bits of e (iterative -- no recursion-depth limit).
    result = (0, 1)  # neutral element
    addend = P
    while e > 0:
        if e & 1:
            result = _edwards_add(result, addend)
        addend = _edwards_add(addend, addend)
        e >>= 1
    return result


def _encodepoint(P):
    x, y = P
    # little-endian y with the low bit of x packed into the top bit.
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _sha512_int(b):
    return int.from_bytes(hashlib.sha512(b).digest(), "little")


def _secret_expand(seed):
    """seed (32 bytes) -> (scalar a, prefix) per RFC 8032 clamping."""
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8   # clear low 3 bits
    a |= (1 << 254)       # set bit 254
    return a, h[32:]


def ed25519_publickey(seed):
    a, _ = _secret_expand(seed)
    return _encodepoint(_scalarmult(_B, a))


def ed25519_sign(seed, msg):
    """Deterministic Ed25519 signature (64 bytes) over msg with the 32-byte seed."""
    a, prefix = _secret_expand(seed)
    A = _encodepoint(_scalarmult(_B, a))
    r = _sha512_int(prefix + msg) % _L
    R = _encodepoint(_scalarmult(_B, r))
    k = _sha512_int(R + A + msg) % _L
    s = (r + k * a) % _L
    return R + s.to_bytes(32, "little")


# ===========================================================================
# CDP secret handling + JWT
# ===========================================================================
def cdp_seed_from_secret(secret_b64):
    """
    CDP_API_KEY_SECRET is base64 of the Ed25519 key material: either the 32-byte
    seed or the 64-byte (seed || public key) form. Return the 32-byte seed.
    """
    if not secret_b64 or not isinstance(secret_b64, str):
        raise ValueError("CDP secret is empty")
    raw = base64.b64decode(secret_b64.strip())
    if len(raw) == 64:
        return raw[:32]
    if len(raw) == 32:
        return raw
    raise ValueError("CDP secret must decode to 32 or 64 bytes, got %d" % len(raw))


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_json(obj):
    return _b64url(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def build_cdp_jwt(key_id, secret_b64, method, url, now=None, nonce=None):
    """
    Build a CDP Bearer JWT bound to `method` + the host/path of `url`, valid for
    120 seconds. `now`/`nonce` are injectable for deterministic tests.
    """
    if not key_id:
        raise ValueError("CDP key_id is required")
    seed = cdp_seed_from_secret(secret_b64)
    now = int(time.time()) if now is None else int(now)
    nonce = nonce or os.urandom(8).hex()  # 16 hex chars

    parts = urlsplit(url)
    uri = "%s %s%s" % (method.upper(), parts.netloc, parts.path)

    header = {"alg": "EdDSA", "typ": "JWT", "kid": key_id, "nonce": nonce}
    claims = {"sub": key_id, "iss": "cdp", "aud": ["cdp_service"],
              "nbf": now, "exp": now + 120, "uri": uri}

    signing_input = "%s.%s" % (_b64url_json(header), _b64url_json(claims))
    sig = ed25519_sign(seed, signing_input.encode("ascii"))
    return "%s.%s" % (signing_input, _b64url(sig))
