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


def load_private_key_flexible(material: bytes, password: bytes | None = None):
    """Load an Ed25519 private key from key material that may have survived a
    hostile env-var round-trip. Tries, in order:

      1. PEM exactly as given;
      2. PEM with repaired framing (newlines collapsed -- see normalize_pem);
      3. single-line base64 of the DER (or of a whole PEM). This also covers the
         common case where ONLY the base64 body of a PEM was pasted (no
         BEGIN/END wrapper) -- that body IS base64-of-DER, so it loads directly.

    Base64 (single line, no newlines) is the paste-safe way to carry a key in an
    env var, so we accept it as a first-class format. Raises ValueError with the
    tried strategies if none work."""
    from cryptography.hazmat.primitives.serialization import (
        load_der_private_key, load_pem_private_key,
    )
    errors = []
    try:
        return load_pem_private_key(material, password=password)
    except Exception as e:  # noqa: BLE001
        errors.append(f"pem={type(e).__name__}")
    try:
        return load_pem_private_key(normalize_pem(material), password=password)
    except Exception as e:  # noqa: BLE001
        errors.append(f"repaired-pem={type(e).__name__}")
    try:
        raw = base64.b64decode("".join(
            material.decode("utf-8", "ignore").split()), validate=False)
        if raw.lstrip().startswith(b"-----BEGIN"):
            return load_pem_private_key(raw, password=password)
        return load_der_private_key(raw, password=password)
    except Exception as e:  # noqa: BLE001
        errors.append(f"base64/der={type(e).__name__}")
    raise ValueError("could not load an Ed25519 private key from the provided "
                     "material (tried " + ", ".join(errors) + ")")


class Signer:
    """Holds an Ed25519 private key and issues signed envelopes. Optionally also
    holds an ML-DSA-65 keypair, in which case every envelope carries a second,
    post-quantum signature alongside the Ed25519 one (see load_pq / pqsign.py)."""

    def __init__(self, private_key: Ed25519PrivateKey):
        self._key = private_key
        pub = self.public_bytes()
        # kid = first 16 hex chars of the sha256 of the raw public key.
        self.kid = sha256_hex(pub)[len("sha256:"):][:16]
        self._pq_pk: bytes | None = None
        self._pq_sk: bytes | None = None
        self.pq_kid: str | None = None

    @classmethod
    def generate(cls) -> "Signer":
        return cls(Ed25519PrivateKey.generate())

    def load_pq(self, material: str) -> "Signer":
        """Attach an ML-DSA-65 keypair (packed base64, from pqsign.pack_keypair /
        `--gen-pq-key`) so envelopes get a hybrid post-quantum signature. Raises
        if the PQ library isn't installed or the material is malformed."""
        from . import pqsign
        if not pqsign.pq_available():
            raise ValueError("post-quantum signing requires dilithium-py "
                             "(pip install -r requirements-pqc.txt)")
        self._pq_pk, self._pq_sk = pqsign.unpack_keypair(material)
        self.pq_kid = pqsign.pq_kid(self._pq_pk)
        return self

    @property
    def has_pq(self) -> bool:
        return self._pq_sk is not None

    def pq_jwk(self) -> dict | None:
        """The public ML-DSA key as a JWK-shaped object for /jwks.json. Uses
        kty 'MLDSA' with the raw public key in `pub` (there is no ratified JWK
        type for ML-DSA yet; this is self-consistent with our own verifier)."""
        if self._pq_pk is None:
            return None
        from . import pqsign
        return {"kty": "MLDSA", "alg": pqsign.PQ_ALG, "kid": self.pq_kid,
                "pub": b64url(self._pq_pk), "use": "sig"}

    @classmethod
    def from_pem(cls, pem: bytes, password: bytes | None = None) -> "Signer":
        """Load from PEM, repaired PEM, or single-line base64/DER -- whatever
        survived the env-var round-trip (see load_private_key_flexible)."""
        if isinstance(pem, str):
            pem = pem.encode()
        key = load_private_key_flexible(pem, password=password)
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
        if self.has_pq:
            # Declare the PQ alg/kid INSIDE protected so BOTH signatures cover
            # them (no downgrade games), then sign the same input with each.
            from . import pqsign
            protected["pq_alg"] = pqsign.PQ_ALG
            protected["pq_kid"] = self.pq_kid
        signing_input = canonical_json({"payload": payload, "protected": protected})
        sig = self._key.sign(signing_input)
        env = {"protected": protected, "payload": payload, "signature": b64url(sig)}
        if self.has_pq:
            from . import pqsign
            env["pq_signature"] = b64url(pqsign.pq_sign(self._pq_sk, signing_input))
        return env


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

    # Hybrid post-quantum check: if the envelope carries an ML-DSA signature AND
    # the matching PQ key is published AND the PQ library is available, it must
    # also verify (a present-but-invalid PQ signature is tampering). If the key
    # isn't published or the library is absent, we cannot check it -- the
    # Ed25519 signature already passed, so we proceed rather than fail.
    pq_alg = protected.get("pq_alg")
    pq_sig_b64 = envelope.get("pq_signature")
    if pq_alg and pq_sig_b64:
        pq_kid = protected.get("pq_kid")
        pq_jwk = next((k for k in jwks.get("keys", [])
                       if k.get("kty") == "MLDSA" and k.get("kid") == pq_kid), None)
        if pq_jwk is not None:
            try:
                from . import pqsign
                available = pqsign.pq_available()
            except Exception:  # noqa: BLE001
                available = False
            if available:
                pk = b64url_decode(pq_jwk["pub"])
                if not pqsign.pq_verify(pk, signing_input, b64url_decode(pq_sig_b64)):
                    raise ValueError("post-quantum signature verification FAILED")
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
