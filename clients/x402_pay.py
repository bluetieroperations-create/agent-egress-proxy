#!/usr/bin/env python3
"""
x402_pay.py -- funded-signer test client for Blackwall's x402 verdict endpoint.

TEST-ONLY TOOLING. The Blackwall service is stdlib-only; this client is NOT part
of it. It needs `eth-account` to sign the EIP-3009 `transferWithAuthorization`
that becomes the `X-PAYMENT` header:

    pip install -r clients/requirements.txt

What it exercises (the real money path):

    POST /v1/forecast-payment              -> 402 + accepts[]   (no payment yet)
    sign EIP-3009 auth with your funded key -> X-PAYMENT header
    POST again with X-PAYMENT               -> facilitator verify+settle
                                            -> GO / HOLD / STOP verdict

You provide (operator):
  * SIGNER_PRIVATE_KEY  -- a THROWAWAY wallet's private key (env var; never commit)
  * that wallet funded on the target network -- Base-Sepolia recommended:
      - testnet USDC (the asset actually transferred) from the Circle faucet
      - a little testnet ETH (the "exact" scheme is gasless via the facilitator,
        but fund a bit as a safety net)

Example (testnet dry-run against a local server):
    export SIGNER_PRIVATE_KEY=0x<throwaway-key>
    python blackwall.py --pay-to 0xYourPayTo --network base-sepolia \
        --facilitator https://facilitator.x402.rs &
    python clients/x402_pay.py \
        --url http://localhost:8402/v1/forecast-payment \
        --counterparty 0xCounterparty --amount 5.00 \
        --network base-sepolia --rpc https://sepolia.base.org

The EIP-712 domain (token name/version) is read ON-CHAIN via --rpc so the
signature matches the real token contract. Without --rpc it falls back to a
known-values table and prints a warning -- prefer --rpc for a real run.
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import x402_challenge          # noqa: E402

try:
    from eth_account import Account
    from eth_account.messages import encode_typed_data
except ImportError:
    sys.stderr.write(
        "x402_pay: needs eth-account. Install it:\n"
        "    pip install -r clients/requirements.txt\n")
    raise SystemExit(2)

# Known chain ids + public RPCs (override with --rpc).
CHAIN_IDS = {"base": 8453, "base-sepolia": 84532}
DEFAULT_RPC = {"base": "https://mainnet.base.org",
               "base-sepolia": "https://sepolia.base.org"}

_CAIP2 = {"base": "eip155:8453", "base-mainnet": "eip155:8453",
          "base-sepolia": "eip155:84532", "ethereum": "eip155:1"}


def _to_caip2(network):
    """Bare/legacy network name -> CAIP-2 (x402 v2). CAIP-2 or unknown names
    pass through so comparisons stay meaningful."""
    if not network or not isinstance(network, str):
        return network
    n = network.strip()
    if ":" in n:
        return n
    return _CAIP2.get(n.lower(), n)

# Fallback EIP-712 domain (name, version) per token, used ONLY when --rpc is
# absent. Verify against the chain for a real run -- these are not authoritative.
FALLBACK_DOMAIN = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ("USD Coin", "2"),  # Base USDC
    "0x036cbd53842c5426634e7929541ec2318f3dcf7e": ("USDC", "2"),      # Base-Sepolia USDC
}

_NAME_SELECTOR = "0x06fdde03"     # name()
_VERSION_SELECTOR = "0x54fd4d50"  # version()


def _to_0x_hex(b):
    """bytes -> '0x...' lowercase hex, regardless of HexBytes .hex() quirks."""
    h = b.hex() if not isinstance(b, str) else b
    return h if h.startswith("0x") else "0x" + h


def _http_json(url, body=None, headers=None, timeout=30):
    """POST (or GET) JSON; return (status, parsed-or-raw, raw_bytes, resp_headers).

    The response HEADERS are returned, not discarded: an x402 v2 server carries
    its payment requirements in `WWW-Authenticate: X402 requirements="<b64>"`
    rather than the body, and a client that only reads the body cannot find an
    `accepts` entry to sign against -- it sees a 402 with nothing in it.
    """
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"content-type": "application/json", "accept": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
            resp_headers = r.headers
    except urllib.error.HTTPError as e:        # 402 lands here
        raw = e.read()
        status = e.code
        resp_headers = e.headers
    try:
        return status, json.loads(raw), raw, resp_headers
    except ValueError:
        return status, None, raw, resp_headers


def _rpc(rpc_url, method, params, timeout=20):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    # Some public RPCs (e.g. sepolia.base.org behind Cloudflare) 403 the default
    # Python-urllib UA; send a browser-like UA + explicit accept.
    req = urllib.request.Request(
        rpc_url, data=json.dumps(payload).encode(),
        headers={"content-type": "application/json",
                 "accept": "application/json",
                 "user-agent": "Mozilla/5.0 (x402-pay test client)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise RuntimeError("rpc %s: %s" % (method, out["error"]))
    return out["result"]


def _decode_abi_string(hexstr):
    b = bytes.fromhex(hexstr[2:]) if hexstr.startswith("0x") else bytes.fromhex(hexstr)
    if len(b) < 64:
        return ""
    off = int.from_bytes(b[0:32], "big")
    ln = int.from_bytes(b[off:off + 32], "big")
    return b[off + 32:off + 32 + ln].decode("utf-8", "replace")


def token_domain(asset, network, rpc_url):
    """Resolve the EIP-712 domain for the USDC token at `asset`."""
    verifying = asset
    if rpc_url:
        name = _decode_abi_string(_rpc(rpc_url, "eth_call",
                                       [{"to": asset, "data": _NAME_SELECTOR}, "latest"]))
        try:
            version = _decode_abi_string(_rpc(rpc_url, "eth_call",
                                              [{"to": asset, "data": _VERSION_SELECTOR}, "latest"]))
        except Exception:
            version = "2"  # some tokens omit version(); EIP-3009 USDC uses "2"
        chain_id = int(_rpc(rpc_url, "eth_chainId", []), 16)
        return {"name": name, "version": version or "2",
                "chainId": chain_id, "verifyingContract": verifying}, True
    # No RPC: fall back to known values (and say so).
    known = FALLBACK_DOMAIN.get(asset.lower())
    if not known:
        raise SystemExit(
            "x402_pay: no --rpc and asset %s not in the fallback table; "
            "pass --rpc so the EIP-712 domain can be read on-chain." % asset)
    name, version = known
    cid = CHAIN_IDS.get(network)
    if cid is None:
        raise SystemExit("x402_pay: unknown --network %r; pass --rpc." % network)
    return {"name": name, "version": version,
            "chainId": cid, "verifyingContract": verifying}, False


def build_payment(challenge_req, signer_pk, signer_addr, network, rpc_url):
    """Sign an EIP-3009 authorization satisfying `challenge_req` -> payment dict.

    The token signed over is the one the SERVER advertises in the 402 challenge
    (`challenge_req['asset']`) -- that is what the facilitator settles, so the
    EIP-712 domain must use it, not anything client-chosen."""
    pay_to = challenge_req["payTo"]
    asset = challenge_req["asset"]
    # x402 v2 uses `amount`; fall back to v1 `maxAmountRequired` for older servers.
    value = int(challenge_req.get("amount", challenge_req.get("maxAmountRequired")))
    ttl = int(challenge_req.get("maxTimeoutSeconds", 120))
    valid_after = 0
    valid_before = int(time.time()) + max(ttl, 600)
    nonce = os.urandom(32)

    # Prefer the EIP-712 domain the SERVER advertises in `extra` -- that is the
    # canonical x402 source and guarantees the client signs the SAME domain the
    # facilitator verifies against (no client/server divergence, and no --rpc
    # needed). Fall back to reading it on-chain only when the server omits it.
    extra = challenge_req.get("extra")
    if isinstance(extra, dict) and extra.get("name") and extra.get("version"):
        chain_id = CHAIN_IDS.get(network)
        if chain_id is None and rpc_url:
            chain_id = int(_rpc(rpc_url, "eth_chainId", []), 16)
        if chain_id is None:
            raise SystemExit("x402_pay: server gave an EIP-712 domain but the "
                             "chainId for --network %r is unknown; pass --rpc." % network)
        domain = {"name": extra["name"], "version": extra["version"],
                  "chainId": chain_id, "verifyingContract": asset}
        on_chain = True  # authoritative source = the server's challenge
    else:
        domain, on_chain = token_domain(asset, network, rpc_url)
        if not on_chain:
            sys.stderr.write(
                "x402_pay: WARNING -- EIP-712 domain not verified on-chain "
                "(name=%r version=%r). Use --rpc for a real run.\n"
                % (domain["name"], domain["version"]))

    message = {
        "from": signer_addr,
        "to": pay_to,
        "value": value,
        "validAfter": valid_after,
        "validBefore": valid_before,
        "nonce": nonce,
    }
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
        "domain": domain,
        "primaryType": "TransferWithAuthorization",
        "message": message,
    }
    signable = encode_typed_data(full_message=typed)
    signed = Account.sign_message(signable, private_key=signer_pk)
    # Self-check: the facilitator recovers the signer the same way; if this
    # doesn't round-trip, the on-wire signature is junk -- fail loud, locally.
    recovered = Account.recover_message(signable, signature=signed.signature)
    if recovered.lower() != signer_addr.lower():
        raise SystemExit("x402_pay: signature self-check FAILED (recovered %s, "
                         "expected %s)" % (recovered, signer_addr))

    # x402 v2 PaymentPayload: the chosen requirements go under `accepted`
    # (echo the server's challenge_req so scheme/network/asset/amount match
    # exactly), and the EIP-3009 authorization nests under payload.authorization.
    payment = {
        "x402Version": 2,
        "accepted": dict(challenge_req),
        "payload": {
            "signature": _to_0x_hex(signed.signature),
            "authorization": {
                "from": signer_addr,
                "to": pay_to,
                "value": str(value),
                "validAfter": str(valid_after),
                "validBefore": str(valid_before),
                "nonce": _to_0x_hex(nonce),
            },
        },
    }
    return payment


def payment_header(requirements, signer_pk, signer_addr, network, rpc_url=None):
    """Sign a payment satisfying a 402 `accepts[]` entry and return the base64
    `X-PAYMENT` header value (the wire form the server expects)."""
    payment = build_payment(requirements, signer_pk, signer_addr, network, rpc_url)
    return base64.b64encode(json.dumps(payment).encode()).decode()


def make_pay(signer_pk, network="base", rpc_url=None):
    """Return a `pay(requirements) -> X-PAYMENT` callback for
    `traceipt_attest.anchor_verdict(..., pay=...)`. Binds a funded key so the
    stdlib anchor loop can fund Traceipt's x402 `/attest` without importing
    eth-account. Reads the signer address once from the key."""
    signer_addr = Account.from_key(signer_pk).address
    def _pay(requirements):
        return payment_header(requirements, signer_pk, signer_addr, network, rpc_url)
    return _pay


def main(argv=None):
    p = argparse.ArgumentParser(description="Funded-signer x402 test client for Blackwall.")
    p.add_argument("--url", required=True, help="Blackwall forecast endpoint URL")
    p.add_argument("--counterparty", required=True, help="counterparty EVM address to score")
    p.add_argument("--amount", required=True, help="payment amount at risk (USDC), e.g. 5.00")
    p.add_argument("--network", default="base-sepolia", help="x402 network (default base-sepolia)")
    p.add_argument("--quote-asset", help="asset of the prospective payment being "
                                         "judged (forecast body); default follows --network")
    p.add_argument("--chain", help="chain of the prospective payment being judged "
                                   "(forecast body); default = --network")
    p.add_argument("--rpc", help="JSON-RPC URL to read the token's EIP-712 domain "
                                 "on-chain (defaults to a public RPC for the network)")
    p.add_argument("--no-rpc", action="store_true",
                   help="skip on-chain domain lookup (use the fallback table)")
    args = p.parse_args(argv)

    pk = os.environ.get("SIGNER_PRIVATE_KEY")
    if not pk:
        sys.stderr.write("x402_pay: set SIGNER_PRIVATE_KEY (a throwaway funded key)\n")
        return 2
    acct = Account.from_key(pk)
    signer_addr = acct.address

    network_usdc = {"base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e"}
    quote_asset = args.quote_asset or network_usdc.get(args.network)
    if not quote_asset:
        sys.stderr.write("x402_pay: unknown --network; pass --quote-asset explicitly\n")
        return 2
    chain = args.chain or args.network
    rpc_url = None if args.no_rpc else (args.rpc or DEFAULT_RPC.get(args.network))

    # The forecast body describes the prospective payment being judged; `payer`
    # is the agent wallet that will sign the x402 payment (this signer), which
    # binds the later on-chain settlement confirmation to us.
    body = {"counterparty": args.counterparty, "amount": args.amount,
            "asset": quote_asset, "chain": chain, "payer": signer_addr}
    sys.stdout.write("signer:       %s\n" % signer_addr)
    sys.stdout.write("POST %s  (no payment)\n" % args.url)

    status, parsed, raw, resp_headers = _http_json(args.url, body)
    if status != 402:
        sys.stdout.write("expected 402, got %s:\n%s\n"
                         % (status, raw.decode("utf-8", "replace")))
        return 1
    accepts, carrier = x402_challenge.parse_challenge(raw, resp_headers)
    if not accepts:
        sys.stdout.write("402 carried no payment requirements in the body or in "
                         "WWW-Authenticate:\n%s\n" % raw.decode("utf-8", "replace"))
        return 1
    if carrier == x402_challenge.HDR_ACCEPTS:
        sys.stdout.write("(requirements came from WWW-Authenticate, not the body)\n")
    req = accepts[0]
    # v2 uses `amount`; keep the v1 `maxAmountRequired` fallback for display.
    adv_amount = req.get("amount", req.get("maxAmountRequired"))
    sys.stdout.write("402 challenge: payTo=%s amount=%s network=%s asset=%s\n"
                     % (req.get("payTo"), adv_amount,
                        req.get("network"), req.get("asset")))

    # v2 advertises CAIP-2 networks (eip155:8453); compare in CAIP-2 space so a
    # bare --network name still matches (base == eip155:8453).
    if _to_caip2(req.get("network")) != _to_caip2(args.network):
        sys.stderr.write("x402_pay: WARNING -- server advertises network %r but "
                         "--network is %r; signature will be rejected.\n"
                         % (req.get("network"), args.network))

    x_payment = payment_header(req, pk, signer_addr, args.network, rpc_url)
    sys.stdout.write("signed EIP-3009 authorization; resending with X-PAYMENT...\n")

    status, parsed, raw, _ = _http_json(args.url, body, headers={"X-PAYMENT": x_payment})
    sys.stdout.write("HTTP %s\n" % status)
    if parsed is not None:
        sys.stdout.write(json.dumps(parsed, indent=2) + "\n")
    else:
        sys.stdout.write(raw.decode("utf-8", "replace") + "\n")
    if status == 200 and isinstance(parsed, dict) and "verdict" in parsed:
        sys.stdout.write("\nVERDICT: %s  (paid path completed)\n" % parsed["verdict"])
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
