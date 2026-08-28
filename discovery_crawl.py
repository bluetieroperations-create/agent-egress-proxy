#!/usr/bin/env python3
"""
discovery_crawl.py -- map the x402 seller ecosystem, and auto-feed the moat.

`chain_backfill.py` seeds reputation from public data but needs a LIST of payee
addresses. This crawler produces that list automatically. Out of the box it walks
the live **CDP x402 Bazaar** (`CDP_BAZAAR`, Coinbase's public discovery layer,
~15k resources, offset-paginated) -- and also parses any extra source you pass (a
facilitator resource list, a directory, or an endpoint's own 402 / `/.well-known/
x402` descriptor). It reads the standard x402 `accepts` shape and extracts each
resource's `payTo` + advertised price + asset/network.

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
from decimal import Decimal

import http_util
from addresses import is_evm_address

_MAX_DEPTH = 6
_NESTED_KEYS = ("items", "resources", "data", "results", "endpoints")

# The live x402 discovery layer: Coinbase's CDP Bazaar. Public GET, offset-paginated
# (`{items:[{accepts:[...]}], pagination:{limit,offset,total}}`), ~15k resources.
CDP_BAZAAR = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"


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
    if isinstance(resource, dict):          # a v2 ResourceInfo {url:...} -> the url
        resource = resource.get("url")
    if not isinstance(resource, str):
        resource = None
    return {"resource": resource, "payTo": payto.lower(),
            "asset": accept.get("asset"), "network": accept.get("network"),
            "price_atomic": _price_atomic(accept)}


def _www_authenticate(headers):
    """The WWW-Authenticate value from a header mapping, case-insensitively."""
    for k, v in (headers or {}).items():
        if str(k).lower() == "www-authenticate":
            return str(v)
    return None


def extract_resources(doc, _depth=0, _headers=None):
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
    # v2 header-style challenge: the requirements live in `WWW-Authenticate`, not
    # the body, so a body-only crawl records these payees as if they did not exist.
    # Additive -- appended after body entries, never replacing them.
    if _headers and _depth == 0:
        try:
            import x402_challenge
            entry = x402_challenge.to_accepts(_www_authenticate(_headers))
        except Exception:
            entry = None
        if entry:
            rec = _accept_record(entry, parent_resource)
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


def crawl_bazaar(*, base_url=CDP_BAZAAR, fetch=None, page_limit=100, max_pages=20):
    """Paginate the CDP x402 Bazaar (offset-based) and aggregate resource records.
    Stops at the reported `total`, at `max_pages`, or on the first bad/empty page.
    `max_pages`*`page_limit` bounds a full crawl (~15k resources -> ~150 pages)."""
    getj = fetch or _urllib_get_json
    resources, offset, pages = [], 0, 0
    while pages < max_pages:
        try:
            doc = getj("%s?limit=%d&offset=%d" % (base_url, page_limit, offset))
        except Exception as e:
            # A mid-crawl error truncates the snapshot. Say so on stderr rather than
            # silently returning a partial map presented as complete.
            sys.stderr.write("crawl_bazaar: stopped at offset %d after %d page(s): %s\n"
                             % (offset, pages, type(e).__name__))
            break
        if not (isinstance(doc, dict) and doc.get("items")):
            break
        resources.extend(extract_resources(doc))
        pag = doc.get("pagination") or {}
        lim = pag.get("limit") or page_limit
        total = pag.get("total")
        offset += lim
        pages += 1
        if total is not None and offset >= total:
            break
    return resources


def crawl_all(*, extra_sources=None, bazaar=True, fetch=None, max_pages=20):
    """Everything: the CDP Bazaar (default, out of the box) + any extra single-doc
    discovery sources. Returns the aggregated resource records."""
    resources = []
    if bazaar:
        resources.extend(crawl_bazaar(fetch=fetch, max_pages=max_pages))
    if extra_sources:
        resources.extend(crawl(extra_sources, fetch=fetch))
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
    # retry/backoff on transient 429/5xx/timeout + size cap (see http_util); a
    # rate-limited Bazaar page is retried before crawl_bazaar treats it as the end.
    return http_util.get_json(url, timeout=timeout, user_agent="Blackwall-discovery/0.1")


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
    p.add_argument("--source", action="append",
                   help="an extra discovery URL, in addition to the Bazaar (repeatable)")
    p.add_argument("--sources-file", help="file with one extra discovery URL per line")
    p.add_argument("--no-bazaar", action="store_true",
                   help="skip the default CDP Bazaar crawl (use only --source/--sources-file)")
    p.add_argument("--max-pages", type=int, default=20,
                   help="CDP Bazaar pages to walk (100 resources/page; default 20)")
    p.add_argument("--out-payees", help="write the deduped payee list here (one/line)")
    p.add_argument("--backfill-store", help="if set, seed this ReputationStore from the payees")
    p.add_argument("--backfill-max-pages", type=int, default=5,
                   help="on-chain pages per payee for the backfill (default 5)")
    args = p.parse_args(argv)

    extra = _read_sources(args)
    # Out of the box: crawl the live CDP Bazaar. Add your own sources with --source,
    # or skip the Bazaar with --no-bazaar.
    resources = crawl_all(extra_sources=extra, bazaar=not args.no_bazaar,
                          max_pages=args.max_pages)
    ps = payees(resources)
    sys.stdout.write("Found %d resource(s): %d distinct payee(s)%s.\n"
                     % (len(resources), len(ps),
                        "" if args.no_bazaar else " (incl. CDP Bazaar)"))
    if args.out_payees:
        with open(args.out_payees, "w", encoding="utf-8") as f:
            f.write("\n".join(ps) + ("\n" if ps else ""))
        sys.stdout.write("wrote payees -> %s\n" % args.out_payees)
    if args.backfill_store:
        from reputation_store import ReputationStore
        import chain_backfill
        store = ReputationStore(args.backfill_store)
        bf = chain_backfill.backfill(store, ps, chain_backfill.BlockscoutPager().fetch,
                                     max_pages=args.backfill_max_pages)
        sys.stdout.write(json.dumps(
            {"resources": len(resources), "payees": len(ps),
             "fetched": bf["fetched"], "ingested": bf["ingested"]}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
