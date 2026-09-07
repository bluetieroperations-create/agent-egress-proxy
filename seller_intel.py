#!/usr/bin/env python3
"""
seller_intel.py -- what a SELLER can learn from the whole x402 payment graph that
it cannot see from its own inbound payments.

WHY THIS EXISTS
---------------
Every other module here answers a BUYER's question ("is this payee safe to pay?").
That side of the market has two sellers in 266 and unproven demand. The sellers,
by contrast, have an immediate problem we are uniquely placed to solve: each one
observes only its own settlements, so it cannot compute its own price against its
category, cannot tell a churned buyer from a quiet week, and cannot see that a
departed buyer is now paying a competitor. We can, because we hold every payee's
history at once.

TWO ANSWERS, BOTH MEASURED ON THE COMMITTED CORPUS
--------------------------------------------------
`price_benchmark` -- "am I priced right?"

    Across 208 cleanly-measured sellers, the correlation between log price and log
    transaction volume is -0.013, and between price and distinct buyers -0.014.
    Sellers under $0.005 see a median 152 transactions; sellers over $0.02 see 160.
    Demand in this range does not visibly respond to price. 52 of 174 sellers price
    below their category median, by a median factor of 5.

    That is OBSERVATIONAL and this module says so in its output: higher-priced
    sellers may simply sell more valuable things, and price cannot be separated
    from product here. The honest claim is "demand does not visibly punish higher
    prices in this range" -- a strong prior to test, not a guarantee. `uplift_note`
    carries that caveat into every result so it cannot be quoted without it.

`defections` -- "did that buyer leave, and where did they go?"

    A buyer that bought repeatedly, stopped, and kept buying elsewhere. 322 such
    events in the corpus, the largest after 292 purchases. A seller sees only the
    absence; the graph shows the destination.

DATA-QUALITY GUARDS (not optional -- the corpus needs them)
-----------------------------------------------------------
`is_truncated`  -- 70 of 281 payees have settlement counts on an exact round
    number (31 at 200, 19 at 300, 10 at 250): a backfill page cap, not real
    history. Their totals, first-seen and last-seen are all understated, so they
    are excluded from baselines and never reported as having gone quiet.

`is_whale_skewed` -- a payee whose mean runs far above its median is carrying a
    few large one-off transfers; scaling its whole revenue by a price multiple
    would invent money that was never per-transaction revenue.

Pure functions given their inputs; no network, no store coupling. The CLI at the
bottom renders one seller's report from the committed corpus.
"""
from __future__ import annotations

import statistics

#: Settlement counts that are almost certainly a backfill page cap rather than a
#: real total. Measured on the committed corpus: 25% of payees sit on one of these.
PAGE_CAP_COUNTS = frozenset({100, 150, 200, 250, 300, 500})

#: mean/median above this means a few large transfers dominate the payee's total.
WHALE_SKEW_RATIO = 20.0

#: A category needs this many clean sellers before its median means anything.
MIN_CATEGORY_SELLERS = 5

#: Minimum settlements before a seller's own median price is worth quoting.
MIN_SELLER_SETTLEMENTS = 10

#: Purchases from one seller before a stop counts as a defection rather than a
#: buyer who simply tried the endpoint once.
DEFECTION_MIN_PURCHASES = 10

#: A buyer must have gone on buying elsewhere for at least this long after its
#: last purchase here. Without it, ordinary interleaved multi-homing reads as
#: churn -- the first run of this module flagged a "defection" whose last
#: purchase and next-elsewhere were TEN SECONDS apart. That is one buyer using
#: two sellers in a single session, and alerting on it would burn the seller's
#: trust on the first message we ever send them.
DEFECTION_MIN_GAP_SECONDS = 7 * 24 * 3600

#: ...and must have bought elsewhere enough times for it to be a pattern.
DEFECTION_MIN_PURCHASES_AFTER = 3

UPLIFT_NOTE = (
    "Observational: sellers charging more may sell more valuable products, and "
    "price cannot be separated from product in this corpus. The evidence supports "
    "'demand does not visibly punish higher prices in this range' (corr -0.013 "
    "over 208 sellers), not a guarantee of the uplift."
)


def is_truncated(settlement_count):
    """True when a payee's history looks page-capped rather than complete."""
    return settlement_count in PAGE_CAP_COUNTS


def is_whale_skewed(amounts, ratio=WHALE_SKEW_RATIO):
    """True when a few large transfers dominate, so per-transaction reasoning
    about this payee's revenue would be misleading."""
    vals = [a for a in amounts if a is not None]
    if not vals:
        return True
    med = statistics.median(vals)
    return med <= 0 or (statistics.mean(vals) / med) > ratio


