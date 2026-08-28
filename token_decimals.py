#!/usr/bin/env python3
"""
token_decimals.py -- read an ERC-20's `decimals()` on-chain, cached forever.

WHY THIS EXISTS
---------------
The decimals audit (docs/DECIMALS_AUDIT.md) removed the hardcoded `6` from three
gating paths, replacing it with a static `KNOWN_DECIMALS` table. That made an
unlisted token fail SAFE -- reported as unverified, or its price observation
omitted -- but not VERIFIED. This closes that residual.

WHY CACHING IS DIFFERENT HERE
-----------------------------
`rpc_node.py` deliberately uses a SHORT (30s) TTL because staleness there is a
safety tradeoff: a payee blacklisted five minutes ago must not read as fine.

**`decimals()` is the opposite.** It is immutable for the life of an ERC-20 --
the value is fixed at deployment and no standard token exposes a setter. So the
correct TTL is effectively infinite, and the practical consequence is that this
costs at most ONE upstream call per token, EVER, not one per payment. A single
warm entry serves every future payment in that asset.

That distinction is the whole reason this is a separate module rather than a call
into the shared node: reusing the 30-second TTL would re-leak the same query
(which token an agent is about to pay in) every 30 seconds for no benefit.

BOUNDARY
--------
- **Opt-in.** Network on the hot path, behind `BLACKWALL_TOKEN_DECIMALS=1`.
- **Fail-open.** Unreachable RPC, junk response, or a non-token address -> None,
  which is exactly the safe behaviour the static table already produces.
- **Static table wins.** Its entries are audited and on-chain verified; the
  network is only consulted for what the table does not know.
- **Never trusts an implausible answer.** A contract can return anything; a value
  outside 0..36 is rejected rather than fed into 10**n scaling.
"""

import os
import threading

DECIMALS_SELECTOR = "0x313ce567"          # keccak("decimals()")[:4]
TOTAL_SUPPLY_SELECTOR = "0x18160ddd"      # keccak("totalSupply()")[:4]
# SPL mint accounts are a fixed 82-byte layout; `decimals` is the single byte at
# offset 44. Verified live against USDC on mainnet-beta (returns 6).
SPL_MINT_LEN = 82
SPL_DECIMALS_OFFSET = 44
MAX_PLAUSIBLE_DECIMALS = 36
ENV_FLAG = "BLACKWALL_TOKEN_DECIMALS"
ENV_RPC = "BLACKWALL_TOKEN_DECIMALS_RPC"
ENV_SOLANA_RPC = "BLACKWALL_TOKEN_DECIMALS_SOLANA_RPC"


def decode_decimals(result):
    """Decode an eth_call result into a plausible decimals value, else None.

    Rejects `0x`/empty (the address is not a token, or the call reverted) and
    anything outside 0..36. An implausible value must not reach 10**n scaling:
    that is how a hostile or broken contract would turn a price check into a
    nonsense comparison.
    """
    if not result or not isinstance(result, str):
        return None
    raw = result[2:] if result.startswith("0x") else result
    if not raw or set(raw) == {"0"} and len(raw) < 2:
        return None
    try:
        value = int(raw, 16)
    except ValueError:
        return None
    if 0 <= value <= MAX_PLAUSIBLE_DECIMALS:
        return value
    return None


def decode_spl_decimals(data):
    """Decimals from a base64 SPL mint account, or None.

    `data` is the `value.data[0]` string from `getAccountInfo` with base64
    encoding. Length is checked because a token ACCOUNT (165 bytes) and a mint
    (82) are both valid accounts; reading offset 44 of the wrong one returns a
    byte of somebody's balance.
    """
    import base64 as _b64
    if not isinstance(data, str) or not data:
        return None
    try:
        raw = _b64.b64decode(data)
    except Exception:
        return None
    if len(raw) < SPL_MINT_LEN:
        return None
    value = raw[SPL_DECIMALS_OFFSET]
    return value if 0 <= value <= MAX_PLAUSIBLE_DECIMALS else None


def enabled(env=None):
    """True when the on-chain lookup is explicitly switched on."""
    env = env if env is not None else os.environ
    # Lowercased: env vars are commonly written uppercase ("TRUE"), and a
    # feature that silently stays off because of case is worse than one that
    # refuses to start.
    return str(env.get(ENV_FLAG, "")).strip().lower() in ("1", "true", "yes", "on")


