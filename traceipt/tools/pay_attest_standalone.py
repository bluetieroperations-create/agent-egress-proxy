#!/usr/bin/env python3
"""Standalone Traceipt /attest payer -- no repo needed, just `eth-account`.

Screens a subject into a canonical verdict (matching Traceipt's shape), hashes
it, pulls the live 402, signs a REAL EIP-3009 USDC authorization, and pays
/attest. The 201 carries the self-contained inclusion proof.

    pip install eth-account
    $env:TRACEIPT_PAYER_KEY = "0x..."     # PowerShell (key holding USDC on Base)
    python pay.py --subject 0x1111111111111111111111111111111111111111 --out mainnet_run.json

The key is read from TRACEIPT_PAYER_KEY and never logged. The CDP facilitator
submits + pays gas for the transfer, so the payer needs USDC, not ETH.
--dry-run builds + signs without sending.
"""
import argparse, base64, hashlib, json, os, sys, urllib.request, urllib.error
from datetime import datetime, timezone

DEMO_SANCTIONED = "0x7f367cc41522ce07553e823bf3be79a889debe1b"  # doc sample -> STOP path
POLICY = "ofac-sanctions-v1"


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_verdict(address):
    """Offline-fixture screen -> the exact canonical verdict Traceipt hashes."""
    listed = address.lower() in {DEMO_SANCTIONED}
    screen = {"provider": "offline-fixture", "listed": listed, "checked": True,
              "source": "offline-fixture", "matches": ["OFAC-SDN"] if listed else []}
    decision = "STOP" if listed else "GO"
    return {"policy": POLICY, "subject": {"address": address.lower()},
            "decided_at": utc_now_iso(), "decision": decision, "screens": [screen]}


def verdict_digest(v):
    canon = json.dumps(v, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canon).hexdigest()


def http_json(method, url, headers=None, body=None, timeout=45):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Accept": "application/json"}
    if data:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode()), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw}
        return e.code, payload, dict(e.headers)


def chain_id(network):
    return int(network.split(":", 1)[1]) if ":" in network else int(network)


def build_signed_xpayment(accept, payer_key, timeout_s):
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    payer = Account.from_key(payer_key).address
    asset, pay_to = accept["asset"], accept["payTo"]
    value, network = str(accept["amount"]), accept["network"]
    extra = accept.get("extra") or {}
    name, version = extra.get("name", "USD Coin"), str(extra.get("version", "2"))

    now = int(datetime.now(timezone.utc).timestamp())
    valid_after, valid_before = 0, now + max(timeout_s, 60) + 60
    nonce = os.urandom(32)

    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"}],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"}]},
        "primaryType": "TransferWithAuthorization",
        "domain": {"name": name, "version": version,
                   "chainId": chain_id(network), "verifyingContract": asset},
        "message": {"from": payer, "to": pay_to, "value": int(value),
                    "validAfter": valid_after, "validBefore": valid_before,
                    "nonce": nonce},
    }
    sig = Account.sign_message(encode_typed_data(full_message=typed), payer_key).signature.hex()
    sig = sig if sig.startswith("0x") else "0x" + sig
    # Canonical x402 v2 PaymentPayload: scheme+network at the top level (what a
    # real facilitator like CDP validates against); requirements ride in
    # paymentRequirements, supplied by the server, not in the payload.
    envelope = {
        "x402Version": 2,
        "scheme": accept.get("scheme", "exact"),
        "network": network,
        "payload": {"signature": sig, "authorization": {
            "from": payer, "to": pay_to, "value": value,
            "validAfter": str(valid_after), "validBefore": str(valid_before),
            "nonce": "0x" + nonce.hex()}},
    }
    return base64.b64encode(json.dumps(envelope).encode()).decode(), payer


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default=os.environ.get("TRACEIPT_BASE_URL", "https://api.traceipt.xyz"))
    p.add_argument("--subject", default="0x" + "11" * 20)
    p.add_argument("--type", default="sanctions-verdict")
    p.add_argument("--ref", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)
    base = args.base_url.rstrip("/")

    verdict = build_verdict(args.subject)
    vhash = verdict_digest(verdict)
    print(f"verdict.decision = {verdict['decision']}")
    print(f"verdict_digest   = {vhash}")

    status, chal, _ = http_json("POST", f"{base}/attest", body={"hash": vhash})
    if status != 402:
        print(f"expected 402, got {status}: {json.dumps(chal)[:400]}", file=sys.stderr)
        return 2
    accepts = chal.get("accepts") or []
    if not accepts:
        print("402 offered no accepts", file=sys.stderr)
        return 2
    accept = accepts[0]
    print(f"challenge: pay {accept['amount']} of {accept['asset']} on {accept['network']} -> {accept['payTo']}")

    key = os.environ.get("TRACEIPT_PAYER_KEY")
    if not key:
        print("set TRACEIPT_PAYER_KEY (never paste it into chat).", file=sys.stderr)
        return 3

    xpayment, payer = build_signed_xpayment(accept, key, int(accept.get("maxTimeoutSeconds", 60)))
    print(f"payer            = {payer}")

    body = {"hash": vhash, "type": args.type}
    if args.ref:
        body["ref"] = args.ref

    if args.dry_run:
        print("\n[dry-run] X-PAYMENT:\n" + xpayment)
        print("[dry-run] body:\n" + json.dumps(body))
        return 0

    status, resp, hdrs = http_json("POST", f"{base}/attest",
                                   headers={"X-PAYMENT": xpayment}, body=body)
    print(f"\n/attest -> HTTP {status}")
    print(json.dumps(resp, indent=2))
    settle = hdrs.get("X-PAYMENT-RESPONSE")
    if settle:
        print(f"\nX-PAYMENT-RESPONSE: {settle}")

    result = {"base_url": base, "payer": payer, "subject": args.subject,
              "verdict": verdict, "verdict_digest": vhash,
              "http_status": status, "response": resp, "x_payment_response": settle}
    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nwrote {args.out}")
    return 0 if status in (200, 201) else 1


if __name__ == "__main__":
    raise SystemExit(main())
