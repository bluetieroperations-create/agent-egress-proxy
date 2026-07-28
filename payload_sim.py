#!/usr/bin/env python3
"""
payload_sim.py -- Phase 1 payload simulation: cross-check the agent's ACTUAL signed
x402 payment against the payment it asked Blackwall to score.

Blackwall's verdict is computed from the CLAIMED {counterparty, amount, asset,
chain} in the request body. But a compromised or MITM'd agent can ask us to score
"pay $5 to X" while the authorization it is about to broadcast actually says "pay
$5000 to Y". Blackwall never sees the real signed payment -- so a clean verdict
would be about a payment that ISN'T the one being made. This closes that gap.

Phase 1 (this module, stdlib, no crypto): when the request carries the agent's
signed EIP-3009 `transferWithAuthorization` (the exact X-PAYMENT it is about to
send the counterparty), decode it and assert the authorization MATCHES the payment
being scored:
  * to      == counterparty   (paying who you asked me to score)
  * value   == amount         (atomic units; the sum you asked me to score)
  * asset   == asset          (same token, when the claim names a contract address)
  * network == chain          (same chain, CAIP-2 aware)
  * a nonce is present        (a real EIP-3009 auth always carries one)
Any mismatch is a NON-NEGOTIABLE hard STOP -- "the signed payment does not match
the payment you asked me to score" -- folded into decide_payment's `hard_stop`.
Time-validity (expired / not-yet-valid) is advisory (a facilitator rejects those
anyway), reported as a warning, not a hard stop.

IMPORTANT -- CHANNEL: the payment-being-scored travels in the request BODY field
`payment_authorization`, NOT the transport `X-PAYMENT` header. That header is
Blackwall's OWN fee payment (Blackwall is itself x402-paid, see x402.py); the two
must never be conflated.

Phase 2 (deferred, needs secp256k1): recover the signer from the EIP-712 digest and
confirm signer == the stated payer -- catches a signature that isn't the payer's.
See docs/STRATEGY_REVIEW.md.

Reuses x402.py's decode/extract helpers and addresses.py. Pure + stdlib.
"""
from __future__ import annotations

from addresses import addresses_equal, is_evm_address
from x402 import (_accepted, _authorization, decode_payment_header, to_atomic,
                  to_caip2)


def _decode(x_payment, decode):
    """`x_payment` may be a base64 X-PAYMENT string or a pre-decoded payment dict."""
    if isinstance(x_payment, dict):
        return x_payment
    if isinstance(x_payment, str):
        return (decode or decode_payment_header)(x_payment)
    return None


def _int_or_none(v):
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def _show(v):
    """A short, log-safe rendering of an attacker-controlled value."""
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "..."


def check_payment_authorization(claim, x_payment, *, decimals=6, now=None,
                                decode=None):
    """Cross-check a signed x402 payment against the CLAIMED payment being scored.

    `claim`     = {counterparty, amount, asset, chain} (amount a decimal string or
                  number; asset may be a contract address or a symbol like "USDC").
    `x_payment` = the base64 X-PAYMENT the agent is about to SEND THE COUNTERPARTY
                  (or a pre-decoded dict). `decimals` is the asset's decimals
                  (USDC = 6). `now` (unix seconds), when given, enables the advisory
                  time-validity checks.

    Returns {"checked": bool, "matches": bool, "mismatches": [str], "warnings": [str]}.
    `checked` is False when no payment was supplied (the cross-check is OPT-IN;
    absence is NOT a failure). When supplied, every entry in `mismatches` is a
    hard-stop reason. NEVER raises -- a crafted payment can't crash the gate.
    """
    if x_payment is None or x_payment == "":
        return {"checked": False, "matches": True, "mismatches": [], "warnings": []}

    payment = _decode(x_payment, decode)
    if not isinstance(payment, dict):
        return {"checked": True, "matches": False, "warnings": [],
                "mismatches": ["could not decode the signed payment to verify it "
                               "matches the payment being scored"]}

    claim = claim if isinstance(claim, dict) else {}
    auth = _authorization(payment)
    acc = _accepted(payment)
    mismatches = []
    warnings = []

    # --- recipient: paying who you asked me to score? ---
    to, cp = auth.get("to"), claim.get("counterparty")
    if not to or not cp or not addresses_equal(to, cp):
        mismatches.append(
            "signed payment pays %s but you asked me to score %s (recipient mismatch)"
            % (_show(to), _show(cp)))

    # --- amount: same sum (compared in atomic units)? ---
    want = to_atomic(claim.get("amount"), decimals)
    got = _int_or_none(auth.get("value"))
    if want is None or got is None or want != got:
        mismatches.append(
            "signed payment value %s != the %s atomic units you asked me to score"
            % (_show(auth.get("value")), _show(want)))

    # --- asset: same token? (only when the claim names a contract address; a
    #     symbol like "USDC" can't be checked against a contract address) ---
    _pl = payment.get("payload")
    _pl = _pl if isinstance(_pl, dict) else {}
    pay_asset = acc.get("asset") or payment.get("asset") or _pl.get("asset")
    claim_asset = claim.get("asset")
    if pay_asset is not None and is_evm_address(claim_asset) \
            and not addresses_equal(pay_asset, claim_asset):
        mismatches.append("signed payment asset %s != the scored asset %s"
                          % (_show(pay_asset), _show(claim_asset)))

    # --- network: same chain? (CAIP-2 aware, so "base" == "eip155:8453") ---
    net = acc.get("network") or payment.get("network")
    claim_chain = claim.get("chain")
    if net is not None and claim_chain is not None \
            and to_caip2(net) != to_caip2(claim_chain):
        mismatches.append("signed payment network %s != the scored chain %s"
                          % (_show(net), _show(claim_chain)))

    # --- a real EIP-3009 authorization always carries a nonce ---
    if not auth.get("nonce"):
        mismatches.append("signed payment carries no authorization nonce -- not a "
                          "valid EIP-3009 authorization")

    # --- time validity: advisory only (a facilitator rejects these itself) ---
    if now is not None:
        vb = _int_or_none(auth.get("validBefore"))
        va = _int_or_none(auth.get("validAfter"))
        if vb is not None and vb <= int(now):
            warnings.append("signed authorization already expired "
                            "(validBefore is in the past)")
        if va is not None and va > int(now):
            warnings.append("signed authorization not yet valid "
                            "(validAfter is in the future)")

    return {"checked": True, "matches": not mismatches,
            "mismatches": mismatches, "warnings": warnings}