class OnChainDecimals:
    """Cache-forever `decimals()` reader.

    `transport(rpc_url, body) -> parsed JSON-RPC response` is injected so the
    whole class is testable without a network. Concurrent lookups of the same
    token are coalesced into one upstream call.
    """

    def __init__(self, rpc_url=None, transport=None, timeout=8.0, table=None,
                 solana_rpc=None):
        self.rpc_url = rpc_url or os.environ.get(ENV_RPC)
        self.solana_rpc = solana_rpc or os.environ.get(ENV_SOLANA_RPC)
        self.timeout = timeout
        self._transport = transport
        self._cache = {}                  # token -> int | None (None = asked, unknown)
        self._lock = threading.Lock()
        self._inflight = {}
        self.upstream_calls = 0
        if table is None:
            try:
                from payload_sim import KNOWN_DECIMALS
                table = KNOWN_DECIMALS
            except Exception:
                table = {}
        self.table = table

    # -- internals ---------------------------------------------------------
    def _send(self, token, selector=DECIMALS_SELECTOR):
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": token, "data": selector}, "latest"]}
        if self._transport is not None:
            return self._transport(self.rpc_url, body)
        from rpc_node import forward_upstream
        return forward_upstream(self.rpc_url, body, timeout=self.timeout)

    def _fetch(self, token):
        try:
            resp = self._send(token)
        except Exception:
            return None
        if not isinstance(resp, dict) or resp.get("error"):
            return None
        value = decode_decimals(resp.get("result"))
        if value != 0:
            return value
        # A contract with a catch-all fallback returning zeros is
        # indistinguishable from a genuine 0-decimal token, and reading 0 for an
        # 18-decimal asset mis-scales by 10^18. Zero-decimal tokens are rare, so
        # confirm with ONE extra call in that case only: a real ERC-20 also
        # implements totalSupply().
        try:
            probe = self._send(token, TOTAL_SUPPLY_SELECTOR)
        except Exception:
            return None
        if not isinstance(probe, dict) or probe.get("error"):
            return None
        result = probe.get("result")
        return 0 if isinstance(result, str) and len(result) > 2 else None

    def _fetch_solana(self, mint):
        body = {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                "params": [mint, {"encoding": "base64"}]}
        try:
            if self._transport is not None:
                resp = self._transport(self.solana_rpc, body)
            else:
                from rpc_node import forward_upstream
                resp = forward_upstream(self.solana_rpc, body, timeout=self.timeout)
        except Exception:
            return None
        if not isinstance(resp, dict) or resp.get("error"):
            return None
        value = (resp.get("result") or {}).get("value") or {}
        data = value.get("data")
        return decode_spl_decimals(data[0] if isinstance(data, list) and data else data)

    # -- api ---------------------------------------------------------------
    def lookup(self, asset):
        """Decimals for `asset`, or None. Table first, then one cached call."""
        if not isinstance(asset, str):
            return None
        key = asset.strip().lower()
        if not key:
            return None
        known = self.table.get(key)
        if known is not None:
            return known
        # Non-EVM (a Solana mint is base58, not 0x) needs getAccountInfo, not
        # eth_call. Handled when a Solana RPC is configured; otherwise unknown.
        is_evm = key.startswith("0x")
        if is_evm and not self.rpc_url:
            return None
        if not is_evm and not self.solana_rpc:
            return None

        with self._lock:
            if key in self._cache:
                return self._cache[key]
            event = self._inflight.get(key)
            if event is None:
                event = threading.Event()
                self._inflight[key] = event
                leader = True
            else:
                leader = False
        if not leader:
            event.wait(self.timeout)
            with self._lock:
                return self._cache.get(key)

        # Solana mints are case-SENSITIVE base58, so use the original string.
        value = self._fetch(key) if is_evm else self._fetch_solana(asset.strip())
        self.upstream_calls += 1
        with self._lock:
            # Cached FOREVER, including a None: decimals() is immutable, so a
            # miss is also stable and re-asking only re-leaks the query.
            self._cache[key] = value
            self._inflight.pop(key, None)
        event.set()
        return value


def resolver(rpc_url=None, transport=None, env=None, solana_rpc=None):
    """An `OnChainDecimals` when enabled and configured, else None (fail-open)."""
    if not enabled(env):
        return None
    src = env or os.environ
    url = rpc_url or src.get(ENV_RPC)
    sol = solana_rpc or src.get(ENV_SOLANA_RPC)
    if not url and not sol and transport is None:
        return None
    return OnChainDecimals(rpc_url=url, transport=transport, solana_rpc=sol)
