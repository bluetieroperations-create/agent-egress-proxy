#!/usr/bin/env python3
"""
ecosystem_scan.py -- turn the x402 map into four things.

The discovery crawl + backfill give a live map of the x402 economy. This module
folds that map into per-ENDPOINT profiles (one per payee) and derives four outputs
from the same pass:

  1. INSTANT VERDICTS -- a pre-warmed reputation corpus (the populated
     ReputationStore) so the engine answers "is this payee safe?" for a known
     endpoint with real history, not a cold-start HOLD.
  2. STATE OF x402 -- `ecosystem_stats()`: endpoint/resource counts, price
     distribution, concentration, sanctioned/thin counts. A shareable report.
  3. TRUST DIRECTORY -- `rank_directory()`: endpoints ranked by an explainable
     trust score (distinct payers, volume, breadth; sanctioned sink to the bottom).
  4. BD FUNNEL -- `audit_candidates()`: active, clean, not-yet-verified endpoints
     worth reaching out to for the seller-audit ("Verified") tier.

Pure + stdlib; enrichment inputs (sanctioned set, reputation store, verified
registry) are injected, so the core is fully unit-testable. Prices are atomic USDC
(6dp) reported in human units.
"""
from __future__ import annotations

import re
import sys
from decimal import Decimal
from urllib.parse import urlparse

USDC_DECIMALS = 6
THIN_DISTINCT = 3          # < this many distinct payers == not broadly used (Sybil-ish)
CANDIDATE_MIN_RESOURCES = 1

# Service-category heuristics for a payee, matched (in order, first hit wins)
# against each resource's host+path. DESCRIPTIVE ONLY -- a coarse map of WHAT the
# x402 economy sells; it never touches a verdict. Classification of arbitrary API
# paths is inherently fuzzy, so ~a third land in "other" -- that's expected, not a
# bug. Extend the patterns as the ecosystem's vocabulary grows.
CATEGORY_UNCLASSIFIED = "other"
CATEGORY_RULES = (
    ("commerce",  r"gift|product|checkout|invoice|esim|shop|store/|cart|/buy|order|refill|billing|catalog"),
    ("dev-tools", r"cors|/hash|hmac|/json|/curl|http-status|/echo|/proxy|status-check|base64|/uuid|regex|/dns|whois|domain-|geocode|/resolve|translate"),
    ("onchain",   r"/chain|/block|/tx|erc20|erc721|token-|/nonce|/receipt|contract|/gas|/rpc|holder|/wallet|/address|/ens|mainnet|quicknode|/node|solana|onchain"),
    ("ai-agents", r"\.ai|/ai/|llm|gpt|deepseek|/chat|completion|/ask|/agent|inference|/mcp|/model|prompt|embed|/rag|/analyze|generate|summar"),
    ("finance",   r"quote|/trade|perp|yield|swap|/price|/market|signal|macro|finance|/fund|/risk|portfolio|defi|tradfi|altcoin|sentiment|equity|stablecoin|/rates|/stock|ticker|earnings|coin"),
    ("search-data", r"search|extract|scrape|/fetch|/data|edgar|filing|/company|weather|timezone|/geo|/trend|lookup|/report|hackernews"),
    ("content-media", r"content|news|recipe|joke|/fact|/feed|/tick|/image|/video|meme|/story|/blog|entertainment|twitter"),
    ("email-comms", r"/mail|email|/send|inbox|/message|notify|/sms"),
    ("storage-files", r"upload|storage|/file|ipfs|/pin"),
    ("identity-security", r"/auth|verify|sanction|kyc|captcha|identity|/login"),
)


def classify_resource(url):
    """Category slug for a single resource URL (host+path keyword match), or
    CATEGORY_UNCLASSIFIED. PURE."""
    u = urlparse(url or "")
    hay = ((u.hostname or "") + " " + (u.path or "")).lower()
    for name, pat in CATEGORY_RULES:
        if re.search(pat, hay):
            return name
    return CATEGORY_UNCLASSIFIED


def classify_category(resources):
    """Best-effort dominant service category for a payee across its resource URLs.
    A classified category beats 'other' on a tie, so a single unclassifiable URL
    among classified ones doesn't hide the signal. PURE + heuristic -- descriptive,
    never gates a verdict."""
    counts = {}
    for url in resources or []:
        c = classify_resource(url)
        counts[c] = counts.get(c, 0) + 1
    if not counts:
        return CATEGORY_UNCLASSIFIED
    # dominant by count; on ties prefer a real category over 'other', then name.
    return max(counts, key=lambda c: (counts[c], c != CATEGORY_UNCLASSIFIED, c))