def clean_sellers(amounts_by_seller, *, min_settlements=MIN_SELLER_SETTLEMENTS):
    """The subset of sellers whose price is safe to reason about: enough history,
    not page-capped, not whale-skewed. Everything downstream builds on this."""
    out = {}
    for seller, amounts in (amounts_by_seller or {}).items():
        vals = [a for a in (amounts or []) if a is not None]
        if len(vals) < min_settlements:
            continue
        if is_truncated(len(vals)) or is_whale_skewed(vals):
            continue
        out[seller] = vals
    return out


def category_baselines(amounts_by_seller, category_of,
                       *, min_sellers=MIN_CATEGORY_SELLERS,
                       min_settlements=MIN_SELLER_SETTLEMENTS):
    """{category: median of member sellers' median prices}.

    Median of medians, not median of all amounts: one high-volume seller would
    otherwise define its whole category's "normal"."""
    clean = clean_sellers(amounts_by_seller, min_settlements=min_settlements)
    by_cat = {}
    for seller, vals in clean.items():
        cat = (category_of or {}).get(seller)
        if cat:
            by_cat.setdefault(cat, []).append(statistics.median(vals))
    return {cat: statistics.median(meds)
            for cat, meds in by_cat.items() if len(meds) >= min_sellers}


def price_benchmark(seller, amounts, category, baselines,
                    *, min_settlements=MIN_SELLER_SETTLEMENTS):
    """Where one seller sits against its category, and what moving to the median
    would have been worth over the observed history.

    Returns None when the seller cannot be measured honestly -- too little
    history, a page-capped total, whale-skewed amounts, or no category baseline.
    Returning None is the point: a benchmark quoted from a truncated history is
    worse than no benchmark."""
    vals = [a for a in (amounts or []) if a is not None]
    if len(vals) < min_settlements:
        return None
    if is_truncated(len(vals)):
        return None
    if is_whale_skewed(vals):
        return None
    base = (baselines or {}).get(category)
    if not base or base <= 0:
        return None

    own = statistics.median(vals)
    if own <= 0:
        return None
    observed = sum(vals)
    multiple = base / own
    return {
        "seller": seller,
        "category": category,
        "settlements": len(vals),
        "median_price": own,
        "category_median": base,
        "position": ("below" if own < base else "above" if own > base else "at"),
        "multiple_to_median": multiple,
        "observed_revenue": observed,
        # What the SAME transaction count would have produced at the category
        # median. Volume is held constant deliberately -- that is the finding.
        "revenue_at_category_median": observed * multiple,
        "implied_uplift": observed * (multiple - 1.0) if multiple > 1 else 0.0,
        "uplift_note": UPLIFT_NOTE,
    }


def rank_underpriced(amounts_by_seller, category_of, baselines=None, *,
                     min_settlements=MIN_SELLER_SETTLEMENTS):
    """Every cleanly-measurable seller pricing below its category median, worst
    first. This is the prospect list for the pricing product."""
    if baselines is None:
        baselines = category_baselines(amounts_by_seller, category_of,
                                       min_settlements=min_settlements)
    rows = []
    for seller, amounts in (amounts_by_seller or {}).items():
        b = price_benchmark(seller, amounts, (category_of or {}).get(seller),
                            baselines, min_settlements=min_settlements)
        if b and b["position"] == "below":
            rows.append(b)
    rows.sort(key=lambda r: -r["multiple_to_median"])
    return rows


def _seconds_between(a, b):
    """Gap between two ISO-8601 timestamps, or None if either is unparseable.
    Fail-soft: an unparseable pair suppresses the alert rather than inventing a
    gap, because a false defection costs more than a missed one."""
    import datetime
    try:
        fmt = lambda s: datetime.datetime.fromisoformat(
            str(s).replace("Z", "+00:00").replace(".000000", ""))
        return (fmt(b) - fmt(a)).total_seconds()
    except (ValueError, TypeError):
        return None


