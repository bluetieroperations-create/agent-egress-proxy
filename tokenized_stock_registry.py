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
# Fill with VERIFIED addresses only (never guess). Each entry:
#   {"issuer","symbol","isin"?,"underlying_symbol"?,"standard"?,
#    "deployments":[{"network","address"}]}
#
# Ondo tokenized stocks (Ethereum). Doubly-verified 2026-08-17: (a) Ondo's official
# token list at docs.ondo.finance/addresses.md, and (b) an independent on-chain
# `symbol()`/`name()` read per address (eth.blockscout.com / public Ethereum RPC) that
# returned the exact ticker + "(Ondo Tokenized)" name. Addresses lowercased. Other
# issuers (Dinari) and other Ondo chains (BNB/Solana) / Robinhood remain EXCLUDED --
# they could not be corroborated to the two-source standard (see docs/TOKENIZED_RWA.md).
_ONDO_ETH = (
    ("AAPLon", "AAPL", "0x14c3abf95cb9c93a8b82c1cdcb76d72cb87b2d4c"),
    ("TSLAon", "TSLA", "0xf6b1117ec07684d3958cad8beb1b302bfd21103f"),
    ("NVDAon", "NVDA", "0x2d1f7226bd1f780af6b9a49dcc0ae00e8df4bdee"),
    ("MSFTon", "MSFT", "0xb812837b81a3a6b81d7cd74cfb19a7f2784555e5"),
    ("GOOGLon", "GOOGL", "0xba47214edd2bb43099611b208f75e4b42fdcfedc"),
    ("AMZNon", "AMZN", "0xbb8774fb97436d23d74c1b882e8e9a69322cfd31"),
    ("METAon", "META", "0x59644165402b611b350645555b50afb581c71eb2"),
    ("SPYon", "SPY", "0xfedc5f4a6c38211c1338aa411018dfaf26612c08"),
    ("QQQon", "QQQ", "0x0e397938c1aa0680954093495b70a9f5e2249aba"),
)
STATIC_SEED = [
    {"issuer": "ondo", "symbol": sym, "underlying_symbol": und,
     "deployments": [{"network": "ethereum", "address": addr}]}
    for sym, und, addr in _ONDO_ETH
]


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
        self.by_addr = {}         # norm_address -> record (chainless O(1) lookup)
        self.by_symbol = {}       # upper(symbol) -> record (first wins; feeds are canonical)
        self.by_isin = {}         # isin -> record

    def add(self, records):
        for rec in records or []:
            for dep in rec.get("deployments") or []:
                self.by_key[(dep["network"], dep["address"])] = rec
                self.by_addr[dep["address"]] = rec
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
        # Chainless: O(1) via the address index (EVM lowercased, base58 exact).
        return self.by_addr.get(normalize_address(address))

    def by_ticker(self, symbol):
        return self.by_symbol.get((symbol or "").upper())

    def is_known(self, address, chain=None):
        return self.lookup(address, chain) is not None

    def __len__(self):
        return len(self.by_key)


def apply_asset_registry(verdict, record):
    """PURE fold of a tokenized-stock registry record into a verdict dict.

    DESCRIPTIVE (issuer / underlying / isin / standard under signals.rwa_asset) plus
    ONE conservative gate: `trading_halted` -> GO->HOLD (the underlying security's
    trading is halted, so on-chain pricing/redemption may be stale or unavailable).
    CONSERVATIVE-ONLY: never upgrades a HOLD/STOP; a None/empty record is a no-op.
    Returns a NEW dict (does not mutate the input)."""
    if not record or not isinstance(record, dict):
        return verdict
    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    v["signals"]["rwa_asset"] = {
        "issuer": record.get("issuer"),
        "symbol": record.get("symbol"),
        "underlying_symbol": record.get("underlying_symbol"),
        "isin": record.get("isin"),
        "standard": record.get("standard"),
        "trading_halted": bool(record.get("trading_halted")),
    }
    reasons = list(v.get("reasons") or [])
    und = record.get("underlying_symbol")
    reasons.append("acquiring tokenized security %s (issuer %s%s)" % (
        record.get("symbol") or "?", record.get("issuer") or "?",
        ", tracks %s" % und if und else ""))
    if record.get("trading_halted"):
        reasons.append("underlying security trading is HALTED -- on-chain "
                       "pricing/redemption may be stale or unavailable")
        if v.get("verdict") == "GO":
            v["verdict"] = "HOLD"
            reasons.append("escalated GO->HOLD: acquiring a tokenized security whose "
                           "underlying trading is halted")
    v["reasons"] = reasons
    return v


def load_backed(get=None, url=BACKED_ASSETS_URL, paginate=True, max_pages=500):
    """Fetch + parse the keyless Backed/xStocks assets feed -> [records].

    The feed is PAGE-based: `?page=N` (1-indexed, 100 assets/page) with a
    `page: {currentPage, hasNextPage}` envelope. With `paginate=True` (default) we
    walk pages until `hasNextPage` is false (or `max_pages`), so the whole asset set
    is ingested, not just the first 100. `get` is a test seam: callable(url) ->
    parsed JSON. FAIL-SOFT: a mid-walk error keeps the pages already fetched."""
    fetch = get or (lambda u: __import__("http_util").get_json(u))
    records, page = [], 1
    while True:
        page_url = (url + ("&" if "?" in url else "?") + "page=%d" % page
                    if paginate else url)
        try:
            payload = fetch(page_url)
        except Exception:
            break                                  # fail-soft: keep what we have
        recs = parse_backed_assets(payload)
        records.extend(recs)
        if not paginate:
            break
        info = payload.get("page") if isinstance(payload, dict) else None
        has_next = bool(info.get("hasNextPage")) if isinstance(info, dict) else False
        if not has_next or not recs or page >= max_pages:
            break
        page += 1
    return records


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
