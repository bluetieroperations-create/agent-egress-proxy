#!/usr/bin/env python3
"""Pay a live Traceipt /attest with a REAL x402 v2 payment and print the
self-contained receipt.

This is the payer side of the flywheel: it screens a subject address into a
canonical verdict, hashes it, then pays the /attest fee with a real EIP-3009
USDC authorization so Traceipt anchors the verdict digest in its next on-chain
Merkle batch. With RECEIPTS_ATTEST_SEAL=immediate the 201 already carries the
full inclusion proof, so the receipt this prints survives even if the server
forgets it.

The payer's private key is read from the environment (TRACEIPT_PAYER_KEY) and
never logged. It is used only to sign the USDC transferWithAuthorization typed
data; the facilitator (Coinbase CDP on mainnet) submits and pays gas for the
transfer, so the payer needs USDC but not ETH.

Usage:
    export TRACEIPT_PAYER_KEY=0x...            # a key holding USDC on the target chain
    python3 tools/pay_attest.py \
        --base-url https://api.traceipt.xyz \
        --subject 0x1111111111111111111111111111111111111111

    # dry run -- build + sign but DON'T send (prints the X-PAYMENT it would use):
    python3 tools/pay_attest.py --subject 0x1111...1111 --dry-run

Real money moves on mainnet. Start on base-sepolia (a live testnet Traceipt) to
rehearse, then point --base-url at the mainnet service.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

# Local imports work whether run from traceipt/ or traceipt/tools/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from traceipt.screening import verdict_digest  # noqa: E402
from traceipt.screening_providers import (  # noqa: E402
    build_verdict, providers_from_env, screen,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _http_json(method: str, url: str, *, headers=None, body=None, timeout=30):
    data = None
    hdrs = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode()), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw}
        return e.code, payload, dict(e.headers)


def _chain_id(network: str) -> int:
    # CAIP-2 "eip155:8453" -> 8453
    if ":" in network:
        return int(network.split(":", 1)[1])
    return int(network)


def build_signed_xpayment(accept: dict, payer_key: str, timeout_s: int) -> tuple[str, str]:
    """Sign a real EIP-3009 transferWithAuthorization for `accept` and return
    (base64 X-PAYMENT header, payer address). Matches the x402 v2 envelope the
    Traceipt gate verifies (accepted + payload.authorization, no top-level
    scheme/network)."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    acct = Account.from_key(payer_key)
    payer = acct.address

    asset = accept["asset"]
    pay_to = accept["payTo"]
    value = str(accept["amount"])
    network = accept["network"]
    extra = accept.get("extra") or {}
    token_name = extra.get("name", "USD Coin")
    token_version = str(extra.get("version", "2"))

    now = int(datetime.now(timezone.utc).timestamp())
    valid_after = 0
    valid_before = now + max(timeout_s, 60) + 60
    nonce_bytes = os.urandom(32)
    nonce_hex = "0x" + nonce_bytes.hex()

    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": token_name,
            "version": token_version,
            "chainId": _chain_id(network),
            "verifyingContract": asset,
        },
        "message": {
            "from": payer,
            "to": pay_to,
            "value": int(value),
            "validAfter": valid_after,
            "validBefore": valid_before,
            "nonce": nonce_bytes,
        },
    }
    signed = Account.sign_message(encode_typed_data(full_message=typed), payer_key)
    signature = "0x" + signed.signature.hex() if not signed.signature.hex().startswith("0x") \
        else signed.signature.hex()

    # Spec-canonical x402 v2 PaymentPayload: scheme + network at the TOP level
    # (what a real facilitator, e.g. Coinbase CDP, validates against). The
    # requirements (asset/payTo/amount) belong in paymentRequirements, which the
    # server supplies -- they are NOT part of the payload.
    envelope = {
        "x402Version": 2,
        "scheme": accept.get("scheme", "exact"),
        "network": network,
        "payload": {
            "signature": signature,
            "authorization": {
                "from": payer,
                "to": pay_to,
                "value": value,
                "validAfter": str(valid_after),
                "validBefore": str(valid_before),
                "nonce": nonce_hex,
            },
        },
    }
    header = base64.b64encode(json.dumps(envelope).encode()).decode()
    return header, payer


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Pay a live Traceipt /attest with a real x402 payment.")
    p.add_argument("--base-url", default=os.environ.get("TRACEIPT_BASE_URL", "https://api.traceipt.xyz"),
                   help="Traceipt service base URL (default: api.traceipt.xyz).")
    p.add_argument("--subject", default="0x" + "11" * 20,
                   help="address to screen into the anchored verdict.")
    p.add_argument("--chain", default="base", help="chain label passed to the screen (informational).")
    p.add_argument("--type", default="sanctions-verdict", help="short label stored with the anchor.")
    p.add_argument("--ref", default=None, help="optional reference stored with the anchor.")
    p.add_argument("--dry-run", action="store_true",
                   help="build + sign but do NOT send; print the X-PAYMENT and verdict.")
    p.add_argument("--out", default=None, help="write the full result JSON here.")
    args = p.parse_args(argv)

    base = args.base_url.rstrip("/")

    # 1. Screen the subject into a canonical verdict + digest (the thing we anchor).
    providers = providers_from_env(os.environ)
    results = screen(args.subject, providers, chain=args.chain)
    verdict = build_verdict(args.subject, results, decided_at=_utc_now_iso())
    vhash = verdict_digest(verdict)
    print(f"verdict.decision = {verdict['decision']}  ({len(verdict['screens'])} screen(s))")
    print(f"verdict_digest   = {vhash}")

    # 2. Get the live 402 challenge and pick the offered payment requirement.
    status, chal, _ = _http_json("POST", f"{base}/attest", body={"hash": vhash})
    if status != 402:
        print(f"expected 402 challenge, got {status}: {json.dumps(chal)[:400]}", file=sys.stderr)
        return 2
    accepts = chal.get("accepts") or []
    if not accepts:
        print("402 offered no `accepts`", file=sys.stderr)
        return 2
    accept = accepts[0]
    print(f"challenge: pay {accept['amount']} of {accept['asset']} on {accept['network']} "
          f"-> {accept['payTo']}")

    key = os.environ.get("TRACEIPT_PAYER_KEY")
    if not key:
        print("set TRACEIPT_PAYER_KEY to a key holding USDC on the target chain "
              "(never paste it into chat).", file=sys.stderr)
        return 3

    # 3. Sign a real EIP-3009 authorization for exactly that requirement.
    xpayment, payer = build_signed_xpayment(accept, key, int(accept.get("maxTimeoutSeconds", 60)))
    print(f"payer            = {payer}")

    body = {"hash": vhash, "type": args.type}
    if args.ref:
        body["ref"] = args.ref

    if args.dry_run:
        print("\n[dry-run] X-PAYMENT (base64):")
        print(xpayment)
        print("\n[dry-run] request body:")
        print(json.dumps(body))
        return 0

    # 4. Pay /attest. On success this 201 carries the self-contained proof.
    status, resp, hdrs = _http_json("POST", f"{base}/attest",
                                    headers={"X-PAYMENT": xpayment}, body=body)
    print(f"\n/attest -> HTTP {status}")
    print(json.dumps(resp, indent=2))
    settle = hdrs.get("X-PAYMENT-RESPONSE")
    if settle:
        print(f"\nX-PAYMENT-RESPONSE: {settle}")

    result = {
        "base_url": base, "payer": payer, "subject": args.subject,
        "verdict": verdict, "verdict_digest": vhash,
        "http_status": status, "response": resp,
        "x_payment_response": settle,
    }
    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nwrote {args.out}")
    return 0 if status in (200, 201) else 1


if __name__ == "__main__":
    raise SystemExit(main())
