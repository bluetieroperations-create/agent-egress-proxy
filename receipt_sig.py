"""
receipt_sig.py -- public-key (Ed25519) signatures for Black_Wall receipts.

WHY THIS EXISTS
---------------
Black_Wall's `receipt_id` (see blackwall.sign_receipt) is a keyed HMAC: it is
tamper-evident *to us* and drives the private `report_token` capability and the
ledger join key. But an HMAC is signed with a SECRET only we hold, so a third
party cannot verify what Black_Wall attested without our key -- it is a private
audit trail, not a neutral, independently-checkable proof.

This module adds an Ed25519 signature over a canonical receipt object so ANY
party can verify -- with only our PUBLIC key -- exactly what verdict Black_Wall
returned, with Black_Wall out of the loop and without trusting us. That is the
property the neutrality story rests on: a verdict is only as neutral as it is
verifiable by someone who does not trust the issuer.

We do NOT roll our own crypto: signing/verification is delegated to PyNaCl
(libsodium bindings). This is the one deliberate server dependency; it is
confined to this module. The signing seed is a 32-byte secret
(BLACKWALL_SIGNING_SEED, hex); the public key is published at
/.well-known/blackwall-receipt-key.json so verifiers can fetch and pin it.

CANONICALIZATION (the signed message)
-------------------------------------
The signed message is `canonical_bytes(body)` where body is the signed-receipt
object MINUS its own `sig` field: JSON with sorted keys, no whitespace, UTF-8.
A verifier reconstructs it identically. Numeric identity fields (amount) are
strings to avoid cross-language float ambiguity; `score` is our own float and
is documented as requiring canonical-JSON number formatting to verify.

LIMITATIONS (not fixed here, stated honestly)
---------------------------------------------
- Only the DECISION is signed (verdict, score, counterparty, amount, asset,
  chain, receipt_id, ts). The advisory `reasons`/`signals` are NOT in the signed
  envelope -- they travel unsigned in the response.
- A real public-key signature buys cryptographic verifiability, not
  independence: a signer everyone relies on is still a capture target. Neutrality
  is a governance property on top of this primitive, not a property of it.
"""
import hashlib
import json
import os

from nacl.signing import SigningKey, VerifyKey
from nacl.exceptions import BadSignatureError

# Dev seed -- NEVER for production. A real deploy supplies BLACKWALL_SIGNING_SEED.
# All-zero seed is unmistakably a placeholder; its public key is well-known so no
# one can be fooled into trusting a dev-signed receipt as production.
_DEV_SEED_HEX = "00" * 32

SIGNED_RECEIPT_VERSION = 1


def canonical_bytes(obj):
    """Deterministic JSON encoding used as the signed message: sorted keys, no
    whitespace, UTF-8. Two dicts with equal content encode to equal bytes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def load_seed(seed_hex=None):
    """Return the 32-byte Ed25519 seed. Explicit arg > env > dev seed."""
    if seed_hex is None:
        seed_hex = os.environ.get("BLACKWALL_SIGNING_SEED", "") or _DEV_SEED_HEX
    try:
        seed = bytes.fromhex(seed_hex)
    except (ValueError, TypeError):
        raise ValueError("BLACKWALL_SIGNING_SEED must be valid hex")
    if len(seed) != 32:
        raise ValueError("BLACKWALL_SIGNING_SEED must be 32 bytes (64 hex chars)")
    return seed


def _signing_key(seed_hex=None):
    return SigningKey(load_seed(seed_hex))


def public_key_hex(seed_hex=None):
    """Hex of the Ed25519 public (verify) key for the given/active seed."""
    return _signing_key(seed_hex).verify_key.encode().hex()


def is_dev_key(seed_hex=None):
    """True iff the ACTIVE seed is the built-in dev seed. When true, receipts are
    FORGEABLE (the seed is public) -- production must set BLACKWALL_SIGNING_SEED.
    Surfaced at /.well-known so operators and verifiers can detect a misconfig."""
    if seed_hex is None:
        seed_hex = os.environ.get("BLACKWALL_SIGNING_SEED", "") or _DEV_SEED_HEX
    try:
        return bytes.fromhex(seed_hex) == bytes.fromhex(_DEV_SEED_HEX)
    except (ValueError, TypeError):
        return False


def key_id(pubkey_hex):
    """Short stable id for a public key so verifiers can pin/rotate keys:
    first 16 hex chars of sha256(pubkey_bytes)."""
    return hashlib.sha256(bytes.fromhex(pubkey_hex)).hexdigest()[:16]


def sign_obj(obj, seed_hex=None):
    """Ed25519-sign the canonical encoding of `obj`; return a hex signature."""
    return _signing_key(seed_hex).sign(canonical_bytes(obj)).signature.hex()


def verify_obj(obj, sig_hex, pubkey_hex):
    """True iff sig_hex is a valid Ed25519 signature by pubkey_hex over the
    canonical encoding of obj. Never raises on bad input -- returns False."""
    try:
        VerifyKey(bytes.fromhex(pubkey_hex)).verify(
            canonical_bytes(obj), bytes.fromhex(sig_hex))
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False


def _body_without_sig(obj):
    return {k: v for k, v in obj.items() if k != "sig"}


def build_signed_receipt(*, receipt_id, counterparty, amount, asset, chain,
                         verdict, score, ts, seed_hex=None):
    """Assemble the canonical signed-receipt object and attach `sig` + `key_id`.
    This is the object a third party verifies. Only the attested decision and
    identity are signed -- internal fields (nonce, signals) are excluded."""
    pub = public_key_hex(seed_hex)
    body = {
        "v": SIGNED_RECEIPT_VERSION,
        "receipt_id": receipt_id,
        "counterparty": counterparty,
        "amount": str(amount),
        "asset": asset,
        "chain": chain,
        "verdict": verdict,
        "score": score,
        "ts": ts,
        "key_id": key_id(pub),
    }
    body["sig"] = sign_obj(body, seed_hex)  # sign_obj sees no `sig` key yet
    return body


def verify_signed_receipt(signed_receipt, pubkey_hex):
    """Verify a signed_receipt produced by build_signed_receipt against a public
    key. Returns False (never raises) on any malformed input or bad signature."""
    if not isinstance(signed_receipt, dict) or "sig" not in signed_receipt:
        return False
    sig = signed_receipt.get("sig")
    if not isinstance(sig, str):
        return False
    return verify_obj(_body_without_sig(signed_receipt), sig, pubkey_hex)
