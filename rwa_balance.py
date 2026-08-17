#!/usr/bin/env python3
"""
rwa_balance.py -- keyless "did the security actually arrive?" reader for the outcome loop.

The outcome-capture loop (rwa_outcomes.py) labels each buy with MARK-TO-MARKET (via Pyth)
and, when a balance reader is wired, SETTLEMENT-HELD: does the payer now hold a positive
balance of the acquired token? This module is that reader -- keyless on-chain balance reads
across EVM (`balanceOf`) and Solana (`getTokenAccountsByOwner`), dispatched by token format.

`BalanceReader(...)` is callable(token, chain, payer) -> bool | None, matching the
`balance_reader` seam `capture_outcomes` already accepts:
  * True  -- payer holds a positive balance of the token
  * False -- payer holds zero (read succeeded, balance is 0 / no token account)
  * None  -- could not determine (unreachable RPC, non-token contract, bad address)

FAIL-OPEN throughout (None on any error). Reuses rwa_readiness's ABI helpers.

CAVEAT (documented, honest): a positive balance is a HEURISTIC for "this buy settled" --
it does not prove THIS purchase delivered the tokens (the payer may have held them before).
A before/after snapshot or the settlement tx hash is the real attribution; this is the
cheap proxy. Stdlib only.
"""
from __future__ import annotations

from addresses import is_evm_address
from rwa_readiness import decode_uint, eth_call_data
from solana_rwa import is_solana_address


class BalanceReader:
    """Chain-dispatching token-balance reader. `eth_call` / `sol_rpc` are test seams:
      * eth_call(to, data_hex) -> result_hex | None
      * sol_rpc(method, params) -> JSON-RPC `result` | None
    When omitted, real JSON-RPC is used against evm_rpc_url / solana_rpc_url."""

    def __init__(self, evm_rpc_url=None, solana_rpc_url=None, timeout=2.5,
                 eth_call=None, sol_rpc=None):
        self.evm_rpc_url = (evm_rpc_url or "").rstrip("/")
        self.solana_rpc_url = (solana_rpc_url or "").rstrip("/")
        self.timeout = timeout
        self._eth_call = eth_call
        self._sol_rpc = sol_rpc

    def __call__(self, token, chain, payer):
        """Heuristic settlement-held: does the payer hold a positive balance?
        True / False / None (unreadable). Wraps `balance_of`."""
        bal = self.balance_of(token, chain, payer)
        return (bal > 0) if bal is not None else None

    def balance_of(self, token, chain, payer):
        """Raw token balance (atomic units) as int, or None if unreadable. Used for the
        DEFINITIVE before/after settlement delta (a snapshot at buy vs at T+N)."""
        if is_evm_address(token) and is_evm_address(payer):
            return self._evm_balance(token, payer)
        if is_solana_address(token) and is_solana_address(payer):
            return self._sol_balance(token, payer)
        return None

    # -- EVM: balanceOf(payer) --
    def _evm_balance(self, token, payer):
        res = self._do_eth_call(token, eth_call_data("balanceOf(address)", (payer,)))
        return decode_uint(res)                     # "0x"/absent -> None; a real 0 -> 0

    def _do_eth_call(self, to, data):
        try:
            if self._eth_call is not None:
                return self._eth_call(to, data)
            return self._json_rpc_eth_call(to, data)
        except Exception:
            return None

    def _json_rpc_eth_call(self, to, data):
        import json
        import urllib.request
        if not self.evm_rpc_url:
            return None
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": to, "data": data}, "latest"]}
        req = urllib.request.Request(
            self.evm_rpc_url, data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json",
                     "user-agent": "Blackwall-rwa-balance/1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            d = json.loads(r.read(1 << 16))
        return d.get("result") if isinstance(d, dict) else None

    # -- Solana: sum token-account balances for (owner, mint) --
    def _sol_balance(self, mint, owner):
        try:
            res = self._do_sol_rpc("getTokenAccountsByOwner",
                                    [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        except Exception:
            return None
        value = res.get("value") if isinstance(res, dict) else None
        if not isinstance(value, list):
            return None                             # couldn't read -> unknown
        total = 0
        for acc in value:
            try:
                amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
                total += int(amt)
            except (KeyError, TypeError, ValueError):
                continue
        return total                                # empty list -> 0 (holds none)

    def _do_sol_rpc(self, method, params):
        if self._sol_rpc is not None:
            return self._sol_rpc(method, params)
        return self._json_rpc_sol(method, params)

    def _json_rpc_sol(self, method, params):
        import json
        import urllib.request
        if not self.solana_rpc_url:
            return None
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        req = urllib.request.Request(
            self.solana_rpc_url, data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json",
                     "user-agent": "Blackwall-rwa-balance/1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            d = json.loads(r.read(1 << 17))
        return d.get("result") if isinstance(d, dict) else None