# Canonical native USDC (Circle), 6 decimals, keyed by contract. A price is only
# rendered in USD when its `asset` is one of these -- otherwise the atomic amount's
# decimals are UNKNOWN (an 18-dp token divided by 10^6 reads as ~10^12x too large;
# that decimals mismatch, not a real "$10T price", is what an agent must not sign).
USDC_6DP = frozenset({
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # Base
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # Polygon PoS
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # Arbitrum
    "0x0b2c639c533813f4aa9d7837caf62653d097ff85",  # Optimism
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # Ethereum
    "0xb97ad0e74fa7d920791e90258a6e2085088b4320",  # Avalanche
    "0x036cbd53842c5426634e7929541ec2318f3dcf7e",  # Base Sepolia (test)
    "0x1c7d4b196cb0c7b01d743fbc6116a902379c7238",  # Sepolia (test)
})


def _is_usdc6(asset):
    return isinstance(asset, str) and asset.lower() in USDC_6DP


def _usdc_price_atomic(rec):
    """The record's price in atomic USDC-6dp, or None when the asset isn't a known
    6-decimal USDC (unknown decimals -> not comparable in USD) or the amount is
    non-positive. Guards the whole price layer against the decimals mismatch."""
    pa = rec.get("price_atomic")
    if not _is_usdc6(rec.get("asset")):
        return None
    try:
        pa = int(pa)
    except (TypeError, ValueError):
        return None
    return pa if pa > 0 else None


def _human(atomic, decimals=USDC_DECIMALS):
    try:
        d = Decimal(int(atomic)) / (Decimal(10) ** decimals)
        if d <= 0:
            return None
        return format(d, "f")
    except Exception:
        return None


def group_by_payee(resources):
    """Group crawled resource records by their payTo address."""
    by = {}
    for r in resources or []:
        p = r.get("payTo")
        if p:
            by.setdefault(p, []).append(r)
    return by


def build_profiles(resources, *, sanctioned=None, store=None):
    """One profile per endpoint operator (payee). Enriches with a sanctions flag
    and, when `store` has it, on-chain reputation (settlement_count / distinct
    payers). Pure given the inputs."""
    sanc = {str(s).lower() for s in (sanctioned or [])}
    profiles = []
    for payee, rs in group_by_payee(resources).items():
        # USD prices only from known 6-dp USDC assets (see _usdc_price_atomic).
        prices = [pa for pa in (_usdc_price_atomic(r) for r in rs) if pa]
        non_usdc = sum(1 for r in rs if r.get("price_atomic") and not _is_usdc6(r.get("asset")))
        rec = {}
        if store is not None:
            try:
                rec = store.lookup(payee) or {}
            except Exception:
                rec = {}
        settlements = int(rec.get("settlement_count", 0) or 0)
        distinct = rec.get("distinct_payers")
        if settlements == 0:
            # no ingested history for this payee (not backfilled, or none on-chain)
            # -> UNKNOWN, not a real "0 distinct payers" -> don't count it as thin.
            distinct = None
        # A single x402 resource can list MANY `accepts` (same URL priced on several
        # networks/assets). `resource_count` is DISTINCT resource URLs (real breadth);
        # `option_count` is the raw accept/payment-option count. Conflating them
        # inflates "resources", "multi-resource" share, and the price distribution.
        distinct_resources = sorted({r.get("resource") for r in rs if r.get("resource")})
        profiles.append({
            "payee": payee,
            "category": classify_category(distinct_resources),
            "resource_count": len(distinct_resources),
            "option_count": len(rs),
            "resources": distinct_resources,
            "networks": sorted({r.get("network") for r in rs if r.get("network")}),
            "min_price": _human(min(prices)) if prices else None,
            "max_price": _human(max(prices)) if prices else None,
            "non_usdc_options": non_usdc,   # priced in a non-USDC / unknown-dp asset
            "sanctioned": (payee in sanc) or bool(rec.get("sanctioned")),
            "settlement_count": int(rec.get("settlement_count", 0) or 0),
            "distinct_payers": distinct,
            "thin": distinct is not None and distinct < THIN_DISTINCT,
        })
    return profiles


