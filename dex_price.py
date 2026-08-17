#!/usr/bin/env python3
"""
dex_price.py -- the token's REAL on-chain market price from a Uniswap-v3 pool, and a
market-vs-NAV peg gate.

The existing peg gate (pyth_price.py) compares the PAID price to the underlying equity
(Pyth). But Backed's token oracle is Pyth-MANAGED -- the oracle price tracks the
underlying by construction, so it CANNOT see the token trading off its NAV on an actual
DEX (a bait/manipulated pool, thin liquidity, or a genuine market depeg). This reads the
token's live pool price and flags a material deviation from the underlying fair value.

  * `dex_token_price(sqrt_price_x96, dec0, dec1, token_is_token0)` -- PURE: decode a
    v3 `slot0().sqrtPriceX96` into the token's price in QUOTE units.
  * `assess_market_peg(dex_price, underlying_price, band)` -- PURE: |dex/underlying - 1|
    > band -> off_peg. Same signal shape as the other RWA gates.
  * `apply_market_peg(verdict, signal)` -- PURE fold: off_peg -> GO->HOLD. Conservative,
    fail-open, never STOP (a pool can be thin/stale).
  * `DexPriceSource` -- reads a pool (explicit `acquires.dex_pool`, else discovered via
    the v3 factory `getPool(token, USDC, fee)` across fee tiers) + `slot0` + `token0` +
    `decimals`, and computes the price. OPT-IN (network), injected transport, fail-open.

DUST-POOL FILTER: a pool must hold >= `min_liquidity` QUOTE (USDC, default $5k) to be
trusted for a price, and among fee tiers the DEEPEST qualifying pool is used -- so a
thin/manipulated pool can't false-flag. Depth is proxied by the pool's USDC balance
(`balanceOf(pool)`), which is simple + robust; below the floor -> no signal (fail-open).
The gate is also HOLD-only and monotonic (can only ADD caution, never clear -- the
paid-vs-underlying Pyth peg fires independently), so any residual is bounded.

LIMITATIONS (audited & accepted):
  * USDC-balance is a proxy for depth, not the exact in-range v3 liquidity; good enough
    to reject dust, not a microstructure model. Tune `min_liquidity` per deployment.
  * USDC-quote only; a token trading only against WETH yields no signal (fail-open).

Stdlib; reuses rwa_readiness's ABI helpers.
"""
from __future__ import annotations

import math

from addresses import is_evm_address
from rwa_readiness import decode_address, decode_uint, eth_call_data

_Q96 = 2 ** 96

# Per-chain Uniswap-v3 factory + USDC quote (lowercased). Extend as needed.
DEX_CONFIG = {
    "ethereum": {"factory": "0x1f98431c8ad98523631ae4a59f267346ea31f984",
                 "usdc": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "usdc_decimals": 6},
    "base": {"factory": "0x33128a8fc17869897dce68ed026d694621f6fdfd",
             "usdc": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "usdc_decimals": 6},
    "arbitrum": {"factory": "0x1f98431c8ad98523631ae4a59f267346ea31f984",
                 "usdc": "0xaf88d065e77c8cc2239327c5edb3a432268e5831", "usdc_decimals": 6},
}
FEE_TIERS = (500, 3000, 10000)
MARKET_PEG_BAND = 0.10          # > 10% off NAV on-chain -> HOLD
# A pool must hold at least this much QUOTE (USDC, atomic 6dp) to be trusted for a price:
# below it, a dust/manipulated pool would give a false off-peg. $5,000 default.
MIN_QUOTE_LIQUIDITY = 5000 * 10 ** 6


def dex_token_price(sqrt_price_x96, dec0, dec1, token_is_token0):
    """PURE: a v3 sqrtPriceX96 -> the token's price in QUOTE units, or None.

    v3 encodes sqrt(token1/token0) in raw units. token1-per-token0 (human) =
    (sqrt/2^96)^2 * 10^(dec0-dec1). If the token IS token0 (quote is token1) that value
    is quote-per-token directly; if the token is token1 (quote is token0) it's the
    reciprocal. NEVER raises."""
    try:
        sp = int(sqrt_price_x96)                      # OverflowError on inf/nan
    except (TypeError, ValueError, OverflowError):
        return None
    if sp <= 0:
        return None
    try:
        t1_per_t0 = (sp / _Q96) ** 2 * (10 ** (int(dec0) - int(dec1)))
    except (TypeError, ValueError, OverflowError):
        return None
    if not (isinstance(t1_per_t0, float) and math.isfinite(t1_per_t0)) or t1_per_t0 <= 0:
        return None
    price = t1_per_t0 if token_is_token0 else (1.0 / t1_per_t0)
    return price if math.isfinite(price) else None


