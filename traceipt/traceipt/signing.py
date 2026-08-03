"""
Ed25519 signing of receipts, packaged as a JWS-like envelope.

Envelope shape (all-JSON, no compact serialization, so it stays greppable
and diffable):

    {
      "protected": {"alg": "EdDSA", "kid": "<key id>", "typ": "x402-receipt+json"},
      "payload":   {...receipt...},
      "signature": "<base64url, no padding>"
    }

The signing input is the canonical JSON of {"payload": ..., "protected": ...}
so the algorithm and key id are covered by the signature (no alg-substitution
games). Verification is possible offline with any Ed25519 library given the
issuer's published JWKS.
"""
from __future__ import annotations

import base64
import json
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

from .canonical import canonical_json, sha256_hex

ALG = "EdDSA"
TYP = "x402-receipt+json"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def normalize_pem(pem: bytes) -> bytes:
    """Reconstruct valid PEM framing from a value whose line breaks were lost.

    Pasting a multi-line PEM into a web form (Render env vars, etc.) routinely
    collapses the newlines to spaces or strips them, which makes
    load_pem_private_key raise 'MalformedFraming'. The base64 body is intact --
    only the framing is broken -- so we find the BEGIN/END markers, strip all
    whitespace out of the body, and re-wrap it at 64 columns. Also tolerates
    literal backslash-n ('\\n') left by some secret pipelines. Raises ValueError
    if no PEM markers are present at all."""
    import re
    text = pem.decode("utf-8", "ignore").replace("\\n", "\n").replace("\\r", "")
    m = re.search(r"-----BEGIN ([A-Z0-9 ]+?)-----(.*?)-----END \1-----",
                  text, re.DOTALL)
    if not m:
        raise ValueError("no PEM BEGIN/END markers found in key material")
    label = m.group(1).strip()
    body = "".join(m.group(2).split())  # drop every space/newline in the body
    wrapped = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {label}-----\n{wrapped}\n-----END {label}-----\n".encode()


class Signer:
    """Holds an Ed25519 private key and issues signed envelopes."""

    def __init__(self, private_key: Ed25519PrivateKey):
        self._key = private_key
        pub = self.public_bytes()
        # kid = first 16 hex chars of the sha256 of the raw public key.
        self.kid = sha256_hex(pub)[len("sha256:"):][:16]

    @classmethod
    def generate(cls) -> "Signer":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_pem(cls, pem: bytes, password: bytes | None = None) -> "Signer":
        if isinstance(pem, str):
            pem = pem.encode()
        try:
            key = serialization.load_pem_private_key(pem, password=password)
        except ValueError:
            # Framing likely mangled by a web form (newlines collapsed). Repair
            # the framing and retry once before giving up.
            key = serialization.load_pem_private_key(
                normalize_pem(pem), password=password)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("PEM key is not Ed25519")
        return cls(key)

    def private_pem(self) -> bytes:
        return self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    def public_bytes(self) -> bytes:
        return self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def disclosure_secret(self) -> bytes:
        """A stable 32-byte secret derived from the private key, used to blind
        selective-disclosure field commitments. Deterministic for a given key
        (so disclosures regenerate across restarts with a durable key), and it
        never exposes the private key itself (a one-way hash of it)."""
        import hashlib
        raw = self._key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        return hashlib.sha256(raw + b"traceipt-disclosure-v1").digest()

    def jwk(self) -> dict:
        return {
            "kty": "OKP",
            "crv": "Ed25519",
            "kid": self.kid,
            "x": b64url(self.public_bytes()),
            "use": "sig",
            "alg": ALG,
        }

    def sign_raw(self, data: bytes) -> bytes:
        """Raw Ed25519 signature over arbitrary bytes (for the W3C
        Verifiable Credential data-integrity proof; see vc.py)."""
        return self._key.sign(data)

    def sign_envelope(self, payload: dict) -> dict:
        protected = {"alg": ALG, "kid": self.kid, "typ": TYP}
        signing_input = canonical_json({"payload": payload, "protected": protected})
        sig = self._key.sign(signing_input)
        return {"protected": protected, "payload": payload, "signature": b64url(sig)}


def verify_envelope(envelope: dict, jwks: dict) -> dict:
    """Verify a signed envelope against a JWKS ({"keys": [...]}).

    Returns the payload on success; raises ValueError on any failure.
    Failure modes are distinct strings so callers can report precisely.
    """
    try:
        protected = envelope["protected"]
        payload = envelope["payload"]
        sig = b64url_decode(envelope["signature"])
    except (KeyError, TypeError) as e:
        raise ValueError(f"malformed envelope: {e}")

    if protected.get("alg") != ALG:
        raise ValueError(f"unsupported alg {protected.get('alg')!r}")
    kid = protected.get("kid")
    key_obj = None
    for jwk in jwks.get("keys", []):
        if jwk.get("kid") == kid and jwk.get("kty") == "OKP" and jwk.get("crv") == "Ed25519":
            key_obj = Ed25519PublicKey.from_public_bytes(b64url_decode(jwk["x"]))
            break
    if key_obj is None:
        raise ValueError(f"no Ed25519 key with kid {kid!r} in JWKS")

    signing_input = canonical_json({"payload": payload, "protected": protected})
    try:
        key_obj.verify(sig, signing_input)
    except InvalidSignature:
        raise ValueError("signature verification FAILED")
    return payload


def load_or_create_signer(path: str) -> Signer:
    """Load the issuer key from a PEM file, creating it on first run."""
    import os

    if os.path.exists(path):
        with open(path, "rb") as f:
            return Signer.from_pem(f.read())
    signer = Signer.generate()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(signer.private_pem())
    return signer
