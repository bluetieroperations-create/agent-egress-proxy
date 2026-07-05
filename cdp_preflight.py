#!/usr/bin/env python3
"""
cdp_preflight.py -- prove the CDP Bearer-JWT auth works BEFORE any paid call.

It mints a real CDP JWT from your credentials and hits the CDP facilitator's
read-only endpoints (GET /supported and POST /verify). Neither moves money --
only /settle transfers -- so this costs nothing. The point is to see whether CDP
ACCEPTS our token: a 401/403 means the JWT is wrong (fix it here, cheaply); any
other response (a supported-list, or a structured x402 validation error) means we
got past auth and settlement will work on the real paid call.

Run (secret stays on your machine -- do NOT paste it into chat):

    $env:CDP_API_KEY_ID     = '<the UUID key id>'
    $env:CDP_API_KEY_SECRET = '<the base64 Ed25519 secret>'
    python cdp_preflight.py
    Remove-Item Env:CDP_API_KEY_SECRET

Reads creds from the same env vars the service uses. Prints nothing secret.
"""
import json
import os
import urllib.error
import urllib.request

from cdp_auth import build_cdp_jwt
from x402 import CDP_FACILITATOR_URL, X402_VERSION, build_requirements

KEY_ID = os.environ.get("CDP_API_KEY_ID")
SECRET = os.environ.get("CDP_API_KEY_SECRET")


def _call(method, path, body=None):
    """Make an authenticated request; return (status, body_text)."""
    url = CDP_FACILITATOR_URL + path
    token = build_cdp_jwt(KEY_ID, SECRET, method, url)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json",
                 "Authorization": "Bearer " + token,
                 "User-Agent": "Mozilla/5.0 (Blackwall CDP preflight)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # network/DNS/TLS
        return None, "REQUEST FAILED: %s" % e


def _verdict(status):
    if status is None:
        return "NETWORK ERROR -- could not reach CDP (not an auth result)."
    if status in (401, 403):
        return ("AUTH FAILED (%d) -- the JWT was rejected. Fix before paying: "
                "check the key is Ed25519 (not ES256), the id/secret pair, and "
                "the base path. No money involved here." % status)
    return ("AUTH OK (%d) -- got past authentication. A supported-list or a "
            "structured x402 validation error here is EXPECTED and fine; it "
            "means settlement will authenticate on the real paid call." % status)


def main():
    if not KEY_ID or not SECRET:
        print("Set CDP_API_KEY_ID and CDP_API_KEY_SECRET in the environment first.")
        raise SystemExit(2)
    print("CDP facilitator:", CDP_FACILITATOR_URL)
    print("Key id (masked):", KEY_ID[:8] + "..." + KEY_ID[-4:], "\n")

    # 1) GET /supported -- lightest possible authenticated read.
    s1, b1 = _call("GET", "/supported")
    print("== GET /supported ==")
    print("HTTP", s1)
    print("->", _verdict(s1))
    # Confirm CDP settles OUR exact tuple: Base mainnet + exact + x402 v2. Our
    # 402 advertises network eip155:8453 / scheme exact / x402Version 2, so CDP
    # must list that kind or a real v2 paid call would be an unsupported-kind
    # rejection. (CDP mixes naming: v1 as "base", v2 as CAIP-2 "eip155:8453".)
    try:
        kinds = json.loads(b1).get("kinds", [])
    except Exception:
        kinds = []
    def _is_base_mainnet(net):
        return str(net).lower() in ("base", "eip155:8453")
    base_kinds = [k for k in kinds if _is_base_mainnet(k.get("network"))]
    ours = [k for k in base_kinds
            if k.get("scheme") == "exact" and k.get("x402Version") == 2]
    print("\n  Base-mainnet kinds CDP supports:")
    for k in base_kinds:
        print("    - network=%s scheme=%s x402Version=%s"
              % (k.get("network"), k.get("scheme"), k.get("x402Version")))
    if ours:
        print("  ==> GO: CDP settles Base-mainnet exact v2 -- our endpoint's tuple. "
              "Safe to do the real paid call through CDP.")
    else:
        v1 = [k for k in base_kinds
              if k.get("scheme") == "exact" and k.get("x402Version") == 1]
        if v1:
            print("  ==> MISMATCH: CDP supports Base-mainnet exact but only x402 "
                  "v1, while our endpoint advertises v2. Do NOT pay yet -- tell "
                  "Claude; the service must offer a v1 accepts entry for CDP "
                  "settlement (or CDP must add mainnet v2).")
        else:
            print("  ==> Base-mainnet exact not found in the list above -- share "
                  "this output before paying.")
    print()

    # 2) POST /verify with a throwaway payload -- read-only, moves no money.
    dummy_req = build_requirements(10000, "0x3ec5e0ec1e1cb8e2afa36f3b40eed9057d9004e1",
                                   "cdp-preflight", network="base")
    dummy_payment = {"x402Version": X402_VERSION, "scheme": "exact",
                     "network": "eip155:8453", "payload": {}}
    s2, b2 = _call("POST", "/verify",
                   {"x402Version": X402_VERSION, "paymentPayload": dummy_payment,
                    "paymentRequirements": dummy_req})
    print("== POST /verify (throwaway payload, no funds move) ==")
    print("HTTP", s2)
    print(b2[:600])
    print("->", _verdict(s2))


if __name__ == "__main__":
    main()