def assess_market_peg(dex_price, underlying_price, band=MARKET_PEG_BAND):
    """PURE: token DEX price vs underlying NAV -> readiness signal. `off_peg` when the
    on-chain market price deviates > band from the underlying (either direction --
    discount = distressed/illiquid, premium = speculative/overpriced). NEVER raises."""
    dp = _f(dex_price)
    up = _f(underlying_price)
    if dp is None or up is None or up <= 0 or dp <= 0:
        return {"grade": "unknown", "standard": "dex-market", "reasons": [],
                "signals": {"standard": "dex-market", "dex_price": dp,
                            "underlying_price": up, "market_ratio": None}}
    ratio = dp / up
    sig = {"standard": "dex-market", "dex_price": dp, "underlying_price": up,
           "market_ratio": ratio}
    if abs(ratio - 1.0) > band:
        return {"grade": "off_peg", "standard": "dex-market", "signals": sig,
                "reasons": ["tokenized security trades %.1f%% %s its underlying NAV on "
                            "the DEX ($%.2f vs $%.2f) -- possible depeg / thin liquidity "
                            "/ manipulated pool"
                            % (abs(ratio - 1.0) * 100, "above" if ratio > 1 else "below",
                               dp, up)]}
    return {"grade": "on_peg", "standard": "dex-market", "signals": sig,
            "reasons": ["on-chain market price within peg (%.2fx NAV)" % ratio]}


def apply_market_peg(verdict, signal):
    """PURE fold: annotate signals.dex_market; escalate GO->HOLD when off_peg.
    CONSERVATIVE-ONLY (never upgrades / never STOP); None/on_peg/unknown recorded but
    non-escalating. Non-mutating."""
    if not signal or not isinstance(signal, dict):
        return verdict
    grade = signal.get("grade")
    if grade not in ("on_peg", "off_peg", "unknown"):
        return verdict
    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    v["signals"]["dex_market"] = signal.get("signals") or {"grade": grade}
    reasons = list(v.get("reasons") or [])
    if grade == "off_peg":
        reasons.extend(signal.get("reasons") or [])
        if v.get("verdict") == "GO":
            v["verdict"] = "HOLD"
            reasons.append("escalated GO->HOLD: tokenized security is off its NAV peg on "
                           "the on-chain market")
    v["reasons"] = reasons
    return v


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class DexPriceSource:
    """Reads a Uniswap-v3 pool for a token's live price in USDC. OPT-IN, fail-open.
    `eth_call` is the test/RPC seam: callable(to, data_hex) -> result_hex | None."""

    def __init__(self, rpc_url=None, timeout=2.5, eth_call=None,
                 min_liquidity=MIN_QUOTE_LIQUIDITY):
        self.rpc_url = (rpc_url or "").rstrip("/")
        self.timeout = timeout
        self._eth_call = eth_call
        self.min_liquidity = int(min_liquidity)

    def price(self, token, chain, pool=None):
        """Token price in USDC, or None. Skips DUST pools (< `min_liquidity` USDC) so a
        thin/manipulated pool can't false-flag. `pool` (from `acquires.dex_pool`) is
        still depth-checked; otherwise the v3 factory is queried across fee tiers and the
        DEEPEST qualifying pool is used (not first-found)."""
        cfg = DEX_CONFIG.get((chain or "").strip().lower())
        if not is_evm_address(token) or not cfg:
            return None
        quote, qdec = cfg["usdc"], cfg["usdc_decimals"]
        if is_evm_address(pool):
            bal = self._quote_balance(pool, quote)
            if bal is None or bal < self.min_liquidity:
                return None                          # dust / unreadable -> no signal
        else:
            pool = self._best_pool(cfg["factory"], token, quote)
        if not is_evm_address(pool):
            return None
        sqrtp = self._slot0_sqrt(pool)
        token0 = decode_address(self._call(pool, eth_call_data("token0()")))
        if sqrtp is None or token0 is None:
            return None
        token_is_token0 = token0.lower() == token.lower()
        tdec = decode_uint(self._call(token, eth_call_data("decimals()")))
        if tdec is None:
            return None
        dec0, dec1 = (tdec, qdec) if token_is_token0 else (qdec, tdec)
        return dex_token_price(sqrtp, dec0, dec1, token_is_token0)

    def _best_pool(self, factory, token, quote):
        """The DEEPEST pool (most quote liquidity) across fee tiers that clears the dust
        floor, or None if none does. Depth-ranked, not first-found."""
        best, best_bal = None, self.min_liquidity - 1
        for fee in FEE_TIERS:
            p = decode_address(self._call(factory, eth_call_data(
                "getPool(address,address,uint24)", (token, quote, fee))))
            if not is_evm_address(p):
                continue
            bal = self._quote_balance(p, quote)
            if bal is not None and bal > best_bal:
                best, best_bal = p, bal
        return best

    def _quote_balance(self, pool, quote):
        """The pool's QUOTE (USDC) balance in atomic units -- the dust filter's depth
        proxy. `balanceOf(pool)` on the quote token. None if unreadable."""
        return decode_uint(self._call(quote, eth_call_data("balanceOf(address)", (pool,))))

    def _slot0_sqrt(self, pool):
        # slot0() returns (uint160 sqrtPriceX96, int24 tick, ...) -- first word is sqrt.
        res = self._call(pool, eth_call_data("slot0()"))
        return decode_uint(res)

    def _call(self, to, data):
        try:
            if self._eth_call is not None:
                return self._eth_call(to, data)
            return self._json_rpc(to, data)
        except Exception:
            return None

    def _json_rpc(self, to, data):
        import json
        import urllib.request
        if not self.rpc_url:
            return None
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": to, "data": data}, "latest"]}
        req = urllib.request.Request(
            self.rpc_url, data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json",
                     "user-agent": "Blackwall-dex/1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            d = json.loads(r.read(1 << 16))
        res = d.get("result") if isinstance(d, dict) else None
        return res if isinstance(res, str) else None
