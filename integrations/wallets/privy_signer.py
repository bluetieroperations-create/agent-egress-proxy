#!/usr/bin/env python3
"""
privy_signer.py -- gate a Privy server-wallet signing call with Blackwall.

Privy server wallets sign via an RPC-shaped request
(`walletApi.rpc({method, params})`): `eth_signTransaction` / `eth_sendTransaction`
carry a tx object in `params[0]`; `eth_signTypedData_v4` carries typed data (an x402
payment authorization). This adapter maps that request into a Blackwall check and
only invokes your Privy signer on GO (or an approved HOLD) -- withholding on STOP so
the wallet never signs.

You supply `sign_fn(request) -> signature` (your existing Privy call); this adapter
decides WHETHER to call it. Override `extract` for custom request shapes.

    guard = WalletGuard(http_decider("https://blackwall.example"),
                        on_unreachable=FAIL_OPEN)          # toggle per deployment
    pv = PrivyGuard(guard, sign_fn=my_privy_rpc)
    res = pv.sign(privy_rpc_request)
"""
from __future__ import annotations

from wallet_guard import WalletGuard, claim_from_tx  # noqa: F401 (re-export)

_TX_METHODS = ("eth_signtransaction", "eth_sendtransaction")
_TYPED_METHODS = ("eth_signtypeddata_v4", "eth_signtypeddata")


def privy_extract(request):
    """Map a Privy RPC signing request -> a Blackwall forecast payload (or {})."""
    r = request if isinstance(request, dict) else {}
    method = str(r.get("method") or "").lower()
    params = r.get("params")
    first = params[0] if isinstance(params, list) and params else None

    if method in _TX_METHODS and isinstance(first, dict):
        # normalize a Privy/EVM tx object (chainId may be int/hex)
        tx = {"to": first.get("to"), "value": first.get("value", 0),
              "data": first.get("data") or first.get("input") or "0x",
              "chain": first.get("chain")}
        return claim_from_tx(tx)

    if method in _TYPED_METHODS:
        # a signed x402 authorization -> payload cross-check (payload-sim Phase 1/2)
        pa = r.get("payment_authorization")
        payload = {"counterparty": r.get("counterparty"), "amount": r.get("amount"),
                   "asset": r.get("asset") or "USDC", "chain": r.get("chain") or "base"}
        if pa:
            payload["payment_authorization"] = pa
        return payload if payload.get("counterparty") else {}

    # explicit normalized fields (integrator-provided) as a fallback
    if isinstance(r.get("transaction"), dict):
        return claim_from_tx(r["transaction"])
    return {}


class PrivyGuard:
    def __init__(self, guard: WalletGuard, *, sign_fn, extract=privy_extract):
        self.guard = guard
        self.sign_fn = sign_fn
        self.extract = extract

    def sign(self, request):
        """Check the request, then call your Privy signer only if allowed.
        Returns a SignResult (.signed, .signature, .decision)."""
        payload = self.extract(request) or {}
        return self.guard.guard_sign(payload, lambda: self.sign_fn(request))
