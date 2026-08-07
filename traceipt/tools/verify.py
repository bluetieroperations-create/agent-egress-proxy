#!/usr/bin/env python3
"""Standalone Traceipt receipt verifier -- proves a receipt WITHOUT the Traceipt
server.

The whole point of Traceipt is that a receipt is *independently* verifiable: it
carries everything a third party needs to check it against the public chain and
the issuer's published key -- no call to api.traceipt.xyz required. This tool is
that checker. It survives the issuing server being down, reset, or gone.

It accepts any of:
  * a /attest 201 body (or the mainnet_run.json the payer client writes) -- an
    attestation inclusion proof;
  * a raw proof object ({leaf_data, leaf_index, tree_size, audit_path, root,
    onchain_tx, onchain_network});
  * a signed receipt envelope ({protected, payload, signature}) from /receipts.

Checks (each independently, PASS/FAIL printed):
  1. verdict binding  -- if a verdict is present, its digest == the anchored leaf
  2. inclusion proof  -- the audit path recomputes the Merkle root (RFC 6962)
  3. on-chain anchor  -- the root really is in a Base tx's calldata
                         (TRACEIPT-ANCHOR marker), fetched from a public RPC
  4. signature        -- (receipts) Ed25519 envelope signature vs the issuer JWKS

Usage:
    python3 tools/verify.py mainnet_run.json
    python3 tools/verify.py proof.json --rpc-url https://mainnet.base.org
    python3 tools/verify.py receipt.json --jwks issuer_jwks.json      # fully offline
    python3 tools/verify.py receipt.json --jwks-url https://api.traceipt.xyz/jwks.json
    python3 tools/verify.py proof.json --offline    # skip the on-chain RPC check
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from traceipt.merkle import verify_inclusion  # noqa: E402

RPC_DEFAULTS = {"base": "https://mainnet.base.org",
                "base-sepolia": "https://sepolia.base.org"}
MARKER = b"TRACEIPT-ANCHOR\x01"

GREEN, RED, YEL, DIM, RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


class Checks:
    def __init__(self):
        self.rows = []
        self.failed = False

    def add(self, ok, name, detail=""):
        self.rows.append((ok, name, detail))
        if ok is False:
            self.failed = True

    def render(self):
        for ok, name, detail in self.rows:
            mark = f"{GREEN}PASS{RST}" if ok else (f"{RED}FAIL{RST}" if ok is False
                                                   else f"{YEL}SKIP{RST}")
            print(f"  [{mark}] {name}" + (f"  {DIM}{detail}{RST}" if detail else ""))


def _rpc(url, method, params, timeout=30):
    req = urllib.request.Request(
        url, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                              "params": params}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "Traceipt/0.2 verifier"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode())
    if resp.get("error"):
        raise RuntimeError(resp["error"])
    return resp["result"]


def _verdict_digest(verdict_obj):
    import hashlib
    canon = json.dumps(verdict_obj, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canon).hexdigest()


def _find_proof(doc):
    """Locate the inclusion-proof object inside whatever the user handed us."""
    if not isinstance(doc, dict):
        return None
    if {"root", "leaf_data"} <= doc.keys():
        return doc
    for key in ("proof",):
        if isinstance(doc.get(key), dict):
            return doc[key]
    resp = doc.get("response")
    if isinstance(resp, dict) and isinstance(resp.get("proof"), dict):
        return resp["proof"]
    return None


def _find_verdict(doc):
    if isinstance(doc, dict):
        if isinstance(doc.get("verdict"), dict):
            return doc["verdict"]
    return None


def check_attestation(doc, checks, *, rpc_url=None, offline=False):
    proof = _find_proof(doc)
    if proof is None:
        return False
    leaf_data = proof.get("leaf_data")
    root_hex = proof.get("root")
    if not leaf_data or not root_hex:
        checks.add(False, "proof structure", "missing leaf_data or root")
        return True

    # 1. verdict binding (optional): the anchored leaf is the digest of the verdict.
    verdict = _find_verdict(doc)
    if verdict is not None:
        want = _verdict_digest(verdict)
        checks.add(want == leaf_data, "verdict binding",
                   "digest matches anchored leaf" if want == leaf_data
                   else f"verdict hashes to {want}, leaf is {leaf_data}")

    # 2. inclusion proof: recompute the root from leaf + audit path.
    try:
        path = [bytes.fromhex(h) for h in (proof.get("audit_path") or [])]
        ok = verify_inclusion(int(proof.get("leaf_index", 0)),
                              int(proof.get("tree_size", 1)),
                              leaf_data.encode(), path, bytes.fromhex(root_hex))
        checks.add(ok, "inclusion proof",
                   f"root {root_hex[:16]}… recomputed from {len(path)}-node path"
                   if ok else "audit path does NOT recompute the root")
    except Exception as e:  # noqa: BLE001
        checks.add(False, "inclusion proof", f"error: {e}")

    # 3. on-chain anchor: the root is in the tx calldata on the named network.
    tx = proof.get("onchain_tx")
    network = proof.get("onchain_network") or "base"
    if offline:
        checks.add(None, "on-chain anchor", "skipped (--offline)")
    elif not tx:
        checks.add(None, "on-chain anchor", "no onchain_tx in proof (not published)")
    else:
        url = rpc_url or RPC_DEFAULTS.get(network, RPC_DEFAULTS["base"])
        try:
            txo = _rpc(url, "eth_getTransactionByHash", [tx])
            if not txo:
                checks.add(False, "on-chain anchor", f"tx {tx[:16]}… not found on {network}")
            else:
                raw = bytes.fromhex(txo["input"][2:])
                carries = raw.startswith(MARKER) and raw[len(MARKER):].hex() == root_hex.lower()
                blk = int(txo["blockNumber"], 16) if txo.get("blockNumber") else None
                checks.add(carries, "on-chain anchor",
                           f"{network} block {blk}, calldata carries the exact root"
                           if carries else "tx calldata does NOT carry this root")
        except Exception as e:  # noqa: BLE001
            checks.add(False, "on-chain anchor", f"RPC error: {e}")
    return True


def _load_jwks(args):
    if args.jwks:
        with open(args.jwks) as f:
            return json.load(f)
    url = args.jwks_url
    if not url and args.base_url:
        url = args.base_url.rstrip("/") + "/jwks.json"
    if url:
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "Traceipt/0.2 verifier"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    return None


def check_receipt(envelope, checks, args):
    from traceipt.signing import verify_envelope
    jwks = _load_jwks(args)
    if jwks is None:
        checks.add(None, "signature",
                   "skipped -- provide --jwks <file> or --jwks-url to verify")
        return
    try:
        verify_envelope(envelope, jwks)
        kid = envelope.get("protected", {}).get("kid")
        checks.add(True, "signature", f"Ed25519 verified against issuer key kid={kid}")
    except Exception as e:  # noqa: BLE001
        checks.add(False, "signature", str(e))


def main(argv=None):
    p = argparse.ArgumentParser(description="Verify a Traceipt receipt independently.")
    p.add_argument("file", help="the receipt / proof / attestation JSON (or '-' for stdin)")
    p.add_argument("--rpc-url", default=None, help="Base RPC for the on-chain check")
    p.add_argument("--offline", action="store_true", help="skip the on-chain RPC check")
    p.add_argument("--jwks", default=None, help="issuer JWKS file (for receipt signatures)")
    p.add_argument("--jwks-url", default=None, help="issuer JWKS URL")
    p.add_argument("--base-url", default=None, help="issuer base URL (derives /jwks.json)")
    args = p.parse_args(argv)

    raw = sys.stdin.read() if args.file == "-" else open(args.file).read()
    try:
        doc = json.loads(raw)
    except ValueError as e:
        print(f"{RED}not valid JSON: {e}{RST}", file=sys.stderr)
        return 2

    checks = Checks()
    print(f"\nTraceipt independent verification of {DIM}{args.file}{RST}\n")

    handled = check_attestation(doc, checks, rpc_url=args.rpc_url, offline=args.offline)
    is_envelope = isinstance(doc, dict) and {"protected", "payload", "signature"} <= doc.keys()
    if is_envelope:
        check_receipt(doc, checks, args)
        handled = True

    if not handled:
        print(f"{RED}Unrecognized document: no inclusion proof and no signed "
              f"envelope found.{RST}\nHand me a /attest 201 body, a proof object, "
              f"or a signed receipt envelope.", file=sys.stderr)
        return 2

    checks.render()
    verdict = f"{RED}VERIFICATION FAILED{RST}" if checks.failed else f"{GREEN}VERIFIED{RST}"
    print(f"\n{verdict}\n")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