def trust_score(profile):
    """Explainable trust score for the directory. Sanctioned -> 0. Otherwise driven
    by DISTINCT PAYERS (real, hard-to-fake diversity), then volume, then breadth."""
    if profile.get("sanctioned"):
        return 0.0
    # No on-chain history -> UNKNOWN, score 0. Never let attacker-controllable
    # advertised breadth alone lift a no-history endpoint up the directory.
    if profile.get("distinct_payers") is None:
        return 0.0
    distinct = profile.get("distinct_payers") or 0
    settlements = profile.get("settlement_count") or 0
    breadth = profile.get("resource_count") or 0
    # distinct payers dominate; volume/breadth are log-damped tie-breakers.
    from math import log1p
    return round(distinct * 10.0 + log1p(settlements) * 2.0 + log1p(breadth), 3)


def rank_directory(profiles, *, top=None):
    """Endpoints ranked by trust_score (desc), each carrying its score + a reason."""
    ranked = []
    for p in profiles:
        score = trust_score(p)
        if p.get("sanctioned"):
            reason = "sanctioned / known-bad"
        elif p.get("distinct_payers") is None:
            reason = "no on-chain history yet"
        elif p.get("thin"):
            reason = "few distinct payers (thin)"
        else:
            reason = "%d distinct payers, %d settlements" % (
                p.get("distinct_payers") or 0, p.get("settlement_count") or 0)
        ranked.append(dict(p, trust_score=score, trust_reason=reason))
    ranked.sort(key=lambda x: (x["trust_score"], x["resource_count"]), reverse=True)
    return ranked[:top] if top else ranked


def audit_candidates(profiles, *, verified=None, min_resources=CANDIDATE_MIN_RESOURCES):
    """BD funnel: endpoints worth pitching the Verified tier -- active, clean, and
    not already verified. `verified` is a set/collection of already-verified payees."""
    ver = {str(v).lower() for v in (verified or [])}
    out = []
    for p in profiles:
        if p.get("sanctioned"):
            continue
        if p["payee"] in ver:
            continue
        if p["resource_count"] < min_resources:
            continue
        out.append(dict(p, opportunity=(p.get("settlement_count") or 0)
                        + (p.get("resource_count") or 0) * 5))
    out.sort(key=lambda x: x["opportunity"], reverse=True)
    return out


def ecosystem_stats(profiles, resources=None):
    """State-of-x402 aggregates for a report."""
    n_endpoints = len(profiles)
    n_resources = sum(p["resource_count"] for p in profiles)          # distinct URLs
    n_options = sum(p.get("option_count", p["resource_count"]) for p in profiles)
    sanctioned = [p for p in profiles if p["sanctioned"]]
    with_hist = [p for p in profiles if p.get("distinct_payers") is not None]
    thin = [p for p in with_hist if p["thin"]]
    # USD distribution: KNOWN 6-dp USDC assets only (else decimals are unknown and
    # an 18-dp amount reads ~10^12x too large). One price per DISTINCT
    # (payee, resource, price) so a resource priced identically across several
    # networks isn't counted many times. Non-USDC priced options are counted
    # separately, not folded into the USD numbers.
    seen_price, prices = set(), []
    non_usdc_priced = 0
    for r in (resources or []):
        if not r.get("price_atomic"):
            continue
        pa = _usdc_price_atomic(r)
        if pa is None:
            non_usdc_priced += 1
            continue
        key = (r.get("payTo"), r.get("resource"), pa)
        if key in seen_price:
            continue
        seen_price.add(key)
        prices.append(pa)
    prices.sort()
    price_dist = None
    if prices:
        n = len(prices)
        def pct(q):
            # nearest-rank on positions 0..n-1 (no interpolation); q=0.9 never
            # degenerates to max the way int(q*n) does for small n.
            return _human(prices[min(n - 1, int(q * (n - 1)))])
        price_dist = {"min": _human(prices[0]), "p50": pct(0.5), "p90": pct(0.9),
                      "max": _human(prices[-1]), "count": n}
    # concentration: share of endpoints offering multiple resources
    multi = [p for p in profiles if p["resource_count"] > 1]
    # what the x402 economy SELLS: payees per service category (descriptive).
    cat_dist = {}
    for p in profiles:
        c = p.get("category", CATEGORY_UNCLASSIFIED)
        cat_dist[c] = cat_dist.get(c, 0) + 1
    cat_dist = dict(sorted(cat_dist.items(), key=lambda kv: (-kv[1], kv[0])))
    return {
        "endpoints": n_endpoints,
        "resources": n_resources,
        "payment_options": n_options,
        "networks": sorted({n for p in profiles for n in p["networks"]}),
        "sanctioned": len(sanctioned),
        "with_onchain_history": len(with_hist),
        "thin_distinct_payers": len(thin),
        "multi_resource_endpoints": len(multi),
        "category_distribution": cat_dist,
        "price_usdc": price_dist,
        "non_usdc_priced_options": non_usdc_priced,
    }


