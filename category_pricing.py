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


def sidecar_meta(index_text, counts, min_payees, generated_at):
    """The `category_index.meta.json` payload for an index whose exact bytes are
    `index_text`. PURE, and separated from main() precisely so the CONTENT PIN can be
    tested without a live crawl -- an untested pin is the pin that silently stops
    pinning.

    Hashes the bytes handed in rather than re-reading the path, so the digest cannot
    describe a file written after it (see payto_baseline._sidecar_age: the sidecar's own
    failure mode is the forgotten refresh).
    """
    import hashlib
    raw = index_text.encode("utf-8") if isinstance(index_text, str) else index_text
    return {"generated_at": generated_at,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "min_payees": min_payees,
            "indexed": sorted(json.loads(raw.decode("utf-8"))),
            "payees": dict(counts)}


def category_payee_counts(observations):
    """{category: distinct payee count} over the SAME observations the index is built
    from. PURE.

    This is the number a reader of `category_index.json` cannot recover and most needs.
    A baseline is a median-of-medians across distinct payees, so one computed over 7
    payees moves when a single payee enters or leaves while one over 29 does not -- and
    the finished artifact renders both as one price string. Measured on the 2026-09-28
    corpus: `dev-tools` rested on 7 payees and moved 2.25x; `ai-agents` rested on 26 and
    did not move at all. Counts are emitted for EVERY category present in the
    observations, including those under MIN_CATEGORY_PAYEES that the index omits, since
    "4 payees, just under the floor" is exactly what explains a category's absence.
    """
    by = {}
    for o in observations or []:
        if not isinstance(o, dict):
            continue
        cat, payee = o.get("category"), o.get("payee")
        if cat in (None, CATEGORY_UNCLASSIFIED) or not payee:
            continue
        by.setdefault(cat, set()).add(payee)
    return {cat: len(payees) for cat, payees in sorted(by.items())}


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

    import datetime
    import discovery_crawl
    from reputation_store import ReputationStore
    resources = discovery_crawl.crawl_all(max_pages=args.max_pages)
    # build_index's two steps, inlined, so the observations can be counted as well as
    # priced without crawling or querying the store twice.
    pc = payee_categories_from_resources(resources)
    observations = observations_from_store(ReputationStore(args.store), pc)
    index = build_category_index(observations, min_payees=args.min_payees)
    counts = category_payee_counts(observations)
    out = json.dumps(index, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w") as f:
            f.write(out + "\n")
        sys.stderr.write("wrote %d category baselines to %s\n" % (len(index), args.out))
        # SIDECAR, not extra keys in the index. `load_category_index` coerces the index
        # with {str(k): str(v)} and five modules parse those values as Decimals, so a
        # nested count would arrive downstream as the string "{'payees': 7}". Same shape
        # as data/directory.meta.json, and for the same reason: a bare artifact cannot
        # carry its own provenance without breaking the readers that already parse it.
        # REUSE, not a second copy: payto_baseline.meta_path already derives this for
        # data/directory.json, and two sidecar-naming rules that could drift is exactly
        # how a sidecar ends up describing a file it does not sit beside.
        from payto_baseline import meta_path as _sidecar_path
        meta_path = _sidecar_path(args.out)
        # CONTENT-PINNED over the EXACT bytes written above, by the same argument
        # payto_baseline._sidecar_age makes for data/directory.meta.json: "a sidecar's own
        # failure mode is the forgotten refresh -- regenerate the corpus, leave the
        # sidecar, and the date now describes a file that no longer exists." A key list is
        # not enough to pin it, which is this change's whole point: a refresh can keep all
        # six category names and move every price.
        meta = sidecar_meta(out + "\n", counts, args.min_payees,
                            datetime.datetime.now(datetime.timezone.utc)
                                    .strftime("%Y-%m-%dT%H:%M:%SZ"))
        with open(meta_path, "w") as f:
            f.write(json.dumps(meta, indent=2, sort_keys=True) + "\n")
        sys.stderr.write("wrote payee counts for %d category/categories to %s\n"
                         % (len(counts), meta_path))
    else:
        sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
