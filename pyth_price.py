#!/usr/bin/env python3
"""
pyth_price.py -- peg / NAV divergence for a tokenized-stock buy, from the FREE keyless
Pyth Hermes oracle.

A tokenized stock should trade ~1:1 with its underlying equity. When an agent is about
to pay stablecoins for one, the useful question a payment firewall can answer -- and the
RWA analogue of Blackwall's core price-anomaly gate -- is:

    Am I paying near the REAL stock price, or a big premium (a depeg / stale-quote /
    bad-route overpay)?

We get the underlying's live price from Pyth (`hermes.pyth.network`, keyless): resolve
the ticker to an equity FEED, read the latest price. Compare it to the per-unit price
the agent is paying (from `acquires.unit_price`, or `amount / acquires.quantity`).

CONSERVATIVE, mirroring the existing anomaly gate: only OVERPAY counts (a discount is
never anomalous); a divergence >= PEG_HOLD_RATIO escalates GO->HOLD (HOLD-only -- an
external oracle can be stale, so never a STOP). FAIL-OPEN: no feed / no price / no unit
price -> no gate, verdict proceeds. Pure core + an injected-transport source. Stdlib.
"""
from __future__ import annotations

HERMES_BASE = "https://hermes.pyth.network"
PEG_HOLD_RATIO = 1.10          # paying >= 10% over the live underlying -> HOLD


def pyth_price_value(price_obj):
    """PURE: a Pyth price object {price, expo} -> float, or None. Pyth encodes the
    price as an integer mantissa `price` scaled by 10**`expo` (expo is negative)."""
    if not isinstance(price_obj, dict):
        return None
    try:
        return int(price_obj["price"]) * (10.0 ** int(price_obj["expo"]))
    except (KeyError, TypeError, ValueError):
        return None


def select_equity_feed(feeds, base):
    """PURE: from a Pyth `/v2/price_feeds` search result, pick the best feed id for an
    equity `base` ticker -- exact base match, asset_type Equity, preferring a
    non-DEPRECATED, non-overnight (".ON") feed. None if no match."""
    base_u = (base or "").upper()
    best = None
    for f in feeds or []:
        if not isinstance(f, dict):
            continue
        attrs = f.get("attributes") or {}
        if (attrs.get("base") or "").upper() != base_u:
            continue
        if (attrs.get("asset_type") or "").lower() != "equity":
            continue
        desc = (attrs.get("description") or "").upper()
        sym = attrs.get("symbol") or ""
        score = (0 if "DEPRECATED" in desc else 2) + (0 if sym.endswith(".ON") else 1)
        if best is None or score > best[0]:
            best = (score, f.get("id"))
    return best[1] if best else None


def peg_divergence_ratio(paid_unit_price, underlying_price):
    """PURE: paid-per-unit / live-underlying. >1 = overpaying, <1 = discount. None on
    bad/zero inputs. (Only overpay is treated as anomalous by the caller.)"""
    try:
        paid, under = float(paid_unit_price), float(underlying_price)
    except (TypeError, ValueError):
        return None
    if paid <= 0 or under <= 0:
        return None
    return paid / under


def _paid_unit_price(amount, acquires):
    """Derive the per-unit price the agent is paying: explicit `acquires.unit_price`,
    else `amount / acquires.quantity`. None when neither is available."""
    acq = acquires or {}
    if acq.get("unit_price") is not None:
        try:
            return float(acq["unit_price"])
        except (TypeError, ValueError):
            return None
    try:
        qty = float(acq.get("quantity"))
        return float(amount) / qty if qty > 0 else None
    except (TypeError, ValueError):
        return None


def compute_peg(price_source, underlying_symbol, amount, acquires):
    """Assemble a peg record for a tokenized-stock buy, or None when it can't be
    computed (no underlying symbol, or no derivable unit price). FAIL-OPEN: an
    unreachable oracle yields underlying_price=None (still records the paid price)."""
    if not underlying_symbol:
        return None
    unit = _paid_unit_price(amount, acquires)
    if unit is None:
        return None
    try:
        under = price_source.price(underlying_symbol) if price_source else None
    except Exception:
        under = None
    ratio = peg_divergence_ratio(unit, under) if under is not None else None
    return {"underlying_symbol": underlying_symbol, "underlying_price": under,
            "paid_unit_price": unit, "divergence_ratio": ratio}


def apply_peg(verdict, peg, threshold=PEG_HOLD_RATIO):
    """PURE fold of a peg record into a verdict. Annotates signals.peg; CONSERVATIVELY
    escalates GO->HOLD when overpaying >= `threshold` the live underlying. Never
    upgrades; None/empty peg is a no-op; non-mutating."""
    if not peg or not isinstance(peg, dict):
        return verdict
    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    v["signals"]["peg"] = {
        "underlying_symbol": peg.get("underlying_symbol"),
        "underlying_price": peg.get("underlying_price"),
        "paid_unit_price": peg.get("paid_unit_price"),
        "divergence_ratio": (round(peg["divergence_ratio"], 4)
                             if peg.get("divergence_ratio") is not None else None),
    }
    reasons = list(v.get("reasons") or [])
    ratio = peg.get("divergence_ratio")
    sym, under = peg.get("underlying_symbol"), peg.get("underlying_price")
    if ratio is not None and ratio >= threshold:
        reasons.append("paying %.2fx the live underlying (%s $%.2f) for this tokenized "
                       "security -- overpaying vs the real stock price" % (ratio, sym, under))
        if v.get("verdict") == "GO":
            v["verdict"] = "HOLD"
            reasons.append("escalated GO->HOLD: tokenized security priced >= %.0f%% over "
                           "its underlying (possible depeg / stale quote / bad route)"
                           % ((threshold - 1) * 100))
    elif ratio is not None and under is not None:
        reasons.append("tokenized security within peg: %s at $%.2f, paying %.2fx"
                       % (sym, under, ratio))
    v["reasons"] = reasons
    return v


class PythPriceSource:
    """Live underlying-equity price via the keyless Pyth Hermes API. `price(symbol)`
    resolves the ticker to an equity feed (cached) and reads its latest price.
    FAIL-OPEN (None on any error). `get` is the test seam: callable(url) -> JSON."""

    def __init__(self, hermes=HERMES_BASE, get=None, feed_map=None):
        self.hermes = (hermes or HERMES_BASE).rstrip("/")
        self._get_json = get
        self.feed_map = {k.upper(): v for k, v in (feed_map or {}).items()}
        self._resolved = {}

    def price(self, symbol):
        if not symbol:
            return None
        fid = self._feed_id(symbol.upper())
        if not fid:
            return None
        try:
            d = self._get(self.hermes + "/v2/updates/price/latest?ids[]=" + fid)
            parsed = d.get("parsed") if isinstance(d, dict) else None
            pr = parsed[0].get("price") if isinstance(parsed, list) and parsed else None
            return pyth_price_value(pr)
        except Exception:
            return None

    def _feed_id(self, base):
        if base in self.feed_map:
            return self.feed_map[base]
        if base in self._resolved:
            return self._resolved[base]
        try:
            feeds = self._get(self.hermes + "/v2/price_feeds?query=" + base
                              + "&asset_type=equity")
            fid = select_equity_feed(feeds, base)
        except Exception:
            fid = None
        self._resolved[base] = fid
        return fid

    def _get(self, url):
        if self._get_json is not None:
            return self._get_json(url)
        import http_util
        return http_util.get_json(url)
