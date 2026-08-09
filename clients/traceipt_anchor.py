#!/usr/bin/env python3
"""
traceipt_anchor.py -- fund Traceipt's x402 `/attest` to anchor a Black_Wall verdict.

This is the REAL-PATH tool for integration A: it takes a verdict JSON, POSTs its
canonical digest to Traceipt `POST /attest`, and when the gate answers 402 it signs
an EIP-3009 payment (funded key + eth-account) and retries -- exactly the flow the
stdlib `traceipt_attest.anchor_verdict(..., pay=...)` drives, with the signing half
supplied by `x402_pay.make_pay`.

TEST-ONLY, like x402_pay.py: it holds a private key. Use a throwaway funded wallet.

    export SIGNER_PRIVATE_KEY=0x...            # throwaway, funded with a little USDC
    python clients/traceipt_anchor.py \
        --base-url https://api.traceipt.xyz \
        --network base --rpc https://mainnet.base.org \
        --verdict '{"verdict":"STOP","score":0.0,"receipt_id":"bw_abc"}'

Without --verdict it anchors a tiny demo verdict. The digest anchored is
`sha256(canonical_json(verdict))` -- a PROOF of the verdict, never its contents.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import traceipt_attest  # noqa: E402  (repo-root module)
from x402_pay import make_pay  # noqa: E402  (sibling client; brings in eth-account)


def main(argv=None):
    p = argparse.ArgumentParser(description="Anchor a Black_Wall verdict via Traceipt /attest (x402-paid).")
    p.add_argument("--base-url", required=True, help="Traceipt base URL, e.g. https://api.traceipt.xyz")
    p.add_argument("--network", default="base", help="x402 network (default base)")
    p.add_argument("--rpc", help="JSON-RPC URL to read the token EIP-712 domain on-chain (real runs)")
    p.add_argument("--verdict", help="verdict JSON to anchor (default: a demo verdict)")
    p.add_argument("--max-atomic", type=int, default=1_000_000,
                   help="refuse to auto-pay if /attest demands more than this many "
                        "atomic USDC units (default 1_000_000 = $1.00; anchoring is "
                        "a micro-payment, so a spoofed challenge can't drain you)")
    p.add_argument("--timeout", type=int, default=15)
    args = p.parse_args(argv)

    pk = os.environ.get("SIGNER_PRIVATE_KEY")
    if not pk:
        sys.stderr.write("traceipt_anchor: set SIGNER_PRIVATE_KEY (a throwaway funded key)\n")
        return 2

    if args.verdict:
        try:
            verdict = json.loads(args.verdict)
        except json.JSONDecodeError as e:
            sys.stderr.write("traceipt_anchor: --verdict is not valid JSON: %s\n" % e)
            return 2
    else:
        verdict = {"verdict": "STOP", "score": 0.0, "receipt_id": "bw_demo"}

    digest = traceipt_attest.verdict_digest(verdict)
    sys.stdout.write("verdict digest: %s\n" % digest)
    sys.stdout.write("POST %s/attest  (will pay on 402)\n" % args.base_url.rstrip("/"))

    pay = make_pay(pk, network=args.network, rpc_url=args.rpc)
    result = traceipt_attest.anchor_verdict(
        args.base_url, verdict, pay=pay,
        max_amount_atomic=args.max_atomic, timeout=args.timeout)

    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    if result.get("ok"):
        sys.stdout.write("\nANCHORED: %s\nproof: %s\n"
                         % (result.get("attestation_id"), result.get("proof_url")))
        return 0
    sys.stdout.write("\nNOT anchored (fail-open): %s\n" % result.get("reason"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
