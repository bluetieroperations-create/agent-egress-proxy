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

import sys
from decimal import Decimal

USDC_DECIMALS = 6
THIN_DISTINCT = 3          # < this many distinct payers == not broadly used (Sybil-ish)
CANDIDATE_MIN_RESOURCES = 1


def _human(atomic, decimals=USDC_DECIMALS):
    try:
        d = Decimal(int(atomic)) / (Decimal(10) ** decimals)
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
        prices = [r["price_atomic"] for r in rs if r.get("price_atomic")]
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
        profiles.append({
            "payee": payee,
            "resource_count": len(rs),
            "resources": sorted({r.get("resource") for r in rs if r.get("resource")}),
            "networks": sorted({r.get("network") for r in rs if r.get("network")}),
            "min_price": _human(min(prices)) if prices else None,
            "max_price": _human(max(prices)) if prices else None,
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
    n_resources = sum(p["resource_count"] for p in profiles)
    sanctioned = [p for p in profiles if p["sanctioned"]]
    with_hist = [p for p in profiles if p.get("distinct_payers") is not None]
    thin = [p for p in with_hist if p["thin"]]
    prices = sorted(r["price_atomic"] for r in (resources or [])
                    if r.get("price_atomic"))
    price_dist = None
    if prices:
        def pct(q):
            return _human(prices[min(len(prices) - 1, int(q * len(prices)))])
        price_dist = {"min": _human(prices[0]), "p50": pct(0.5), "p90": pct(0.9),
                      "max": _human(prices[-1]), "count": len(prices)}
    # concentration: share of endpoints offering multiple resources
    multi = [p for p in profiles if p["resource_count"] > 1]
    return {
        "endpoints": n_endpoints,
        "resources": n_resources,
        "networks": sorted({n for p in profiles for n in p["networks"]}),
        "sanctioned": len(sanctioned),
        "with_onchain_history": len(with_hist),
        "thin_distinct_payers": len(thin),
        "multi_resource_endpoints": len(multi),
        "price_usdc": price_dist,
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


def _load_sanctioned():
    try:
        import sanctions
        with open("sanctions.txt", "r", encoding="utf-8") as f:
            return list(sanctions.parse_sanctioned_addresses(f.read()))
    except Exception:
        return []


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
    out = scan(resources, sanctioned=_load_sanctioned(), store=store)

    s = out["stats"]
    sys.stdout.write("x402 scan: %d endpoints, %d resources, %d chains; backfilled %d "
                     "(with history %d, thin %d).\n"
                     % (s["endpoints"], s["resources"], len(s["networks"]), len(sample),
                        s["with_onchain_history"], s["thin_distinct_payers"]))
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
                            (c["resources"][0] if c["resources"] else "")])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
