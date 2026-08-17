#!/usr/bin/env python3
"""
holder_concentration.py -- a keyless rug/manipulation signal from token holder
distribution (Blockscout), the DEX-like on-chain read for "is supply dangerously
concentrated?"

If a single NON-CONTRACT wallet holds a dominant share of a token's supply, that wallet
can dump/manipulate at will -- the classic rug/whale-exit pattern. This reads the top
holders (keyless Blockscout REST) and flags a dominant EOA holder.

CONTRACT holders are EXCLUDED: LP pools, the issuer's custody/distribution contract,
bridges, and staking contracts routinely hold large shares LEGITIMATELY (especially for
tokenized RWAs, where the issuer custodies supply). So the signal is a dominant
*non-contract* holder -- the hard-to-explain concentration. The zero/burn address is
excluded too.

NOTE ON RWAs: this is a stronger signal for general/memecoin buys than for RWAs (an
issuer holding via an EOA would false-flag). It is HOLD-only, fail-open, and advisory --
it can only add a human review, never clear or STOP.

  * `top_eoa_share(holders, total_supply)` -- PURE: top non-contract holder fraction.
  * `assess_concentration(share, threshold)` -- PURE -> signal (concentrated/dispersed).
  * `apply_concentration(verdict, signal)` -- PURE fold: concentrated -> GO->HOLD.
  * `HolderConcentrationSource` -- live Blockscout fetch (injected transport), fail-open.

Stdlib.
"""
from __future__ import annotations

from addresses import is_evm_address

# Per-chain Blockscout REST base (keyless). Extend as needed.
BLOCKSCOUT_BASES = {
    "ethereum": "https://eth.blockscout.com",
    "base": "https://base.blockscout.com",
    "arbitrum": "https://arbitrum.blockscout.com",
}
# A single non-contract wallet holding >= this fraction of supply -> HOLD.
CONCENTRATION_HOLD = 0.50


def _int(x):
    try:
        return int(str(x))
    except (TypeError, ValueError):
        return None


def top_eoa_share(holders, total_supply):
    """PURE: (largest_non_contract_holder_value / total_supply, address), or
    (None, None) when indeterminable. Excludes CONTRACT holders and the zero/burn
    address. `holders` are flat dicts {address, value, is_contract}. NEVER raises."""
    ts = _int(total_supply)
    if not ts or ts <= 0:
        return (None, None)
    best_val, best_addr = 0, None
    for h in holders or []:
        if not isinstance(h, dict) or h.get("is_contract"):
            continue
        a = h.get("address")
        if not is_evm_address(a) or int(a, 16) == 0:
            continue
        v = _int(h.get("value")) or 0
        if v > best_val:
            best_val, best_addr = v, a
    if best_addr is None:
        return (None, None)
    return (best_val / ts, best_addr)


def assess_concentration(share, address=None, threshold=CONCENTRATION_HOLD):
    """PURE: top-EOA share -> {grade: concentrated|dispersed|unknown, share, reasons}."""
    if not isinstance(share, (int, float)):
        return {"grade": "unknown", "share": None, "reasons": []}
    if share >= threshold:
        return {"grade": "concentrated", "share": share,
                "reasons": ["a single non-contract wallet holds %.0f%% of token supply%s "
                            "-- rug / whale-dump / manipulation risk"
                            % (share * 100, " (%s)" % address if address else "")]}
    return {"grade": "dispersed", "share": share,
            "reasons": ["top non-contract holder %.0f%% of supply" % (share * 100)]}


def apply_concentration(verdict, signal):
    """PURE fold: annotate signals.holder_concentration; escalate GO->HOLD when
    concentrated. CONSERVATIVE-ONLY (never upgrades / never STOP); None/unknown records
    but never escalates. Non-mutating."""
    if not signal or not isinstance(signal, dict):
        return verdict
    grade = signal.get("grade")
    if grade not in ("concentrated", "dispersed", "unknown"):
        return verdict
    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    v["signals"]["holder_concentration"] = {"grade": grade, "top_share": signal.get("share")}
    reasons = list(v.get("reasons") or [])
    if grade == "concentrated":
        reasons.extend(signal.get("reasons") or [])
        if v.get("verdict") == "GO":
            v["verdict"] = "HOLD"
            reasons.append("escalated GO->HOLD: token supply is concentrated in one "
                           "non-contract wallet (rug/manipulation risk)")
    v["reasons"] = reasons
    return v


class HolderConcentrationSource:
    """Live top-holder concentration via Blockscout REST (keyless). FAIL-OPEN. `get` is
    the test seam: callable(url) -> parsed JSON. NEVER raises out of check()."""

    def __init__(self, get=None, timeout=3.0, bases=None):
        self._get = get
        self.timeout = timeout
        self.bases = bases or BLOCKSCOUT_BASES

    def check(self, token, chain):
        base = self.bases.get((chain or "").strip().lower())
        if not is_evm_address(token) or not base:
            return None
        try:
            info = self._fetch("%s/api/v2/tokens/%s" % (base, token))
            total = info.get("total_supply") if isinstance(info, dict) else None
            raw = self._fetch("%s/api/v2/tokens/%s/holders" % (base, token))
        except Exception:
            return None
        holders = _normalize_holders(raw)
        if not holders or not total:
            return None
        share, addr = top_eoa_share(holders, total)
        if share is None:
            return None
        return assess_concentration(share, address=addr)

    def _fetch(self, url):
        if self._get is not None:
            return self._get(url)
        import http_util
        return http_util.get_json(url, timeout=self.timeout)


def _normalize_holders(raw):
    """Blockscout `/holders` -> [{address, value, is_contract}]. Tolerant of the v2
    shape (items[].address.{hash,is_contract}) and a flat fallback."""
    items = raw.get("items") if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        addr = it.get("address")
        if isinstance(addr, dict):
            out.append({"address": addr.get("hash"), "value": it.get("value"),
                        "is_contract": bool(addr.get("is_contract"))})
        elif isinstance(addr, str):
            out.append({"address": addr, "value": it.get("value"),
                        "is_contract": bool(it.get("is_contract"))})
    return out
