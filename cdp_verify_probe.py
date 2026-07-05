#!/usr/bin/env python3
"""
cdp_verify_probe.py -- prove CdpFacilitator.verify() end-to-end against LIVE CDP
with a REAL signature, WITHOUT moving any money.

This is the belt-and-suspenders step between the auth preflight and the first
paid call. It signs a real EIP-3009 `transferWithAuthorization` with the burner
key (reusing clients/x402_pay.build_payment), then runs it through the actual
`CdpFacilitator.verify()` class over the wire. In x402, `/verify` only CHECKS the
signature + balance -- it does NOT submit the transfer on-chain (only `/settle`
does) -- so this costs $0. A run that returns isValid:true proves everything
except the settle submission itself: the CDP JWT auth, the real payment envelope,
the class plumbing, and that CDP accepts our signature for our payTo/amount.

Run it LOCALLY, before deploying to Render, so any envelope problem surfaces at
$0 on your machine instead of during a live paid call:

    $env:CDP_API_KEY_ID     = '<uuid>'
    $env:CDP_API_KEY_SECRET = '<base64 ed25519 secret>'
    $env:BAZAAR_WALLET_KEY  = '0x<burner private key>'   # 66 chars
    python cdp_verify_probe.py
    Remove-Item Env:CDP_API_KEY_SECRET, Env:BAZAAR_WALLET_KEY

Prints no secret and no private key. Never calls settle. Signs a throwaway
authorization (fresh random nonce) that is never submitted, so no funds move even
if you run it repeatedly.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "clients"))

from creds_local import load_creds  # noqa: E402
from x402 import BASE_USDC, CdpFacilitator, build_requirements  # noqa: E402

# Public: Blackwall's payTo (recipient). Overridable, defaults to the live one.
PAY_TO = os.environ.get("BLACKWALL_PAY_TO",
                        "0x3ec5e0ec1e1cb8e2afa36f3b40eed9057d9004e1")
# The fee an amount-at-risk of $10.01 charges (10bps) -- the cheapest payable
# call. Only needs to be <= the burner's balance for the balance check to pass.
try:
    FEE_ATOMIC = int(os.environ.get("CDP_PROBE_FEE_ATOMIC", "10010"))
except ValueError:
    raise SystemExit("CDP_PROBE_FEE_ATOMIC must be an integer (atomic USDC units)")


def main():
    load_creds()  # auto-load ~/.blackwall-creds so setting env vars by hand is optional
    cdp_id = os.environ.get("CDP_API_KEY_ID")
    cdp_secret = os.environ.get("CDP_API_KEY_SECRET")
    burner = os.environ.get("BAZAAR_WALLET_KEY")
    if not (cdp_id and cdp_secret):
        print("Set CDP_API_KEY_ID and CDP_API_KEY_SECRET first.")
        raise SystemExit(2)
    # Accept the key with or without the 0x prefix (MetaMask sometimes omits it);
    # normalize so you never have to prepend it by hand.
    burner = (burner or "").strip()
    if burner[:2].lower() == "0x":
        burner = burner[2:]
    if len(burner) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in burner):
        print("Set BAZAAR_WALLET_KEY to the burner private key (64 hex chars, "
              "with or without 0x).")
        raise SystemExit(2)
    burner = "0x" + burner

    try:
        from x402_pay import build_payment  # from clients/ (uses eth-account)
        from eth_account import Account
    except Exception as e:
        print("Need eth-account (the repo's client signing dep): %s" % e)
        raise SystemExit(2)

    signer = Account.from_key(burner).address
    print("Burner (payer): %s" % signer)
    print("payTo         : %s" % PAY_TO)
    print("Fee signed     : %d atomic USDC (~$%.6f) -- NOT settled, verify only\n"
          % (FEE_ATOMIC, FEE_ATOMIC / 1e6))

    # The exact v2 requirements the live server advertises for this call.
    req = build_requirements(FEE_ATOMIC, PAY_TO, "cdp-verify-probe",
                             asset=BASE_USDC, network="base")
    # Real EIP-3009 signature over the server-advertised domain (no RPC needed --
    # req carries the EIP-712 `extra` domain). Never submitted on-chain.
    payment = build_payment(req, burner, signer, "base", rpc_url=None)

    # THE test: the real class, over the wire, to live CDP. verify != settle.
    fac = CdpFacilitator(cdp_id, cdp_secret)
    result = fac.verify(payment, req)

    print("CDP /verify ->", result)
    if result.get("valid"):
        print("\n==> PASS: live CDP accepted a real signed payment through "
              "CdpFacilitator.verify(). Auth + envelope + class + signature all "
              "proven end-to-end. The only thing left unproven is the settle "
              "submission itself -- that's the real $0.01 call.")
    else:
        reason = result.get("reason", "")
        print("\n==> verify returned NOT valid. reason: %s" % reason)
        print("    Interpret before deploying / paying:")
        print("    - 'facilitator unreachable' / auth-ish -> JWT/creds issue "
              "(re-run cdp_preflight.py).")
        print("    - insufficient balance -> fund the burner above $%.4f."
              % (FEE_ATOMIC / 1e6))
        print("    - signature/authorization invalid -> envelope/domain "
              "mismatch; share this output (no secrets in it).")


if __name__ == "__main__":
    main()
