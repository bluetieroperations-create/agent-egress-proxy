"""Ed25519-signed receipt envelopes -- the "independently verifiable" half.

WHY THIS EXISTS
---------------
`blackwall.sign_receipt` returns an HMAC-SHA256 `receipt_id`. That is a fine
audit-trail identifier and a fine ledger join key, but it is SYMMETRIC: verifying
it needs the secret, so only Blackwall can check it and anyone handed the secret
can forge it. Blackwall nonetheless advertised "an independently-verifiable
Ed25519 signed receipt". This module makes that true.

The envelope is byte-compatible with Traceipt's (`traceipt/traceipt/signing.py`)
so ONE verifier covers both products -- `clients/traceipt-verify` already
verifies exactly this shape:

    protected     {"alg": "EdDSA", "kid": "<16 hex>", "typ": "x402-receipt+json"}
    signing input canonical_json({"payload": ..., "protected": ...})
    envelope      {"protected": ..., "payload": ..., "signature": b64url(sig)}

STDLIB ONLY. Traceipt signs with the `cryptography` package; this engine cannot.
`cdp_auth.ed25519_sign` implements RFC 8032 from hashlib alone and is checked
against the RFC test vectors. It is deliberately SIGN-ONLY, which is exactly this
role: Blackwall SIGNS receipts, third parties VERIFY them, and the engine never
verifies its own output.

KEY HANDLING -- the part that must not be sloppy
------------------------------------------------
1. **No dev-key fallback.** `blackwall._DEV_RECEIPT_KEY` exists so the HMAC path
   works out of the box. There is deliberately no equivalent here: with no seed
   configured, `ReceiptSigner.available` is False and `forecast` simply omits the
   `receipt` field. A receipt signed with a committed key is WORSE than no
   receipt, because it LOOKS verifiable.
2. **Fail CLOSED and LOUD on a malformed seed.** If the operator set the variable
   they intended signing; silently degrading to unsigned would ship a service that
   looks configured and is not. `load_seed` raises, and the caller surfaces it at
   boot (the precedent is commit cc60fe0, which made a forgeable receipt key loud).
3. **Never reuse the HMAC secret.** `BLACKWALL_RECEIPT_KEY` is an HMAC secret with
   different exposure; using it as an Ed25519 seed is refused explicitly.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

ALG = "EdDSA"
TYP = "x402-receipt+json"
KTY = "OKP"
CRV = "Ed25519"

#: Ed25519 seed (32 bytes, base64url). Deliberately NOT BLACKWALL_RECEIPT_KEY.
ENV_SEED = "BLACKWALL_SIGNING_SEED"
#: The HMAC secret, named here only so it can be REFUSED as a signing seed.
ENV_HMAC = "BLACKWALL_RECEIPT_KEY"

SEED_BYTES = 32
KID_HEX_CHARS = 16


def b64url(data):
    """Unpadded base64url, per JOSE."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(text):
    """Decode unpadded base64url. Raises on malformed input."""
    if not isinstance(text, str):
        raise ValueError("expected a base64url string")
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _reject_floats(obj):
    """Floats have no canonical JSON form -- two runs can differ in the last
    digit, which would make a signature unverifiable. Reject rather than sign
    something that may not round-trip. bool is checked before int (bool IS int).
    """
    if isinstance(obj, float):
        raise ValueError("floats are not canonicalizable; use a decimal string")
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise ValueError("object keys must be strings")
            _reject_floats(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _reject_floats(value)


def canonical_json(obj):
    """Canonical JSON bytes. Must match Traceipt's byte-for-byte, or a receipt
    signed here will not verify with `clients/traceipt-verify`.
    """
    _reject_floats(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def derive_kid(public_key):
    """Key id = first 16 hex chars of sha256(raw public key). Matches Traceipt."""
    if not isinstance(public_key, (bytes, bytearray)) or len(public_key) != 32:
        raise ValueError("public key must be 32 raw bytes")
    return hashlib.sha256(bytes(public_key)).hexdigest()[:KID_HEX_CHARS]


def public_jwk(public_key, kid=None):
    """The OKP/Ed25519 JWK a verifier imports."""
    return {"kty": KTY, "crv": CRV, "alg": ALG, "use": "sig",
            "kid": kid or derive_kid(public_key), "x": b64url(bytes(public_key))}


def load_seed(environ=None):
    """Read the signing seed. Returns None when unset; RAISES when set-but-bad.

    The distinction is the whole point: unset means "signing not configured, omit
    the receipt", while set-but-bad means the operator INTENDED signing and the
    service must not come up pretending to be configured.
    """
    environ = os.environ if environ is None else environ
    raw = (environ.get(ENV_SEED) or "").strip()
    if not raw:
        return None

    hmac_secret = (environ.get(ENV_HMAC) or "").strip()
    if hmac_secret and raw == hmac_secret:
        raise ValueError(
            "%s must not be the same value as %s -- the HMAC secret has different "
            "exposure and must never be used as an Ed25519 seed"
            % (ENV_SEED, ENV_HMAC))

    try:
        seed = b64url_decode(raw)
    except Exception as exc:
        raise ValueError("%s is not valid base64url: %s" % (ENV_SEED, exc))
    if len(seed) != SEED_BYTES:
        raise ValueError("%s must decode to %d bytes, got %d"
                         % (ENV_SEED, SEED_BYTES, len(seed)))
    if not any(seed):
        raise ValueError("%s is all zero bytes -- refusing to sign with it"
                         % ENV_SEED)
    return seed


def build_protected(kid):
    return {"alg": ALG, "kid": kid, "typ": TYP}


def signing_input(payload, protected):
    """The exact bytes that get signed. Traceipt signs the SAME structure, so
    keep the key order/shape identical -- canonical_json sorts, so what matters
    is that both sides wrap {"payload":..., "protected":...}.
    """
    return canonical_json({"payload": payload, "protected": protected})


#: Fields the signed receipt attests to. Deliberately NOT the whole verdict:
#: `signals` is a large, version-unstable blob carrying floats, and signing it
#: would make every receipt depend on internal scoring shape. What a third party
#: needs to check is WHAT was decided about WHICH request.
CLAIM_FIELDS = ("receipt_id", "verdict", "hard_stop", "counterparty", "amount",
                "asset", "chain", "agent_id", "score", "reasons")


def build_claims(verdict, request=None):
    """Pure: the canonicalizable claim set to sign.

    `score` is emitted as a DECIMAL STRING. It is a float internally, and floats
    have no canonical JSON form -- signing one risks a receipt that will not
    re-verify on another platform. Amounts are already strings everywhere in this
    codebase for the same reason.
    """
    source = dict(request or {})
    source.update(verdict or {})
    claims = {}
    for field in CLAIM_FIELDS:
        if field not in source:
            continue
        value = source[field]
        if field == "score" and isinstance(value, (int, float)) \
                and not isinstance(value, bool):
            value = "%.6f" % float(value)
        claims[field] = value
    return claims


class ReceiptSigner:
    """Signs verdict payloads. `available` is False when no seed is configured."""

    def __init__(self, seed=None, environ=None, retired_public_keys=()):
        self._seed = seed if seed is not None else load_seed(environ)
        self.retired = [bytes(k) for k in retired_public_keys or ()]
        if self._seed is None:
            self.public_key = None
            self.kid = None
        else:
            from cdp_auth import ed25519_publickey
            self.public_key = ed25519_publickey(self._seed)
            self.kid = derive_kid(self.public_key)

    @property
    def available(self):
        return self._seed is not None

    def sign(self, payload):
        """Return a signed envelope, or None when signing is not configured.

        None (not an exception) because an unsigned verdict is still a valid
        verdict -- the caller omits the field rather than failing the request.
        """
        if not self.available:
            return None
        from cdp_auth import ed25519_sign
        protected = build_protected(self.kid)
        signature = ed25519_sign(self._seed, signing_input(payload, protected))
        return {"protected": protected, "payload": payload,
                "signature": b64url(signature)}

    def jwks(self):
        """Active key first, then retired keys.

        Retired keys stay published on purpose: a receipt signed last year must
        remain verifiable after a rotation, and dropping the old key silently
        invalidates every receipt already in a customer's audit trail.
        """
        keys = []
        seen = set()
        if self.public_key is not None:
            jwk = public_jwk(self.public_key, self.kid)
            keys.append(jwk)
            seen.add(jwk["kid"])
        for raw in self.retired:
            jwk = public_jwk(raw)
            if jwk["kid"] not in seen:
                keys.append(jwk)
                seen.add(jwk["kid"])
        return {"keys": keys}
