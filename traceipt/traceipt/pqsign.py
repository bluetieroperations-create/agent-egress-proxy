"""Post-quantum signatures (ML-DSA-65 / FIPS 204) as an OPTIONAL hybrid layer.

Traceipt receipts are audit artifacts kept for years, so the promise is "still
un-forgeable a decade from now." The on-chain Merkle anchor is SHA-256 and thus
already quantum-sound; the one quantum-exposed piece is the Ed25519 envelope
signature. This module adds a SECOND signature (ML-DSA-65) alongside Ed25519 so
a receipt survives even if elliptic-curve signatures fall to a quantum attacker.
Ed25519 stays primary (today's verifiers), ML-DSA is the forward hedge.

`dilithium-py` is an OPTIONAL dependency (pure Python, no native build):
    pip install -r requirements-pqc.txt
If it is not installed, hybrid signing is simply unavailable and Traceipt runs
exactly as before (Ed25519 only). Nothing here is imported unless a PQ key is
actually configured.
"""
from __future__ import annotations

import hashlib
import struct

PQ_ALG = "ML-DSA-65"


def _ml_dsa():
    from dilithium_py.ml_dsa import ML_DSA_65  # lazy: optional dependency
    return ML_DSA_65


def pq_available() -> bool:
    try:
        _ml_dsa()
        return True
    except Exception:  # noqa: BLE001
        return False


def pq_generate() -> tuple[bytes, bytes]:
    """A fresh ML-DSA-65 keypair as (public_key, secret_key) bytes."""
    return _ml_dsa().keygen()


def pq_sign(secret_key: bytes, message: bytes) -> bytes:
    return _ml_dsa().sign(secret_key, message)


def pq_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    try:
        return bool(_ml_dsa().verify(public_key, message, signature))
    except Exception:  # noqa: BLE001 -- malformed key/sig is a failed verify, not a crash
        return False


def pq_kid(public_key: bytes) -> str:
    """Short key id: first 16 hex of sha256(public key). Mirrors the Ed25519 kid."""
    return hashlib.sha256(public_key).hexdigest()[:16]


def pack_keypair(public_key: bytes, secret_key: bytes) -> str:
    """Serialize (pk, sk) to a single base64 string for the RECEIPTS_PQ_KEY
    secret. Length-prefixed so it survives any future parameter-set change."""
    import base64
    blob = struct.pack(">I", len(public_key)) + public_key + secret_key
    return base64.b64encode(blob).decode("ascii")


def unpack_keypair(material: str) -> tuple[bytes, bytes]:
    """Inverse of pack_keypair. Returns (pk, sk)."""
    import base64
    blob = base64.b64decode(material.strip())
    (pk_len,) = struct.unpack(">I", blob[:4])
    pk = blob[4:4 + pk_len]
    sk = blob[4 + pk_len:]
    if not pk or not sk:
        raise ValueError("RECEIPTS_PQ_KEY does not decode to a (pk, sk) pair")
    return pk, sk
