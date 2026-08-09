#!/usr/bin/env python3
"""
price_integrity.py -- advertised-vs-settled price divergence (bait-and-switch).

A seller can list a cheap price on the CDP Bazaar to rank well / attract agents in
discovery, then actually COLLECT far more on-chain. Blackwall already checks the live
quote (own-price + category baselines); this adds a PAYEE-level trust signal: does this
payee's on-chain SETTLED median run far above its most EXPENSIVE advertised price? If
so, its public listing understates the real cost -- a bait-and-switch pattern.

Metric: ratio = settled_median / max_advertised (per payee). An eval on the live corpus
(161 payees with both) found 95% settle at <= 2.5x their max-advertised price; the tail
(>= 10x) is sellers listing ~$0.001 and collecting $0.05-$0.09. Gate (HOLD) at 10x -- the
5-6x band is ambiguous with the temporal confound below, 10x+ is unambiguous.

ADVISORY posture: HOLD only, never STOP; fail-open. Known confound -- the advertised
price is a CURRENT snapshot while the settled amounts are a HISTORICAL window, so a
seller that RAISED its price after listing would diverge benignly; the 5x bar and
HOLD-only (reviewable) keep that a conservative escalation, not a block. Also can't
attribute a settled amount to a specific advertised endpoint (the store's price_history
carries no resource tag), so this is a coarse payee-level signal. See docs/CATEGORY.md.

Pure builders here; the verdict-side gate is blackwall.decide_payment
(`divergence_ratio` -> DIVERGENCE_HOLD_RATIO). A --out CLI writes the index as JSON to
load via BLACKWALL_DIVERGENCE_INDEX.
"""
from __future__ import annotations

import statistics

# Only payees whose settled median runs >= this multiple of their MAX advertised price
# are worth indexing (the watch-list tail); the verdict gate applies the HOLD ratio.
DIVERGENCE_WATCH_RATIO = 2.0
# The verdict gate (blackwall.DIVERGENCE_HOLD_RATIO) is the source of truth; kept here
# in step for docs/CLI. The WATCH ratio is the index floor (keep the tail small).
DIVERGENCE_HOLD_RATIO = 10.0    # settle >= 10x the most-expensive listing -> HOLD


def divergence_ratio(settled_amounts, advertised_prices):
    """settled_median / max_advertised for a payee, or None when either side is
    missing/non-positive. PURE. Uses the MAX advertised price as the base so a payee
    with a genuinely expensive (listed) endpoint that people pay is NOT flagged -- only
    a payee collecting more than ANYTHING it advertises."""
    settled = [float(a) for a in (settled_amounts or []) if _pos(a)]
    adv = [float(a) for a in (advertised_prices or []) if _pos(a)]
    if not settled or not adv:
        return None
    amax = max(adv)
    if amax <= 0:
        return None
    return statistics.median(settled) / amax


def _pos(x):
    try:
        return float(x) > 0
    except (TypeError, ValueError):
        return False


def build_divergence_index(resources, store, *, watch_ratio=DIVERGENCE_WATCH_RATIO):
    """{payee: "ratio"} for payees whose settled median is >= watch_ratio x their max
    advertised price. `resources` = CDP Bazaar records ({payTo, price_atomic, asset}),
    `store` = a ReputationStore (settled amounts via price_history). Only USDC-6dp
    advertised prices count (others have unknown decimals). PURE given inputs."""
    from ecosystem_scan import _usdc_price_atomic  # USDC-6dp price extraction
    adv = {}
    for r in resources or []:
        pt = (r.get("payTo") or "").lower()
        pa = _usdc_price_atomic(r)   # atomic USDC-6dp or None
        if pt and pa:
            adv.setdefault(pt, []).append(pa / 1_000_000.0)
    index = {}
    for payee, aprices in adv.items():
        try:
            rec = store.lookup(payee) or {}
        except Exception:
            continue
        ratio = divergence_ratio(rec.get("price_history"), aprices)
        if ratio is not None and ratio >= watch_ratio:
            index[payee] = "%.4f" % ratio
    return index


def main(argv=None):
    import argparse
    import json
    import sys
    p = argparse.ArgumentParser(
        description="Build the advertised-vs-settled price-divergence index (JSON).")
    p.add_argument("--store", required=True, help="ReputationStore SQLite path")
    p.add_argument("--max-pages", type=int, default=8, help="CDP Bazaar pages to crawl")
    p.add_argument("--watch-ratio", type=float, default=DIVERGENCE_WATCH_RATIO)
    p.add_argument("--out", help="write the index JSON here (default stdout)")
    args = p.parse_args(argv)

    import discovery_crawl
    from reputation_store import ReputationStore
    resources = discovery_crawl.crawl_all(max_pages=args.max_pages)
    index = build_divergence_index(resources, ReputationStore(args.store),
                                   watch_ratio=args.watch_ratio)
    out = json.dumps(index, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w") as f:
            f.write(out + "\n")
        sys.stderr.write("wrote %d price-divergence entries to %s\n" % (len(index), args.out))
    else:
        sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