def scan(resources, *, sanctioned=None, store=None, verified=None):
    """One pass -> all four views."""
    profiles = build_profiles(resources, sanctioned=sanctioned, store=store)
    return {
        "profiles": profiles,
        "stats": ecosystem_stats(profiles, resources),
        "directory": rank_directory(profiles),
        "candidates": audit_candidates(profiles, verified=verified),
    }


def _csv_safe(v):
    """Neutralize CSV/spreadsheet formula injection: a cell a BD analyst opens in
    Excel/Sheets that starts with = + - @ (or a leading tab/CR) would execute. The
    `resource` field comes from an untrusted discovery doc, so prefix it with a
    quote. See OWASP 'CSV Injection'."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _load_sanctioned():
    """Return (addresses, ok). ok=False means screening was UNAVAILABLE (missing/
    unreadable list) -> the scan must not silently report 'sanctioned: 0' as if it
    screened. Fails open on data, but flags it."""
    try:
        import sanctions
        with open("sanctions.txt", "r", encoding="utf-8") as f:
            return list(sanctions.parse_sanctioned_addresses(f.read())), True
    except Exception:
        return [], False


def main(argv=None):
    import argparse
    import collections
    import csv
    import json
    p = argparse.ArgumentParser(
        description="Scan the x402 ecosystem -> instant-verdict corpus + report + directory + BD funnel.")
    p.add_argument("--backfill-store", required=True,
                   help="ReputationStore path -- the INSTANT-VERDICT corpus (#1)")
    p.add_argument("--max-pages", type=int, default=6, help="CDP Bazaar pages to crawl")
    p.add_argument("--backfill-top", type=int, default=25,
                   help="backfill on-chain reputation for the N most-active endpoints")
    p.add_argument("--backfill-max-pages", type=int, default=2)
    p.add_argument("--out-report", help="write State-of-x402 stats JSON (#2)")
    p.add_argument("--out-directory", help="write the ranked trust directory JSON (#3)")
    p.add_argument("--out-candidates", help="write the BD funnel CSV (#4)")
    args = p.parse_args(argv)

    import chain_backfill
    import discovery_crawl
    from reputation_store import ReputationStore

    resources = discovery_crawl.crawl_all(max_pages=args.max_pages)
    rc = collections.Counter(r["payTo"] for r in resources if r.get("payTo"))
    sample = [a for a, _ in rc.most_common(args.backfill_top)]
    store = ReputationStore(args.backfill_store)                       # <- the #1 corpus
    chain_backfill.backfill(store, sample, chain_backfill.BlockscoutPager().fetch,
                            max_pages=args.backfill_max_pages)
    sanc, screened = _load_sanctioned()
    if not screened:
        sys.stderr.write("WARNING: sanctions list unavailable -- 'sanctioned: 0' means "
                         "NOT SCREENED, not clean.\n")
    out = scan(resources, sanctioned=sanc, store=store)

    s = out["stats"]
    sys.stdout.write("x402 scan: %d endpoints, %d resources (%d options, %d non-USDC), "
                     "%d chains; backfilled %d (with history %d, thin %d).\n"
                     % (s["endpoints"], s["resources"], s["payment_options"],
                        s.get("non_usdc_priced_options", 0), len(s["networks"]),
                        len(sample), s["with_onchain_history"], s["thin_distinct_payers"]))
    if args.out_report:
        json.dump(s, open(args.out_report, "w"), indent=2, default=str)
    if args.out_directory:
        json.dump([d for d in out["directory"] if d.get("distinct_payers") is not None],
                  open(args.out_directory, "w"), indent=2, default=str)
    if args.out_candidates:
        cands = out["candidates"]
        with open(args.out_candidates, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["payee", "resource_count", "min_price", "max_price",
                        "settlement_count", "opportunity", "resource"])
            for c in cands:
                w.writerow([c["payee"], c["resource_count"], c["min_price"],
                            c["max_price"], c["settlement_count"], c["opportunity"],
                            _csv_safe(c["resources"][0] if c["resources"] else "")])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
