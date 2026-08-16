#!/usr/bin/env python3
"""
tokenized_stock_registry.py -- recognize a tokenized-stock/RWA contract + its issuer.

The "who/what is this token" layer under the RWA readiness gate (rwa_readiness.py).
Given a contract address an agent is about to BUY, answer: is this a known tokenized
security, which issuer minted it, and what is the underlying instrument? DESCRIPTIVE
only -- like categories.py, it never gates a verdict; it enriches one (issuer,
underlying symbol/ISIN for a price cross-check, a `standard` hint for the probe).

Discovery is self-assembled (there is no FREE canonical cross-issuer registry -- the
only one, rwa.xyz, is paid/Enterprise). The best keyless source is the ISSUER's own
public feed; Backed / xStocks is the gem:

    GET https://api.backed.fi/api/v2/public/assets   (no API key)

-> a `nodes[]` array, each asset carrying symbol (e.g. "NVDAx"), isin,
underlyingSymbol, isTradingHalted, and a `deployments[]` array of {network, address}
across chains (Ethereum / Solana / Arbitrum / Mantle / Ink / TON). That IS a
machine-readable ticker -> contract-address map, ingestible like discovery_crawl.py.

Gated issuers (Ondo, Dinari, Robinhood) do not expose a keyless address list, so
their entries come from an OPERATOR-SUPPLIED static seed (`STATIC_SEED`, EMPTY by
default -- we NEVER fabricate on-chain addresses; a wrong address would mis-identify
a token). Scrape those once from an explorer and add them.

Address normalization is chain-aware: EVM addresses lowercase (case-insensitive);
Solana addresses are base58 and CASE-SENSITIVE -- never lowercased.

Pure core + an injected-transport live loader. Stdlib only (reuses http_util).
"""
from __future__ import annotations

import re

_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

BACKED_ASSETS_URL = "https://api.backed.fi/api/v2/public/assets"

# Operator-supplied entries for issuers without a keyless feed (Ondo/Dinari/Robinhood).
# EMPTY by default -- fill with VERIFIED addresses only (never guess). Each entry:
#   {"issuer","symbol","isin"?,"underlying_symbol"?,"standard"?,
#    "deployments":[{"network","address"}]}
STATIC_SEED = []


def _norm_network(net):
    return (net or "").strip().lower()


def normalize_address(address, network=None):
    """Chain-aware address normalization. EVM (0x+40hex) -> lowercase; a Solana /
    other base58 address is returned unchanged (case-significant). None if empty."""
    if not isinstance(address, str) or not address.strip():
        return None
    a = address.strip()
    if _EVM_RE.match(a):
        return a.lower()
    return a                       # base58 (Solana/TON) -- do NOT lowercase


def _first_list(obj, keys=("nodes", "assets", "data", "items", "results")):
    """Best-effort: find the assets array in a possibly-nested API envelope.
    Accepts a bare list, {nodes:[...]}, {data:{assets:{nodes:[...]}}}, etc."""
    if isinstance(obj, list):
        return obj
    if not isinstance(obj, dict):
        return []
    # direct hit
    for k in keys:
        v = obj.get(k)
        if isinstance(v, list):
            return v
    # one level of nesting under any key
    for v in obj.values():
        got = _first_list(v, keys) if isinstance(v, (dict, list)) else []
        if got:
            return got
    return []


def _record_from_node(node, issuer="backed"):
    """Normalize one Backed asset node -> a registry record, or None if it names no
    deployment address. Tolerant of missing optional fields."""
    if not isinstance(node, dict):
        return None
    symbol = node.get("symbol") or node.get("ticker")
    deployments = []
    for d in node.get("deployments") or []:
        if not isinstance(d, dict):
            continue
        net = _norm_network(d.get("network") or d.get("chain"))
        addr = normalize_address(d.get("address"), net)
        if net and addr:
            dep = {"network": net, "address": addr}
            wrap = normalize_address(d.get("wrapperAddressV2") or d.get("wrapperAddress"), net)
            if wrap:
                dep["wrapper_address"] = wrap
            deployments.append(dep)
    if not deployments:
        return None
    return {
        "issuer": issuer,
        "symbol": symbol,
        "name": node.get("name"),
        "isin": node.get("isin"),
        "underlying_symbol": node.get("underlyingSymbol") or node.get("underlying_symbol"),
        "underlying_isin": node.get("underlyingIsin") or node.get("underlying_isin"),
        "trading_halted": bool(node.get("isTradingHalted") or node.get("trading_halted")),
        "standard": node.get("standard"),
        "deployments": deployments,
    }


