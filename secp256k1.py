#!/usr/bin/env python3
"""
secp256k1.py -- pure-Python secp256k1 with ECDSA public-key RECOVERY (ecrecover).

Needed for payload-simulation Phase 2: recover the signer of an EIP-3009
`transferWithAuthorization` from its signature and confirm it is the claimed payer.
Same posture as cdp_auth.py's pure-Python Ed25519 -- stdlib only, no third-party
crypto. Curve math is affine (clear over fast); recovery is the one operation the
verdict path needs. `ecdsa_sign` is included ONLY to make the tests self-contained
(round-trip) without eth-account; the verdict path never signs.

Anchored by published vectors: privkey 1 -> pubkey the well-known secp256k1 G, and
the derived Ethereum addresses for privkeys 1/2 (see test_secp256k1.py).
"""
from __future__ import annotations

import hashlib
import hmac

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F   # field prime
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141   # group order
A = 0
B = 7
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
G = (GX, GY)


def _inv(x, m):
    return pow(x % m, -1, m)


def is_on_curve(point):
    if point is None:
        return True                      # point at infinity
    x, y = point
    return (y * y - (x * x * x + A * x + B)) % P == 0


def point_add(p1, p2):
    """Affine addition on secp256k1. None is the point at infinity."""
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return None                      # P + (-P) = O
    if p1 == p2:
        m = (3 * x1 * x1 + A) * _inv(2 * y1, P) % P
    else:
        m = (y2 - y1) * _inv((x2 - x1) % P, P) % P
    x3 = (m * m - x1 - x2) % P
    y3 = (m * (x1 - x3) - y1) % P
    return (x3, y3)


def scalar_mult(k, point):
    """k * point via double-and-add (iterative)."""
    if k % N == 0 or point is None:
        return None
    if k < 0:
        return scalar_mult(-k, (point[0], (-point[1]) % P))
    result = None
    addend = point
    while k:
        if k & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        k >>= 1
    return result


def _sqrt_mod_p(a):
    """Square root mod P (P % 4 == 3, so a^((P+1)/4)); None if not a QR."""
    r = pow(a % P, (P + 1) // 4, P)
    return r if (r * r) % P == a % P else None


def ecdsa_recover(z, r, s, rec_id):
    """Recover the public key point (x, y) that signed digest `z` with signature
    (r, s) and recovery id `rec_id` (0..3; Ethereum's v is 27/28 -> 0/1, or the
    EIP-155 form -- normalize before calling). Returns the point or None if the
    signature/rec_id is invalid. NEVER raises.

    Q = r^-1 (s*R - z*G), where R is the curve point whose x is r (+N if rec_id>=2)
    and whose y-parity is rec_id & 1.
    """
    try:
        if not (0 <= rec_id <= 3):
            return None
        if not (1 <= r < N and 1 <= s < N):     # canonical range
            return None
        # x-coordinate of R (rec_id bit 1 = "r was reduced mod N", so add N back)
        x = r + N if (rec_id >> 1) else r
        if x >= P:
            return None
        # y from the curve, with parity chosen by rec_id bit 0
        y2 = (x * x * x + A * x + B) % P
        y = _sqrt_mod_p(y2)
        if y is None:
            return None
        if (y & 1) != (rec_id & 1):
            y = P - y
        R = (x, y)
        # R is on the curve by construction (y is a sqrt of x^3+7). secp256k1 has
        # prime order N and cofactor 1, so every on-curve point is in the group --
        # no separate subgroup check needed (and it would cost a full scalar mult).
        r_inv = _inv(r, N)
        # Q = r^-1 (s*R - z*G)
        sR = scalar_mult(s, R)
        zG = scalar_mult(z % N, G)
        neg_zG = None if zG is None else (zG[0], (-zG[1]) % P)
        Q = scalar_mult(r_inv, point_add(sR, neg_zG))
        if Q is None or not is_on_curve(Q):
            return None
        return Q
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Test-support only (the verdict path never signs). Deterministic ECDSA per
# RFC 6979 so round-trip tests are stable without Date/random.
# ---------------------------------------------------------------------------
def privkey_to_pub(d):
    """d * G -> public key point (test support / address derivation)."""
    return scalar_mult(d, G)


def _rfc6979_k(z, d):
    """Deterministic nonce k per RFC 6979 (HMAC-SHA256), for test signing."""
    x = d.to_bytes(32, "big")
    h1 = z.to_bytes(32, "big")
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < N:
            return cand
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def ecdsa_sign(z, d):
    """Deterministic ECDSA sign -> (r, s, rec_id) with low-s (Ethereum). Test-only."""
    k = _rfc6979_k(z, d)
    R = scalar_mult(k, G)
    r = R[0] % N
    s = (_inv(k, N) * (z + r * d)) % N
    rec_id = (R[1] & 1) ^ (0 if s * 2 < N else 1)
    if s * 2 >= N:                              # enforce low-s, flip parity
        s = N - s
    return r, s, rec_id
