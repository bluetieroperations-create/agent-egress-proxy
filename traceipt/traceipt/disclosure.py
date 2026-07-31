"""
Selective disclosure: reveal SOME receipt fields and prove the rest is the
issuer's original, without revealing the hidden ones.

A receipt exposes counterparty, amount, and what was bought. Businesses want
confidentiality; auditors and counterparties want proof. Selective disclosure
squares that: at issue time each receipt commits to a Merkle tree over its
individual fields (a `disclosure` block, signed with the receipt and anchored
like the rest). Later the issuer can hand a third party a redacted disclosure
that reveals a chosen subset plus inclusion proofs; the verifier confirms the
revealed values are the issuer's originals while the hidden fields stay hidden.

Blinding: each leaf is salted with HMAC(disclosure_secret, receipt_id | path),
a secret derived from the issuer's signing key. Field VALUES have low entropy
(amounts, addresses), so without a secret salt a verifier could brute-force a
hidden value from its leaf hash -- and the inclusion proofs for revealed fields
necessarily expose the hidden fields' leaf hashes as siblings. The salt defeats
that: it is revealed only for revealed fields; hidden fields' salts (hence
values) stay secret.

Pure + stdlib (uses merkle.py).
"""
from __future__ import annotations

import hashlib
import hmac

from .canonical import canonical_json
from .merkle import inclusion_proof, merkle_root, verify_inclusion

DISCLOSURE_ALG = "merkle-sha256-v1"
DISCLOSURE_TYPE = "receipt-disclosure"

# Never a disclosable leaf: the commitment block itself (would be circular).
_NON_LEAF_TOP_KEYS = {"disclosure"}


def _flatten(obj, prefix=""):
    """Deterministic (path, scalar-value) leaves of a payload. Dicts recurse
    with dotted paths (sorted keys), lists index with [i]; scalars are leaves.
    Sorted order means issuer and verifier build the identical tree."""
    out = []
    if isinstance(obj, dict):
        for k in sorted(obj):
            if prefix == "" and k in _NON_LEAF_TOP_KEYS:
                continue
            child = k if prefix == "" else f"{prefix}.{k}"
            out.extend(_flatten(obj[k], child))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_flatten(v, f"{prefix}[{i}]"))
    else:
        out.append((prefix, obj))
    return out


def _salt(secret: bytes, receipt_id: str, path: str) -> bytes:
    return hmac.new(secret, f"{receipt_id}|{path}".encode("utf-8"),
                    hashlib.sha256).digest()


def _leaf_bytes(path: str, value, salt: bytes) -> bytes:
    """Canonical, typed, salted leaf data for a field."""
    return canonical_json([path, value, salt.hex()])


def field_leaves(payload: dict, secret: bytes):
    """Ordered list of (path, value, salt, leaf_bytes) for a payload. Excludes
    the disclosure block, so it is identical before and after the block is
    added to the payload."""
    rid = payload["receipt_id"]
    leaves = []
    for path, value in _flatten(payload):
        salt = _salt(secret, rid, path)
        leaves.append((path, value, salt, _leaf_bytes(path, value, salt)))
    return leaves


def commit(payload: dict, secret: bytes) -> dict:
    """The `disclosure` block to fold into a receipt payload before signing:
    the field-tree root the receipt commits to, plus its shape."""
    leaves = field_leaves(payload, secret)
    root = merkle_root([lb for (_, _, _, lb) in leaves])
    return {
        "alg": DISCLOSURE_ALG,
        "fields_root": root.hex(),
        "leaf_count": len(leaves),
        "paths": [p for (p, _, _, _) in leaves],
    }


def disclose(payload: dict, secret: bytes, reveal_paths) -> dict:
    """Build a disclosure BODY revealing `reveal_paths`, hiding the rest. The
    issuer signs this (sign_envelope) to make it verifiable. Unknown paths are
    ignored. `fields_root` is taken from the payload's committed disclosure
    block so it always matches the signed/anchored receipt."""
    leaves = field_leaves(payload, secret)
    leaf_bytes = [lb for (_, _, _, lb) in leaves]
    reveal = set(reveal_paths or [])
    revealed = []
    for idx, (path, value, salt, _lb) in enumerate(leaves):
        if path in reveal:
            revealed.append({
                "path": path, "value": value, "salt": salt.hex(),
                "index": idx,
                "audit_path": [h.hex() for h in inclusion_proof(leaf_bytes, idx)],
            })
    committed = (payload.get("disclosure") or {}).get("fields_root")
    return {
        "type": DISCLOSURE_TYPE,
        "alg": DISCLOSURE_ALG,
        "receipt_id": payload["receipt_id"],
        "fields_root": committed or merkle_root(leaf_bytes).hex(),
        "tree_size": len(leaves),
        "revealed": revealed,
    }


def verify_disclosure(body: dict):
    """Verify an AUTHENTICATED disclosure body (the caller has already checked
    the issuer signature via signing.verify_envelope and passes the payload).
    Returns (ok, revealed_dict, problems). Each revealed field must hash into
    the disclosure's fields_root via its inclusion proof; hidden fields are
    never present so they cannot leak."""
    problems = []
    if body.get("alg") != DISCLOSURE_ALG or body.get("type") != DISCLOSURE_TYPE:
        return False, {}, ["not a recognized receipt disclosure"]
    try:
        root = bytes.fromhex(body["fields_root"])
    except Exception:
        return False, {}, ["fields_root is not valid hex"]
    n = body.get("tree_size")
    if not isinstance(n, int) or n <= 0:
        return False, {}, ["tree_size missing or invalid"]
    revealed = {}
    items = body.get("revealed", [])
    for item in items:
        try:
            path = item["path"]
            value = item["value"]
            salt = bytes.fromhex(item["salt"])
            idx = item["index"]
            audit = [bytes.fromhex(h) for h in item["audit_path"]]
        except Exception as e:
            problems.append(f"malformed revealed item: {e}")
            continue
        if verify_inclusion(idx, n, _leaf_bytes(path, value, salt), audit, root):
            revealed[path] = value
        else:
            problems.append(f"field {path!r} failed its inclusion proof")
    ok = (not problems) and len(revealed) == len(items)
    return ok, revealed, problems
