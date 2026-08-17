#!/usr/bin/env python3
"""
backed_oracle.py -- two keyless signals from Backed/xStocks' public oracle + proof-of-
reserves endpoints, for the tokenized-RWA gate.

The `/public/assets` feed is metadata-only (no price). But two sibling PUBLIC endpoints
carry decision-grade data:

  * GET /api/v2/public/oracles -> per-token price-oracle config. Each entry names the
    AUTHORITATIVE underlying (`collateral.symbol`, e.g. TSLAx -> TSLA) and, when the
    token has a direct Pyth Hermes feed, its `metadata.hermesId`. This HARDENS the peg
    gate: resolve the exact issuer-declared feed instead of heuristically searching Pyth
    by ticker (which can pick the wrong / deprecated feed). ~all carry a `collateral`;
    a subset carry a `hermesId` (the rest use Pyth Lazer, not the free Hermes REST).

  * GET /api/v2/public/proof-of-reserves -> per-token `sharesHeld` (underlying shares in
    custody) vs `circulatingSupply` (tokens minted). A NEW risk signal no x402/RWA
    competitor has: is the tokenized security actually BACKED? `backing_ratio =
    sharesHeld / circulatingSupply`; materially < 1 => under-collateralized -> HOLD.

Both are DESCRIPTIVE/CONSERVATIVE: the backing gate is HOLD-only (issuer-published data
can lag), fail-open (missing/zero data -> no-op), never STOP. Pure parse + assess + an
injected-transport loader. Stdlib.
"""
from __future__ import annotations

ORACLES_URL = "https://api.backed.fi/api/v2/public/oracles"
RESERVES_URL = "https://api.backed.fi/api/v2/public/proof-of-reserves"

# Below this fraction of backing, flag the token as possibly under-collateralized. A
# small slack under 1.0 absorbs benign rounding / reporting lag; a real shortfall is
# larger. HOLD-only.
BACKING_HOLD_RATIO = 0.98


def _fnum(x):
    try:
        return float(str(x))
    except (TypeError, ValueError):
        return None


def _nodes(payload):
    """Tolerant: a bare list, {nodes:[...]}, or {data:{...:[...]}} -> the list."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("nodes", "data", "items", "results"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
        for v in payload.values():
            got = _nodes(v)
            if got:
                return got
    return []


def parse_oracles(payload):
    """PURE: oracles response -> {SYMBOL_UPPER: {underlying, hermes_id, exponent}}.
    Keeps only entries naming a symbol; last write wins. NEVER raises."""
    out = {}
    for e in _nodes(payload):
        if not isinstance(e, dict):
            continue
        sym = (e.get("symbol") or "").upper()
        if not sym:
            continue
        meta = e.get("metadata") if isinstance(e.get("metadata"), dict) else {}
        collat = e.get("collateral") if isinstance(e.get("collateral"), dict) else {}
        hid = meta.get("hermesId")
        out[sym] = {
            "underlying": collat.get("symbol"),
            "hermes_id": hid if isinstance(hid, str) and hid else None,
            "exponent": meta.get("exponent"),
        }
    return out


def parse_reserves(payload):
    """PURE: proof-of-reserves response -> {SYMBOL_UPPER: {shares_held,
    circulating_supply, backing_ratio}}. backing_ratio is None when supply is 0/absent
    (a new/inactive token is UNKNOWN, never 'unbacked'). NEVER raises."""
    out = {}
    for e in _nodes(payload):
        if not isinstance(e, dict):
            continue
        sym = (e.get("symbol") or "").upper()
        if not sym:
            continue
        held = _fnum(e.get("sharesHeld"))
        supply = _fnum(e.get("circulatingSupply"))
        ratio = (held / supply) if (held is not None and supply not in (None, 0)
                                    and supply > 0) else None
        out[sym] = {"shares_held": held, "circulating_supply": supply,
                    "backing_ratio": ratio}
    return out


def assess_backing(reserves_rec, threshold=BACKING_HOLD_RATIO):
    """PURE: a reserves record -> {grade: ready|under_collateralized|unknown, ratio,
    reasons}. `under_collateralized` when backing_ratio < threshold. NEVER raises."""
    rec = reserves_rec if isinstance(reserves_rec, dict) else {}
    ratio = rec.get("backing_ratio")
    if not isinstance(ratio, (int, float)):
        return {"grade": "unknown", "ratio": None, "reasons": []}
    if ratio < threshold:
        return {"grade": "under_collateralized", "ratio": ratio,
                "reasons": ["tokenized security appears UNDER-COLLATERALIZED: only "
                            "%.1f%% backed (shares held vs tokens in circulation)"
                            % (ratio * 100)]}
    return {"grade": "ready", "ratio": ratio,
            "reasons": ["backing confirmed: %.1f%% collateralized" % (ratio * 100)]}


def apply_backing(verdict, signal):
    """PURE fold: annotate signals.backing and escalate GO->HOLD when
    under_collateralized. CONSERVATIVE-ONLY (never upgrades / never STOP); None/unknown
    is a no-op that still records the ratio when present. Non-mutating."""
    if not signal or not isinstance(signal, dict):
        return verdict
    grade = signal.get("grade")
    if grade not in ("ready", "under_collateralized", "unknown"):
        return verdict
    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    v["signals"]["backing"] = {"grade": grade, "ratio": signal.get("ratio")}
    reasons = list(v.get("reasons") or [])
    if grade == "under_collateralized":
        reasons.extend(signal.get("reasons") or [])
        if v.get("verdict") == "GO":
            v["verdict"] = "HOLD"
            reasons.append("escalated GO->HOLD: proof-of-reserves shows the tokenized "
                           "security may not be fully backed")
    v["reasons"] = reasons
    return v


class BackedOracleIndex:
    """Combined oracle + proof-of-reserves index. `feed_map()` feeds PythPriceSource an
    authoritative SYMBOL->hermesId map; `underlying(sym)` / `backing(sym)` serve the
    peg + backing folds. Built from the two loaders; fail-soft (empty on error)."""

    def __init__(self, oracles=None, reserves=None):
        self.oracles = oracles or {}
        self.reserves = reserves or {}

    def feed_map(self):
        # underlying-ticker -> token's hermes feed id, for direct token pricing; also
        # map the token symbol itself. Only entries with a real hermesId.
        m = {}
        for sym, o in self.oracles.items():
            if o.get("hermes_id"):
                m[sym] = o["hermes_id"]
                if o.get("underlying"):
                    m.setdefault(o["underlying"].upper(), o["hermes_id"])
        return m

    def underlying(self, symbol):
        o = self.oracles.get((symbol or "").upper())
        return o.get("underlying") if o else None

    def backing(self, symbol):
        return self.reserves.get((symbol or "").upper())


def _load_paginated(url, get):
    fetch = get or (lambda u: __import__("http_util").get_json(u))
    rows, page = [], 1
    while True:
        u = url + ("&" if "?" in url else "?") + "page=%d" % page
        try:
            payload = fetch(u)
        except Exception:
            break
        rows.extend(_nodes(payload))
        info = payload.get("page") if isinstance(payload, dict) else None
        if not (isinstance(info, dict) and info.get("hasNextPage")) or page >= 200:
            break
        page += 1
    return rows


def load_backed_oracle_index(get=None):
    """Fetch + parse both Backed public endpoints into a BackedOracleIndex. Fail-soft."""
    oracles = parse_oracles(_load_paginated(ORACLES_URL, get))
    reserves = parse_reserves(_load_paginated(RESERVES_URL, get))
    return BackedOracleIndex(oracles=oracles, reserves=reserves)
