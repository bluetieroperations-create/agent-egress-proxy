"""Rebuild the anchor index from the public chain.

Traceipt publishes every Merkle root on-chain in the calldata of a 0-value
self-send from the gas wallet, tagged with a `TRACEIPT-ANCHOR\\x01` marker
(see publisher.py). That makes the set of published roots a PUBLIC, permanent
fact -- anyone can enumerate the gas wallet's transactions and read the roots
back out.

So a disk-less instance whose DB reset does NOT actually lose its roots: this
module re-derives (root, tx, timestamp) for every anchor straight from the
chain, so `/anchors` self-heals and any inclusion proof a caller presents can
be checked against an on-chain-confirmed root -- with no trusted database and
no persistent disk.

The leaf->position mapping inside each batch is NOT on-chain (only the root is),
so recovered anchors carry the root and its on-chain tx but not the original
leaves; that is exactly what is needed to answer "is this root anchored, and
when?" -- the verifier supplies the leaf + audit path.

The explorer transport is injectable, so this is testable with no network.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

MARKER_HEX = b"TRACEIPT-ANCHOR\x01".hex()  # 54524143454950542d414e43484f5201

# Etherscan-v2-style explorer endpoints that expose account/txlist for free.
EXPLORERS = {
    "base": "https://api.basescan.org/api",
    "base-sepolia": "https://api-sepolia.basescan.org/api",
}


def _basescan_txlist(endpoint, api_key):
    """Return a transport `f(address) -> [tx dicts]` backed by an Etherscan-style
    account/txlist endpoint. `api_key` may be empty (works at a low rate)."""
    def fetch(address):
        q = urllib.parse.urlencode({
            "module": "account", "action": "txlist", "address": address,
            "startblock": 0, "endblock": 99999999, "sort": "asc",
            "apikey": api_key or ""})
        req = urllib.request.Request(
            f"{endpoint}?{q}",
            headers={"Accept": "application/json",
                     "User-Agent": "Traceipt/0.2 anchor-recover"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode())
        result = body.get("result")
        # Etherscan returns result as a string message on error / empty.
        return result if isinstance(result, list) else []
    return fetch


def extract_anchor(tx: dict, address: str, network: str) -> dict | None:
    """If `tx` is one of our anchor publications (outbound from `address`,
    calldata = marker + 32-byte root), return {root, onchain_tx, onchain_network,
    created_at}; else None. Pure -- no network."""
    if not isinstance(tx, dict):
        return None
    frm = (tx.get("from") or "").lower()
    if frm != address.lower():
        return None
    inp = tx.get("input") or tx.get("data") or ""
    if inp.startswith("0x"):
        inp = inp[2:]
    if not inp.startswith(MARKER_HEX):
        return None
    root = inp[len(MARKER_HEX):]
    if len(root) != 64:
        return None
    # Etherscan gives a unix-seconds string in `timeStamp`; keep it as an ISO-ish
    # marker if present, else leave the raw value.
    ts = tx.get("timeStamp")
    created_at = None
    if ts is not None:
        try:
            from datetime import datetime, timezone
            created_at = datetime.fromtimestamp(int(ts), timezone.utc)\
                .replace(microsecond=0).isoformat()
        except (ValueError, OSError, OverflowError):
            created_at = str(ts)
    return {"root": root.lower(), "onchain_tx": tx.get("hash"),
            "onchain_network": network, "created_at": created_at}


def recover_anchors_from_chain(address: str, network: str, *,
                               api_key: str = "", transport=None) -> list[dict]:
    """Enumerate the gas wallet's transactions and return the anchor records
    (root, tx, network, created_at) for every TRACEIPT-ANCHOR publication, in
    chain order. `transport` (address -> [tx dicts]) is injectable for tests;
    the default hits the network's Etherscan-style explorer."""
    if transport is None:
        endpoint = EXPLORERS.get(network)
        if endpoint is None:
            raise ValueError(f"no explorer configured for network {network!r}")
        transport = _basescan_txlist(endpoint, api_key)
    out = []
    seen = set()
    for tx in transport(address):
        rec = extract_anchor(tx, address, network)
        if rec and rec["root"] not in seen:
            seen.add(rec["root"])
            out.append(rec)
    return out
