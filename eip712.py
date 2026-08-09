#!/usr/bin/env python3
"""
eip712.py -- EIP-712 typed-data hashing for EIP-3009 transferWithAuthorization,
plus Ethereum address derivation. Pure-Python (keccak.py), stdlib only.

Used by payload-simulation Phase 2 to reconstruct the exact digest a wallet signed
for a `transferWithAuthorization` and recover the signer (secp256k1.ecdsa_recover),
confirming it is the claimed payer. Because the EIP-712 DOMAIN commits to `chainId`
and `verifyingContract` (the token), a valid recovery ALSO binds the chain and the
asset -- closing the Phase-1 gap where those were self-declared metadata.

Digest = keccak256( 0x1901 || domainSeparator || structHash ), the standard
EIP-712 encoding. Field encodings: strings -> keccak256(utf8); uint256 -> 32-byte
big-endian; address -> 20 bytes left-padded to 32; bytes32 -> the 32 bytes.
"""
from __future__ import annotations

from keccak import keccak256

_DOMAIN_TYPE = (b"EIP712Domain(string name,string version,uint256 chainId,"
                b"address verifyingContract)")
_TWA_TYPE = (b"TransferWithAuthorization(address from,address to,uint256 value,"
             b"uint256 validAfter,uint256 validBefore,bytes32 nonce)")
_DOMAIN_TYPEHASH = keccak256(_DOMAIN_TYPE)
_TWA_TYPEHASH = keccak256(_TWA_TYPE)


def _to_int(v):
    """Accept an int or a decimal/0x-hex string -> int (raises on garbage)."""
    if isinstance(v, int):
        return v
    s = str(v).strip()
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def enc_uint(v) -> bytes:
    n = _to_int(v)
    if n < 0 or n >= (1 << 256):
        raise ValueError("uint256 out of range")
    return n.to_bytes(32, "big")


def enc_address(a) -> bytes:
    """A 0x address (or int) -> 32-byte left-padded word."""
    n = a if isinstance(a, int) else int(str(a).strip(), 16)
    if n < 0 or n >= (1 << 160):
        raise ValueError("address out of range")
    return n.to_bytes(32, "big")


def enc_bytes32(v) -> bytes:
    """A 0x...(32-byte) hex string or bytes -> exactly 32 bytes."""
    if isinstance(v, (bytes, bytearray)):
        b = bytes(v)
    else:
        s = str(v).strip()
        if s.lower().startswith("0x"):
            s = s[2:]
        b = bytes.fromhex(s)
    if len(b) != 32:
        raise ValueError("bytes32 must be exactly 32 bytes")
    return b


def domain_separator(name, version, chain_id, verifying_contract) -> bytes:
    """EIP-712 domainSeparator for {name, version, chainId, verifyingContract}."""
    return keccak256(
        _DOMAIN_TYPEHASH
        + keccak256(name.encode("utf-8") if isinstance(name, str) else bytes(name))
        + keccak256(version.encode("utf-8") if isinstance(version, str) else bytes(version))
        + enc_uint(chain_id)
        + enc_address(verifying_contract))


def transfer_authorization_struct_hash(message) -> bytes:
    """keccak256 of the encoded TransferWithAuthorization struct.
    `message` = {from, to, value, validAfter, validBefore, nonce}."""
    return keccak256(
        _TWA_TYPEHASH
        + enc_address(message["from"])
        + enc_address(message["to"])
        + enc_uint(message["value"])
        + enc_uint(message["validAfter"])
        + enc_uint(message["validBefore"])
        + enc_bytes32(message["nonce"]))


def transfer_authorization_digest(domain, message) -> int:
    """The EIP-712 digest (as int, for ecdsa_recover) a wallet signs for a
    transferWithAuthorization. `domain` = {name, version, chainId, verifyingContract}.
    Raises on malformed fields (caller treats a raise as 'cannot verify')."""
    ds = domain_separator(domain["name"], domain["version"],
                          domain["chainId"], domain["verifyingContract"])
    sh = transfer_authorization_struct_hash(message)
    return int.from_bytes(keccak256(b"\x19\x01" + ds + sh), "big")


def pubkey_to_address(point) -> str:
    """secp256k1 public-key point (x, y) -> lowercase 0x Ethereum address."""
    x, y = point
    raw = x.to_bytes(32, "big") + y.to_bytes(32, "big")
    return "0x" + keccak256(raw)[-20:].hex()


def split_signature(sig):
    """65-byte signature (0x hex or bytes) -> (r, s, rec_id). `v` may be 27/28
    (legacy), 0/1, or EIP-155; normalized to rec_id in 0..3. None on bad length."""
    if isinstance(sig, str):
        s = sig[2:] if sig.lower().startswith("0x") else sig
        try:
            sig = bytes.fromhex(s)
        except ValueError:
            return None
    if not isinstance(sig, (bytes, bytearray)) or len(sig) != 65:
        return None
    r = int.from_bytes(sig[0:32], "big")
    s = int.from_bytes(sig[32:64], "big")
    v = sig[64]
    if v >= 35:                 # EIP-155: v = 35 + 2*chainId + rec
        rec = (v - 35) & 1
    elif v >= 27:
        rec = v - 27
    else:
        rec = v
    return r, s, rec
