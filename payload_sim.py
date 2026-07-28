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

LIMITATIONS (audited & accepted):
  * Known-asset gate. Phase 2's signer recovery (and thus the cryptographic
    chain/asset binding) runs only for assets with a trusted EIP-712 domain in
    x402.EIP712_DOMAINS (Base/Base-Sepolia USDC today). For an UNKNOWN asset there
    is no trusted domain, so signer verification degrades to a warning and the
    Phase-1 asset/chain checks fall back to best-effort on self-declared metadata
    (a payment omitting them isn't cross-checked on those fields). Recipient +
    amount always bind; add domains to the table to extend the crypto binding.
  * Fundamental limit: we verify the SHOWN authorization; we cannot prove it is the
    one actually broadcast. A fully malicious agent showing one auth and sending
    another is out of scope for any phase.
  * Decimals. The amount is compared in atomic units assuming USDC (6dp). A
    non-USDC asset with different decimals would FALSE-mismatch on amount -- errs
    SAFE (STOP, never a false GO). Blackwall is USDC-only today; wire decimals to
    the asset when that changes.
  * Latency. Pure-Python secp256k1 recovery is ~20ms per signed check -- negligible
    next to the on-chain settlement it guards, and only runs when a signature is
    present.

Phase 2 (BUILT, `verify_signer=True`): reconstruct the EIP-712 digest of the
`transferWithAuthorization` and RECOVER the signer (pure-Python secp256k1), then
confirm it is the authorization's stated `from`. Crucially the digest's DOMAIN is
built from the CLAIM (chainId from `chain`, verifyingContract from `asset`), so a
valid recovery also cryptographically binds the chain + asset -- closing the Phase-1
gap where those were self-declared. Recovery failure or a signer != `from` for a
KNOWN asset is a hard STOP; an unknown asset/chain (no trusted domain) or an absent
signature degrades to a warning (Phase-1 recipient+amount still bind).

Reuses x402.py's decode/extract helpers, addresses.py, and the pure-Python
keccak/secp256k1/eip712 primitives. Pure + stdlib.
"""
from __future__ import annotations

from addresses import addresses_equal, is_evm_address
from x402 import (EIP712_DOMAINS, _accepted, _authorization, decode_payment_header,
                  to_atomic, to_caip2)


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


def _claim_domain(claim):
    """Build the EIP-712 domain FROM THE CLAIM (name/version from the asset's known
    domain, chainId from the claimed chain, verifyingContract = the claimed asset),
    or None if the asset/chain isn't a trusted known domain. Building it from the
    claim -- not the attacker's metadata -- is what makes a successful recovery bind
    the signature to the claimed chain + asset."""
    asset = claim.get("asset")
    if not is_evm_address(asset):
        return None
    nv = EIP712_DOMAINS.get(asset.lower())
    if not nv:
        return None
    caip2 = to_caip2(claim.get("chain"))
    if not (isinstance(caip2, str) and caip2.startswith("eip155:")):
        return None
    try:
        chain_id = int(caip2.split(":")[1])
    except (ValueError, IndexError):
        return None
    return {"name": nv["name"], "version": nv["version"],
            "chainId": chain_id, "verifyingContract": asset}


def _recover_signer(payment, auth, domain):
    """Recover the Ethereum address that signed the payment's EIP-3009
    authorization under `domain`, or None. NEVER raises."""
    try:
        from eip712 import (pubkey_to_address, split_signature,
                            transfer_authorization_digest)
        from secp256k1 import ecdsa_recover
        payload = payment.get("payload") if isinstance(payment, dict) else None
        sig = payload.get("signature") if isinstance(payload, dict) else None
        parts = split_signature(sig) if sig else None
        if not parts:
            return None
        r, s, rec = parts
        z = transfer_authorization_digest(domain, {
            "from": auth.get("from"), "to": auth.get("to"),
            "value": auth.get("value"), "validAfter": auth.get("validAfter"),
            "validBefore": auth.get("validBefore"), "nonce": auth.get("nonce")})
        q = ecdsa_recover(z, r, s, rec)
        return pubkey_to_address(q) if q else None
    except Exception:
        return None


def check_payment_authorization(claim, x_payment, *, decimals=6, now=None,
                                verify_signer=True, decode=None):
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

    # --- Phase 2: recover the signer and confirm it is the stated payer. The
    #     domain is built from the CLAIM, so a valid recovery ALSO binds the
    #     signature to the claimed chain + asset (EIP-712 domain = chainId +
    #     token). Hard STOP on a bad/foreign signature for a KNOWN asset; unknown
    #     asset/chain or a missing signature degrades to a warning. ---
    if verify_signer:
        _from = auth.get("from")
        domain = _claim_domain(claim)
        payload = payment.get("payload") if isinstance(payment, dict) else None
        has_sig = isinstance(payload, dict) and bool(payload.get("signature"))
        if domain is None:
            warnings.append("signer not verified: no trusted EIP-712 domain for the "
                            "claimed asset/chain (%s)" % _show(claim.get("asset")))
        elif not has_sig:
            warnings.append("signer not verified: payment carries no signature")
        elif not _from:
            warnings.append("signer not verified: authorization has no `from`")
        else:
            recovered = _recover_signer(payment, auth, domain)
            if recovered is None:
                mismatches.append(
                    "signature is not a valid payer signature for the claimed "
                    "chain+asset -- forged, or a different chain/asset than scored")
            elif not addresses_equal(recovered, _from):
                mismatches.append(
                    "signature signer %s != the stated payer %s (from)"
                    % (_show(recovered), _show(_from)))

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
