#!/usr/bin/env python3
"""
category_pricing.py -- per-CATEGORY price baselines from ON-CHAIN settled amounts.

Blackwall's price-anomaly gate is per-payee (a quote vs a counterparty's OWN median).
This adds a per-CATEGORY market rate: the median (across DISTINCT payees) of what
payees in a service category actually COLLECTED on-chain. It catches a COLD-START payee
(no own history, so the per-payee gate is blind) whose quote is an order-of-magnitude
outlier vs its category cohort -- e.g. a finance API quoting $5 when the finance
category's on-chain median is ~$0.005.

ON-CHAIN, not advertised: the baseline is built from settled amounts (the reputation
store's `price_history`, seeded by chain_backfill), which a seller CANNOT inflate by
editing a Bazaar listing -- unlike the advertised `price_atomic`.

ADVISORY: the category is derived from the seller-controlled resource URL and is fuzzy
(~1/3 'other'); the baseline only ever escalates to HOLD (never STOP), fail-open. The
'other' bucket is never indexed (it isn't a coherent cohort). See docs/CATEGORY.md.

Pure builders here; the verdict-side gate lives in blackwall.decide_payment
(`category_median` -> CATEGORY_HOLD_RATIO). A `--out` CLI writes the index as JSON so a
deploy can precompute it and load it via BLACKWALL_CATEGORY_INDEX.
"""
from __future__ import annotations

import json

from categories import CATEGORY_UNCLASSIFIED, classify_category


def load_category_index(path):
    """Load a precomputed {category: median} index JSON from `path` (the shared
    loader for both the HTTP server and the MCP server -- single source of truth so
    they can't drift). Returns (index|None, error|None):
      * a falsy path        -> (None, None)   -- signal simply off, nothing to warn
      * a non-empty path we couldn't use -> (None, "<why>")  -- caller may warn
      * success             -> ({str: str}, None)
    FAIL-OPEN: never raises. Values are normalized to strings (Decimal-parseable
    downstream)."""
    if not path:
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)
    if not isinstance(loaded, dict) or not loaded:
        return None, "empty or not a JSON object"
    return {str(k): str(v) for k, v in loaded.items()}, None


# The loader is index-agnostic ({str: str} JSON). Alias it for the price-divergence
# index (price_integrity.py) so callers read clearly and can't drift the parse logic.
load_index_json = load_category_index

# distinct payees needed to define a category market rate. Broader/fuzzier than a
# resource class, so require more peers than MIN_PEER_COUNTERPARTIES(3).
MIN_CATEGORY_PAYEES = 5


def build_category_index(observations, *, min_payees=MIN_CATEGORY_PAYEES):
    """{category: median_price_str} from [{category, payee, amount}] observations.
    Median-of-medians across DISTINCT payees (a high-volume/wash payee can't drag the
    rate); a category with < min_payees distinct payees is omitted (too thin to be a
    market). 'other' is never indexed. PURE."""
    # Reuse the median-of-medians machinery (lazy import avoids any import cycle: this
    # module is never imported by blackwall's core verdict path).
    from blackwall import build_peer_class_index
    obs = [{"resource_class": o.get("category"), "counterparty": o.get("payee"),
            "amount": o.get("amount")}
           for o in (observations or [])
           if isinstance(o, dict)
           and o.get("category") not in (None, CATEGORY_UNCLASSIFIED)]
    return build_peer_class_index(obs, min_counterparties=min_payees)


def payee_categories_from_resources(resources):
    """{payee_lower: category} by classifying each payee's set of resource URLs
    (dominant category across them). Skips payees with no classifiable resource."""
    by = {}
    for r in resources or []:
        pt = (r.get("payTo") or "").lower()
        url = r.get("resource")
        if pt and url:
            by.setdefault(pt, []).append(url)
    return {pt: classify_category(urls) for pt, urls in by.items()}


def observations_from_store(store, payee_category):
    """Collect ON-CHAIN settled amounts per payee from a ReputationStore, tagged with
    each payee's category. `payee_category`: {payee_lower: category_slug}. Fail-soft:
    a payee the store can't look up is skipped. Returns [{category, payee, amount}]."""
    obs = []
    for payee, cat in (payee_category or {}).items():
        if cat in (None, CATEGORY_UNCLASSIFIED):
            continue
        try:
            rec = store.lookup(payee) or {}
        except Exception:
            continue
        for amt in rec.get("price_history") or []:
            obs.append({"category": cat, "payee": payee, "amount": amt})
    return obs


def build_index(store, resources, *, min_payees=MIN_CATEGORY_PAYEES):
    """End-to-end: classify payees from `resources`, pull their on-chain settled
    amounts from `store`, and build the {category: median} index. PURE given inputs."""
    pc = payee_categories_from_resources(resources)
    return build_category_index(observations_from_store(store, pc),
                                min_payees=min_payees)


def main(argv=None):
    import argparse
    import json
    import sys
    p = argparse.ArgumentParser(
        description="Build the per-category on-chain price baseline index (JSON).")
    p.add_argument("--store", required=True, help="ReputationStore SQLite path (on-chain history)")
    p.add_argument("--max-pages", type=int, default=8, help="CDP Bazaar pages to crawl for payee->category")
    p.add_argument("--min-payees", type=int, default=MIN_CATEGORY_PAYEES)
    p.add_argument("--out", help="write the index JSON here (default stdout)")
    args = p.parse_args(argv)

    import discovery_crawl
    from reputation_store import ReputationStore
    resources = discovery_crawl.crawl_all(max_pages=args.max_pages)
    index = build_index(ReputationStore(args.store), resources, min_payees=args.min_payees)
    out = json.dumps(index, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w") as f:
            f.write(out + "\n")
        sys.stderr.write("wrote %d category baselines to %s\n" % (len(index), args.out))
    else:
        sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
