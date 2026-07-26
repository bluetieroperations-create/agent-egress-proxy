"""
RFC 6962 Merkle tree — for batch-anchoring receipts.

Anchoring lets the issuer periodically publish a single 32-byte Merkle root
(cheaply, e.g. one on-chain transaction per thousands of receipts). Each
receipt then carries an inclusion proof against that root, so a third party
can prove a receipt existed by the anchor's timestamp WITHOUT trusting the
issuer's database — the certificate-transparency pattern.

RFC 6962 domain separation defeats the classic second-preimage attack:
  leaf  hash = SHA256(0x00 || data)
  node  hash = SHA256(0x01 || left || right)
so a leaf can never be reinterpreted as an internal node.

Everything here is pure functions over bytes; the ledger stores the results.
"""
from __future__ import annotations

import hashlib


def leaf_hash(data: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + data).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _largest_power_of_two_below(n: int) -> int:
    """Largest power of two strictly less than n (n >= 2)."""
    k = 1
    while k * 2 < n:
        k *= 2
    return k


def merkle_root(leaves: list[bytes]) -> bytes:
    """RFC 6962 Merkle Tree Hash over a list of leaf DATA blobs."""
    n = len(leaves)
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return leaf_hash(leaves[0])
    k = _largest_power_of_two_below(n)
    return node_hash(merkle_root(leaves[:k]), merkle_root(leaves[k:]))


def inclusion_proof(leaves: list[bytes], m: int) -> list[bytes]:
    """Audit path for leaf index m: sibling hashes ordered leaf-adjacent
    first (RFC 6962 convention)."""
    n = len(leaves)
    if not 0 <= m < n:
        raise IndexError("leaf index out of range")
    if n == 1:
        return []
    k = _largest_power_of_two_below(n)
    if m < k:
        return inclusion_proof(leaves[:k], m) + [merkle_root(leaves[k:])]
    return inclusion_proof(leaves[k:], m - k) + [merkle_root(leaves[:k])]


def root_from_inclusion(m: int, n: int, leaf: bytes, proof: list[bytes]) -> bytes:
    """Recompute the root from a leaf and its audit path (RFC 6962 §2.1.1
    verification). `leaf` is the raw leaf DATA (we leaf-hash it here)."""
    if not 0 <= m < n:
        raise IndexError("leaf index out of range")
    fn, sn = m, n - 1
    r = leaf_hash(leaf)
    for sibling in proof:
        if fn == sn or (fn & 1):
            r = node_hash(sibling, r)
            if not (fn & 1):
                while fn != 0 and not (fn & 1):
                    fn >>= 1
                    sn >>= 1
        else:
            r = node_hash(r, sibling)
        fn >>= 1
        sn >>= 1
    return r


def verify_inclusion(m: int, n: int, leaf: bytes, proof: list[bytes],
                     root: bytes) -> bool:
    try:
        return root_from_inclusion(m, n, leaf, proof) == root
    except IndexError:
        return False