def parse_backed_assets(payload, issuer="backed"):
    """PURE: Backed `/public/assets` response (any reasonable envelope) -> [records].
    Skips malformed nodes and nodes with no deployment address. NEVER raises."""
    out = []
    for node in _first_list(payload):
        rec = _record_from_node(node, issuer=issuer)
        if rec is not None:
            out.append(rec)
    return out


def parse_seed(seed):
    """PURE: validate + normalize operator STATIC_SEED entries -> [records]. Drops any
    entry without at least one {network, address} deployment (no fabricated address
    survives as a usable lookup). NEVER raises."""
    out = []
    for e in seed or []:
        if not isinstance(e, dict):
            continue
        deployments = []
        for d in e.get("deployments") or []:
            if not isinstance(d, dict):
                continue
            net = _norm_network(d.get("network") or d.get("chain"))
            addr = normalize_address(d.get("address"), net)
            if net and addr:
                deployments.append({"network": net, "address": addr})
        if not deployments:
            continue
        out.append({
            "issuer": e.get("issuer") or "seed",
            "symbol": e.get("symbol"),
            "name": e.get("name"),
            "isin": e.get("isin"),
            "underlying_symbol": e.get("underlying_symbol"),
            "underlying_isin": e.get("underlying_isin"),
            "trading_halted": bool(e.get("trading_halted")),
            "standard": e.get("standard"),
            "deployments": deployments,
        })
    return out


class TokenizedStockRegistry:
    """Indexed, chain-aware lookup over normalized tokenized-stock records.

    `lookup(address, chain=None)` is the hot path used by an enrichment fold: given
    the token an agent is buying, return its record (issuer/underlying/standard) or
    None. Later records with the same (chain, address) OVERRIDE earlier ones, so an
    operator seed can correct an ingested feed by loading it last."""

    def __init__(self):
        self.by_key = {}          # (network, norm_address) -> record
        self.by_symbol = {}       # upper(symbol) -> record (first wins; feeds are canonical)
        self.by_isin = {}         # isin -> record

    def add(self, records):
        for rec in records or []:
            for dep in rec.get("deployments") or []:
                self.by_key[(dep["network"], dep["address"])] = rec
            sym = (rec.get("symbol") or "").upper()
            if sym and sym not in self.by_symbol:
                self.by_symbol[sym] = rec
            isin = rec.get("isin")
            if isin and isin not in self.by_isin:
                self.by_isin[isin] = rec
        return self

    def lookup(self, address, chain=None):
        """Record for a contract address, or None. When `chain` is given the match is
        exact; otherwise the address is matched on ANY chain (first hit)."""
        if not address:
            return None
        if chain is not None:
            net = _norm_network(chain)
            return self.by_key.get((net, normalize_address(address, net)))
        na_evm = normalize_address(address)
        for (net, addr), rec in self.by_key.items():
            if addr == normalize_address(address, net) or addr == na_evm:
                return rec
        return None

    def by_ticker(self, symbol):
        return self.by_symbol.get((symbol or "").upper())

    def is_known(self, address, chain=None):
        return self.lookup(address, chain) is not None

    def __len__(self):
        return len(self.by_key)


def load_backed(get=None, url=BACKED_ASSETS_URL):
    """Fetch + parse the keyless Backed/xStocks assets feed -> [records].
    `get` is a test seam: callable(url) -> parsed JSON. Fail-soft: [] on any error."""
    fetch = get or (lambda u: __import__("http_util").get_json(u))
    try:
        payload = fetch(url)
    except Exception:
        return []
    return parse_backed_assets(payload)


def build_registry(get=None, seed=None):
    """Assemble a TokenizedStockRegistry from the Backed feed + the operator seed
    (seed loaded LAST so it can override). Fail-soft."""
    reg = TokenizedStockRegistry()
    reg.add(load_backed(get=get))
    reg.add(parse_seed(seed if seed is not None else STATIC_SEED))
    return reg


def main(argv=None):
    """Fetch the keyless Backed feed and print a compact registry summary."""
    import json
    import sys
    reg = build_registry()
    rows = []
    seen = set()
    for (net, addr), rec in sorted(reg.by_key.items()):
        key = (rec.get("symbol"), net)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"symbol": rec.get("symbol"), "underlying": rec.get("underlying_symbol"),
                     "network": net, "address": addr, "issuer": rec.get("issuer")})
    sys.stdout.write(json.dumps(
        {"tokenized_stocks": len(reg), "distinct_deployments": len(rows),
         "sample": rows[:25]}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
