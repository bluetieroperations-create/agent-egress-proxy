#!/usr/bin/env python3
"""
aave_reserve.py -- an ADVISORY quality/de-risk signal from Aave v3 reserve config.

"Is this token serious enough that a major lending protocol lists it as collateral, and
has that protocol since DE-RISKED it?" A token Aave lists (active, real LTV) is
blue-chip-vetted; a reserve Aave has FROZEN is one a serious risk process de-listed --
an advisory caution. NOT-listed is the COMMON case for tokenized RWAs and is NEUTRAL
(no signal), never a penalty.

Reads Aave v3 `getReserveConfigurationData(asset)` from the PoolDataProvider (keyless
eth_call). Decodes isActive / isFrozen / ltv / usageAsCollateral.

Tiering: this is an ADVISORY signal (see rwa_aggregate.py). `frozen` contributes to the
composite risk (Aave de-risked it) but does not gate alone; `listed` is a positive note
(recorded, never upgrades -- conservative). Fail-open: an unlisted/inactive reserve or an
unreadable provider -> None (no signal). Stdlib; reuses rwa_readiness's ABI helpers.
"""
from __future__ import annotations

from addresses import is_evm_address
from rwa_readiness import eth_call_data, selector

# Aave v3 PoolDataProvider (AaveProtocolDataProvider) per chain (lowercased). VERIFY per
# deployment -- these move across Aave releases; a wrong address just fails open (no signal).
AAVE_DATA_PROVIDERS = {
    "ethereum": "0x7b4eb56e7cd4b454ba8ff71e4518426369a138a3",
    "base": "0x2d8a3c5677189723c4cb8873cfc9c8976fdf38ac",
    "arbitrum": "0x69fa688f1dc47d4b5d8029d5a35fb7a548310654",
}


def decode_reserve_config(result):
    """PURE: decode Aave `getReserveConfigurationData` -> {ltv, collateral_enabled,
    is_active, is_frozen}, or None. The return is 10 static words:
    [0]decimals [1]ltv [2]liqThreshold [3]liqBonus [4]reserveFactor
    [5]usageAsCollateralEnabled [6]borrowingEnabled [7]stableBorrowEnabled
    [8]isActive [9]isFrozen. NEVER raises."""
    if not isinstance(result, str):
        return None
    s = result[2:] if result.lower().startswith("0x") else result
    if len(s) < 64 * 10:
        return None
    def w(i):
        try:
            return int(s[i * 64:(i + 1) * 64], 16)
        except ValueError:
            return None
    ltv, coll, active, frozen = w(1), w(5), w(8), w(9)
    if None in (ltv, coll, active, frozen):
        return None
    return {"ltv": ltv, "collateral_enabled": bool(coll),
            "is_active": bool(active), "is_frozen": bool(frozen)}


def assess_aave(config):
    """PURE: reserve config -> {grade: frozen|listed|unlisted, ltv, collateral_enabled}.
      * unlisted  -- not an active Aave reserve (the common RWA case; NEUTRAL/no signal).
      * frozen    -- active but FROZEN (Aave de-risked it) -> advisory concern.
      * listed    -- active, not frozen (vetted; positive note, never upgrades).
    NEVER raises."""
    c = config if isinstance(config, dict) else {}
    if not c.get("is_active"):
        return {"grade": "unlisted", "ltv": None, "collateral_enabled": None}
    if c.get("is_frozen"):
        return {"grade": "frozen", "ltv": c.get("ltv"),
                "collateral_enabled": c.get("collateral_enabled")}
    return {"grade": "listed", "ltv": c.get("ltv"),
            "collateral_enabled": c.get("collateral_enabled")}


def apply_aave(verdict, signal):
    """PURE fold: record `signals.aave` (descriptive). ADVISORY -- it never gates alone
    (the aggregation layer weighs a `frozen` reserve collectively). Appends a note on
    `frozen`; `listed` is a positive note; `unlisted` is a no-op. Non-mutating."""
    if not signal or not isinstance(signal, dict):
        return verdict
    grade = signal.get("grade")
    if grade not in ("frozen", "listed"):
        return verdict                               # unlisted / unknown -> no signal
    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    v["signals"]["aave"] = {"grade": grade, "ltv": signal.get("ltv"),
                            "collateral_enabled": signal.get("collateral_enabled")}
    reasons = list(v.get("reasons") or [])
    if grade == "frozen":
        reasons.append("advisory: Aave has FROZEN this token's lending reserve -- a major "
                       "protocol de-risked it")
    else:
        reasons.append("note: token is a listed Aave collateral reserve (LTV %s bps) -- "
                       "protocol-vetted" % signal.get("ltv"))
    v["reasons"] = reasons
    return v


class AaveReserveSource:
    """Reads Aave v3 reserve config for a token. FAIL-OPEN, keyless eth_call. `eth_call`
    is the test/RPC seam: callable(to, data_hex) -> result_hex | None. NEVER raises."""

    def __init__(self, rpc_url=None, timeout=2.5, eth_call=None, providers=None):
        self.rpc_url = (rpc_url or "").rstrip("/")
        self.timeout = timeout
        self._eth_call = eth_call
        self.providers = providers or AAVE_DATA_PROVIDERS

    def check(self, token, chain):
        provider = self.providers.get((chain or "").strip().lower())
        if not is_evm_address(token) or not is_evm_address(provider):
            return None
        res = self._call(provider, eth_call_data(
            "getReserveConfigurationData(address)", (token,)))
        config = decode_reserve_config(res)
        if config is None or not config.get("is_active"):
            return None                              # unlisted/inactive -> no signal
        return assess_aave(config)

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
            headers={"content-type": "application/json", "user-agent": "Blackwall-aave/1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            d = json.loads(r.read(1 << 16))
        res = d.get("result") if isinstance(d, dict) else None
        return res if isinstance(res, str) else None
