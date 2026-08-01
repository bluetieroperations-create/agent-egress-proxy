#!/usr/bin/env python3
"""
discovery_crawl.py -- map the x402 seller ecosystem, and auto-feed the moat.

`chain_backfill.py` seeds reputation from public data but needs a LIST of payee
addresses. This crawler produces that list automatically: it fetches x402 discovery
sources (a Bazaar/facilitator resource list, a directory, or an endpoint's own 402
challenge / `/.well-known/x402` descriptor), parses the standard x402 `accepts`
shape, and extracts each resource's `payTo` + advertised price + asset/network.

One crawl feeds THREE Blackwall signals with zero customers:
  * `payees()`             -> `chain_backfill` (counterparty REPUTATION).
  * `price_observations()` -> peer-group PRICE baselines (what a class charges).
  * resource URLs          -> `readiness` targets.
And it yields a MAP of every x402 endpoint + its price -- competitive/BD intel.

Parsing is shape-tolerant (v1 `maxAmountRequired` / v2 `amount`; `accepts` on a 402
body, a resource record, or nested under `items`/`resources`/`data`). Pure + stdlib;
the fetch transport is injectable. Never raises on a malformed doc.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from decimal import Decimal

from addresses import is_evm_address

_MAX_DEPTH = 6
_NESTED_KEYS = ("items", "resources", "data", "results", "endpoints")


def _price_atomic(accept):
    raw = accept.get("amount", accept.get("maxAmountRequired"))
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def _accept_record(accept, parent_resource):
    if not isinstance(accept, dict):
        return None
    payto = accept.get("payTo") or accept.get("pay_to")
    if not is_evm_address(payto):
        return None
    resource = accept.get("resource") or parent_resource
    return {"resource": resource, "payTo": payto.lower(),
            "asset": accept.get("asset"), "network": accept.get("network"),
            "price_atomic": _price_atomic(accept)}


def extract_resources(doc, _depth=0):
    """Recursively pull x402 resource records from a discovery doc / 402 body /
    resource list -> [{resource, payTo, asset, network, price_atomic}]. Bounded
    depth; never raises."""
    out = []
    if _depth > _MAX_DEPTH or doc is None:
        return out
    if isinstance(doc, list):
        for d in doc:
            out.extend(extract_resources(d, _depth + 1))
        return out
    if not isinstance(doc, dict):
        return out
    parent_resource = doc.get("resource")
    if isinstance(parent_resource, dict):          # v2 ResourceInfo {url: ...}
        parent_resource = parent_resource.get("url")
    accepts = doc.get("accepts")
    if isinstance(accepts, list):
        for a in accepts:
            rec = _accept_record(a, parent_resource)
            if rec:
                out.append(rec)
    for key in _NESTED_KEYS:
        v = doc.get(key)
        if isinstance(v, list):
            out.extend(extract_resources(v, _depth + 1))
    return out


def payees(resources):
    """Deduped, validated payTo addresses (feed to chain_backfill)."""
    seen, out = set(), []
    for r in resources or []:
        p = r.get("payTo")
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _human(atomic, decimals=6):
    try:
        d = Decimal(int(atomic)) / (Decimal(10) ** decimals)
        return format(d, "f") if d > 0 else None
    except Exception:
        return None


def price_observations(resources):
    """Advertised list prices as cross-counterparty price observations (feed a
    peer-group baseline). USDC (6dp) assumed for the human amount."""
    out = []
    for r in resources or []:
        amt = _human(r.get("price_atomic"))
        if amt:
            out.append({"counterparty": r["payTo"], "resource": r.get("resource"),
                        "amount": amt, "asset": r.get("asset"),
                        "network": r.get("network")})
    return out


def crawl(sources, *, fetch=None):
    """Fetch each discovery source and aggregate its resource records. A source
    that errors or returns junk is skipped, not fatal."""
    getj = fetch or _urllib_get_json
    resources = []
    for s in sources or []:
        try:
            doc = getj(s)
        except Exception:
            continue
        resources.extend(extract_resources(doc))
    return resources


def crawl_and_backfill(store, sources, *, fetch=None, chain_fetch=None, max_pages=5):
    """Crawl discovery -> extract payees -> seed reputation from public Base data.
    Returns {resources, payees, fetched, ingested}."""
    import chain_backfill
    resources = crawl(sources, fetch=fetch)
    ps = payees(resources)
    cf = chain_fetch or chain_backfill.BlockscoutPager().fetch
    summary = chain_backfill.backfill(store, ps, cf, max_pages=max_pages)
    return {"resources": len(resources), "payees": len(ps),
            "fetched": summary["fetched"], "ingested": summary["ingested"]}


def _urllib_get_json(url, timeout=12):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Blackwall-discovery/0.1",
                      "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _read_sources(args):
    src = list(args.source or [])
    if args.sources_file:
        with open(args.sources_file, "r", encoding="utf-8") as f:
            src += [ln.strip() for ln in f
                    if ln.strip() and not ln.lstrip().startswith("#")]
    return src


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Crawl x402 discovery, map endpoints, and (optionally) seed reputation.")
    p.add_argument("--source", action="append", help="a discovery URL (repeatable)")
    p.add_argument("--sources-file", help="file with one discovery URL per line")
    p.add_argument("--out-payees", help="write the deduped payee list here (one/line)")
    p.add_argument("--backfill-store", help="if set, seed this ReputationStore from the payees")
    p.add_argument("--max-pages", type=int, default=5)
    args = p.parse_args(argv)

    sources = _read_sources(args)
    if not sources:
        sys.stderr.write("discovery_crawl: no sources (--source or --sources-file)\n")
        return 2
    resources = crawl(sources)
    ps = payees(resources)
    sys.stdout.write("Found %d resource(s) across %d source(s): %d distinct payee(s).\n"
                     % (len(resources), len(sources), len(ps)))
    if args.out_payees:
        with open(args.out_payees, "w", encoding="utf-8") as f:
            f.write("\n".join(ps) + ("\n" if ps else ""))
        sys.stdout.write("wrote payees -> %s\n" % args.out_payees)
    if args.backfill_store:
        from reputation_store import ReputationStore
        summary = crawl_and_backfill(ReputationStore(args.backfill_store), sources,
                                     max_pages=args.max_pages)
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
