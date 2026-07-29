#!/usr/bin/env python3
"""
turnkey_signer.py -- gate a Turnkey signing call with Blackwall.

Turnkey signs server-side via signing "activities" (SIGN_TRANSACTION /
SIGN_RAW_PAYLOAD). This adapter wraps the call: it maps a Turnkey signing request
into a Blackwall check and only invokes your Turnkey signer on GO (or an approved
HOLD). Withholding the signature on STOP means the payment never leaves Turnkey.

You supply `sign_fn(request) -> signature` (your existing Turnkey call -- SDK or the
stamped HTTP API); this adapter decides WHETHER to call it. Because Turnkey's
`unsignedTransaction` is RLP-encoded, the default extractor reads the tx object you
already hold pre-encoding: attach it as `request["transaction"] = {to,value,data,
chain}` (or `parameters.transaction`), and/or `request["payment_authorization"]`
for a signed x402 payment. Override `extract` for other shapes.

    guard = WalletGuard(in_process_decider(), on_unreachable=FAIL_CLOSED)
    tk = TurnkeyGuard(guard, sign_fn=my_turnkey_sign)
    res = tk.sign(turnkey_request)          # res.signed / res.signature / res.decision
"""
from __future__ import annotations

from wallet_guard import WalletGuard, claim_from_tx  # noqa: F401 (re-export)


def turnkey_extract(request):
    """Map a Turnkey signing request -> a Blackwall forecast payload (or {} if it
    can't be parsed, so the guard's availability toggle governs)."""
    r = request if isinstance(request, dict) else {}
    params = r.get("parameters") if isinstance(r.get("parameters"), dict) else {}

    tx = r.get("transaction")
    if not isinstance(tx, dict):
        tx = params.get("transaction")
    payload = claim_from_tx(tx) if isinstance(tx, dict) else {}

    pa = r.get("payment_authorization") or params.get("payment_authorization")
    if pa:
        payload = payload or {
            "counterparty": r.get("counterparty"), "amount": r.get("amount"),
            "asset": r.get("asset") or "USDC", "chain": r.get("chain") or "base"}
        payload["payment_authorization"] = pa
    return payload


class TurnkeyGuard:
    def __init__(self, guard: WalletGuard, *, sign_fn, extract=turnkey_extract):
        self.guard = guard
        self.sign_fn = sign_fn
        self.extract = extract

    def sign(self, request):
        """Check the request, then call your Turnkey signer only if allowed.
        Returns a SignResult (.signed, .signature, .decision)."""
        payload = self.extract(request) or {}
        return self.guard.guard_sign(payload, lambda: self.sign_fn(request))