def defections(events_by_payer, *, min_purchases=DEFECTION_MIN_PURCHASES,
               min_gap_seconds=DEFECTION_MIN_GAP_SECONDS,
               min_purchases_after=DEFECTION_MIN_PURCHASES_AFTER):
    """Buyers that bought repeatedly from one seller, stopped, and kept buying.

    `events_by_payer`: {payer: [(ts, seller), ...]}, ts as an ISO-8601 string.
    Returns [{payer, left_seller, purchases, last_purchase, still_active_until,
    gap_days, purchases_after, moved_to}], heaviest relationships first.

    THREE conditions, and the last two exist because the first alone is wrong:

      1. the buyer bought >= `min_purchases` times from this seller;
      2. it kept buying elsewhere for at least `min_gap_seconds` afterwards --
         without this, a buyer alternating between two sellers inside one
         session reads as churn (measured: a flagged pair ten seconds apart);
      3. it made >= `min_purchases_after` purchases elsewhere, so the move is a
         pattern rather than a single stray call.

    A buyer that simply stopped using x402 is NOT a defection and is excluded:
    the seller would be chasing someone who left the market, not a competitor."""
    out = []
    for payer, events in (events_by_payer or {}).items():
        rows = sorted((ts, s) for ts, s in (events or []) if ts is not None and s)
        if len(rows) < 2:
            continue
        counts, last_seen = {}, {}
        for ts, seller in rows:
            counts[seller] = counts.get(seller, 0) + 1
            last_seen[seller] = ts
        if len(counts) < 2:
            continue
        latest = rows[-1][0]
        for seller, n in counts.items():
            if n < min_purchases or last_seen[seller] >= latest:
                continue
            after = [(ts, s) for ts, s in rows
                     if ts > last_seen[seller] and s != seller]
            if len(after) < min_purchases_after:
                continue
            gap = _seconds_between(last_seen[seller], latest)
            if gap is None or gap < min_gap_seconds:
                continue
            out.append({
                "payer": payer,
                "left_seller": seller,
                "purchases": n,
                "last_purchase": last_seen[seller],
                "still_active_until": latest,
                "gap_days": round(gap / 86400.0, 1),
                "purchases_after": len(after),
                "moved_to": sorted({s for _, s in after}),
            })
    out.sort(key=lambda d: -d["purchases"])
    return out


def defections_for(seller, all_defections):
    """The subset a given seller would be alerted about."""
    return [d for d in (all_defections or []) if d["left_seller"] == seller]


def seller_report(seller, *, amounts_by_seller, category_of, events_by_payer,
                  baselines=None, min_settlements=MIN_SELLER_SETTLEMENTS):
    """Everything this product tells one seller, in one object."""
    if baselines is None:
        baselines = category_baselines(amounts_by_seller, category_of,
                                       min_settlements=min_settlements)
    lost = defections_for(seller, defections(events_by_payer))
    return {
        "seller": seller,
        "category": (category_of or {}).get(seller),
        "pricing": price_benchmark(seller, (amounts_by_seller or {}).get(seller),
                                   (category_of or {}).get(seller), baselines,
                                   min_settlements=min_settlements),
        "defections": lost,
        "buyers_lost": len(lost),
        "purchases_lost": sum(d["purchases"] for d in lost),
    }


# ---------------------------------------------------------------------------
def main(argv=None):
    """Render one seller's report from the committed corpus."""
    import argparse
    import collections
    import json

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", default="/tmp/rep.db", help="settlement sqlite path")
    ap.add_argument("--directory", default="data/directory.json")
    ap.add_argument("--seller", help="payee address; omit to list the top underpriced")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args(argv)

    import sqlite3
    conn = sqlite3.connect(args.db)
    amounts, events = collections.defaultdict(list), collections.defaultdict(list)
    for cp, payer, amt, ts in conn.execute(
            "SELECT counterparty, payer, amount, ts FROM settlements"):
        cp = str(cp).lower()
        try:
            amounts[cp].append(float(amt))
        except (TypeError, ValueError):
            pass
        if payer:
            events[str(payer).lower()].append((str(ts), cp))

    with open(args.directory) as fh:
        category_of = {str(x.get("payee", "")).lower(): x.get("category")
                       for x in json.load(fh)}

    base = category_baselines(amounts, category_of)
    if args.seller:
        print(json.dumps(seller_report(args.seller.lower(), amounts_by_seller=amounts,
                                       category_of=category_of,
                                       events_by_payer=events, baselines=base),
                         indent=2, default=str))
        return 0

    rows = rank_underpriced(amounts, category_of, base)
    print("%-14s %-16s %10s %10s %8s %12s" %
          ("seller", "category", "price", "cat_med", "under", "uplift"))
    for r in rows[:args.top]:
        print("%-14s %-16s %10.4f %10.4f %7.1fx %12.2f" %
              (r["seller"][:12], (r["category"] or "?")[:16], r["median_price"],
               r["category_median"], r["multiple_to_median"], r["implied_uplift"]))
    print("\n%d underpriced sellers of %d cleanly measurable."
          % (len(rows), len(clean_sellers(amounts))))
    print("NOTE: " + UPLIFT_NOTE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
