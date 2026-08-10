#!/usr/bin/env python3
"""
verify_receipt.py -- independently verify a Black_Wall signed receipt.

This is a REFERENCE verifier. Its whole point is that you do NOT have to trust
Black_Wall (or run its server) to check what Black_Wall attested: a signed
receipt is a standard RFC 8032 Ed25519 signature over a canonical JSON object,
so ANY Ed25519 implementation in any language can verify it. This script uses
PyNaCl; the logic is identical to the "canonicalization" note published at
/.well-known/blackwall-receipt-key.json.

Usage:
  # verify a receipt against an explicitly supplied public key
  python verify_receipt.py --receipt receipt.json --pubkey <hex>

  # or fetch the public key from a running Black_Wall and verify against it
  python verify_receipt.py --receipt receipt.json \
      --key-url https://agent-egress-proxy.onrender.com/.well-known/blackwall-receipt-key.json

  # receipt can also come from stdin
  cat receipt.json | python verify_receipt.py --pubkey <hex>

Exit codes:
  0 = VALID production attestation (verified against a non-dev key).
  1 = INVALID signature, or an error reading the receipt / obtaining the key.
  2 = the signature verifies but ONLY against the built-in DEV key, so the
      receipt is FORGEABLE by anyone and is NOT a trustworthy attestation.
      Pass --allow-dev-key to accept it anyway (exit 0), e.g. for local testing.
Prints what was attested on success.
"""
import argparse
import json
import sys

from receipt_sig import verify_signed_receipt, key_id


def _load_receipt(path):
    raw = sys.stdin.read() if path in (None, "-") else open(path, encoding="utf-8").read()
    obj = json.loads(raw)
    # Accept either a bare signed_receipt or a full forecast response wrapping one.
    return obj.get("signed_receipt", obj) if isinstance(obj, dict) else obj


def _fetch_pubkey(url):
    from urllib.request import urlopen
    with urlopen(url, timeout=10) as r:
        doc = json.loads(r.read())
    return doc["public_key"]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Verify a Black_Wall signed receipt.")
    ap.add_argument("--receipt", "-r", default="-",
                    help="path to receipt JSON (default: stdin)")
    ap.add_argument("--allow-dev-key", action="store_true",
                    help="accept a receipt signed by the built-in (forgeable) "
                         "dev key and exit 0 instead of 2 (local testing only)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pubkey", help="Black_Wall receipt public key (hex)")
    src.add_argument("--key-url", help="URL of the published receipt key JSON")
    args = ap.parse_args(argv)

    try:
        receipt = _load_receipt(args.receipt)
    except Exception as e:
        print("ERROR: could not read receipt: %s" % e, file=sys.stderr)
        return 1
    try:
        pubkey = args.pubkey or _fetch_pubkey(args.key_url)
    except Exception as e:
        print("ERROR: could not obtain public key: %s" % e, file=sys.stderr)
        return 1

    if not verify_signed_receipt(receipt, pubkey):
        print("INVALID: signature does not verify against the given public key")
        return 1

    # A valid signature by the PUBLIC dev key proves nothing -- anyone can forge
    # it. A verified-but-dev receipt must NEVER be reported as a trustworthy
    # attestation via a success exit code: an automated `if verify_receipt ...`
    # pipeline would then trust a forgery. Fail with a distinct exit (2) unless
    # the operator explicitly opts in with --allow-dev-key.
    from receipt_sig import public_key_hex
    is_dev = (pubkey == public_key_hex("00" * 32))
    if is_dev and not args.allow_dev_key:
        print("FORGEABLE: signature verifies but ONLY against the built-in DEV "
              "key -- anyone can produce this receipt. NOT a trustworthy "
              "attestation. Re-run with --allow-dev-key to accept anyway.")
        return 2

    # Signature is valid -- report exactly what was attested, and flag if the
    # receipt was signed by a DIFFERENT key than the one we verified with (it
    # cannot be, since verify passed, but we surface key_id for pinning).
    if is_dev:
        print("WARNING: verified against the built-in DEV key -- this receipt is "
              "FORGEABLE (accepted only because --allow-dev-key was given) and "
              "must not be trusted as a production attestation.", file=sys.stderr)
    print("VALID -- Black_Wall attested:")
    for f in ("verdict", "score", "counterparty", "amount", "asset", "chain",
              "ts", "receipt_id", "key_id"):
        if f in receipt:
            print("  %-12s %s" % (f + ":", receipt[f]))
    print("verified against key_id: %s%s" % (
        key_id(pubkey), "  [DEV KEY -- FORGEABLE]" if is_dev else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
