#!/usr/bin/env python3
"""
index_guard.py -- the release gate for the two SEED INDEXES a refresh rebuilds:
`data/category_index.json` (per-category market rate) and
`data/divergence_index.json` (advertised-vs-settled ratio per payee).

THE THIRD GUARD, and the one whose absence was found by auditing a refresh rather than
by reading code. `refresh_guard.py` protects the reputation store; `directory_guard.py`
protects the x402 directory; NOTHING protected these two. refresh_seed.sh said so in as
many words and the gap stayed open anyway:

    "INDEX DEPTH. The guard below validates the STORE only -- payees, edges, age --
     so a shallow index build passes it while quietly shrinking coverage."

WHY THAT MATTERS, precisely. A missing category baseline is FAIL-OPEN, not fail-closed:
`blackwall.py` reads `category_index.get(category) ... else None`, so an absent category
means the category-price check silently does nothing for every payment in it. No wrong
verdict, no error, no log line -- the check is simply not there. Same shape as
`payee_syntax`'s "0 malformed" meaning 0 SEEN, and the same shape as the un-reachable
payTo gate that cost this repo a week. A gate that quietly stops gating is the failure
mode this codebase keeps rediscovering.

WHAT THIS GUARD CAN AND CANNOT SEE, which is the whole design problem. A smaller index
has two completely different causes and they look identical from the artifact:

  1. LEGITIMATE THINNING. `build_category_index` omits any category with fewer than
     MIN_CATEGORY_PAYEES(5) distinct payees, because a median over four payees is not a
     market rate. When a category genuinely loses payees, dropping it is CORRECT and
     blocking the refresh over it would be wrong.
  2. A SHALLOW OR THROTTLED INDEX CRAWL. The same code over a truncated crawl emits
     fewer baselines for no reason but the crawl.

MEASURED, both of them, which is where the thresholds below come from:

  - 2026-09-24, on the store from that week's refresh: the index went 7 -> 6 categories,
    losing `commerce`. Rebuilt at DOUBLE depth (48 pages instead of 24): still 6,
    `commerce` still absent. Rebuilt at `--min-payees 1`: NINE categories, with
    `commerce`, `email-comms` and `storage-files` the three that exist on-chain but sit
    under the threshold. So that shrink was case 1 -- correct behaviour -- and proving it
    took two extra crawls precisely because no guard recorded the distinction.
  - refresh_seed.sh's own note, measured 2026-08-28 on the store of the day: 8 pages
    produced FOUR baselines, 24 produced SEVEN. Four was case 2.

So magnitude is the signal available: 6 of 7 is churn, 4 of 7 is a crawl that failed.
MIN_CATEGORY_RETENTION sits between the two MEASURED points rather than at a round
number someone liked.

HONEST LIMIT, because magnitude is a proxy and not the thing itself. Three categories
were measured under the threshold on 2026-09-24 (`commerce`, `email-comms`,
`storage-files`); if three INDEXED categories thinned in the same week, a legitimate
refresh would land at 4 of 7 and this guard would reject it. That is a real false-reject
risk and it is accepted knowingly, because the two outcomes are not symmetric: a reject
is visible, nags, and is recoverable by re-running or refreshing by hand, while a
silently gutted index is a price check that stops existing and says nothing. The warning
text on every lost baseline is what makes a wrong reject diagnosable in one read instead
of two crawls.

WHAT A REJECT DOES, and the cost being accepted. A reject fails the whole refresh --
store included -- and refresh_seed.sh leaves every committed artifact untouched. That is
deliberate: the indexes are built FROM the store and describe it, so promoting a good
store beside stale indexes would ship exactly the mismatched pairing that the provenance
record shipped on 2026-09-21 (a record claiming 46,031 settlements beside a store holding
67,972 -- confidently wrong rather than absent). Keeping a consistent older set and
nagging is the lesser harm. The cost is that a bad index crawl can hold up a good store
refresh; the freshness clock (90 days of margin) is long enough to absorb a week of that,
and the nag says what happened.
"""
from __future__ import annotations

import json

# Category-baseline retention. Between the two measured points above: 7 -> 6 (86%,
# legitimate thinning) must ACCEPT; 7 -> 4 (57%, a measured shallow crawl) must REJECT.
# 0.7 puts the line at 4.9 of 7, so five or more survives and four does not.
MIN_CATEGORY_RETENTION = 0.7

# Divergence retention is a WARNING threshold, not a reject. AUDITED DOWN from a reject
# before this shipped, and the measurement is why: the healthy 2026-09-21 refresh moved
# this index 18 -> 15 by losing TEN of its eighteen payees and gaining seven. That is 56%
# of the membership replaced on a refresh nothing was wrong with. A refresh that lost the
# same ten and gained only one would sit at 9/18 and a count-based reject would have
# blocked it -- killing a good STORE refresh (the store rides on this verdict) over
# ordinary churn in a per-payee index.
#
# So the honest reading is that COUNT cannot separate churn from failure for this index,
# and pretending otherwise would buy a false-reject risk for no detection. What is
# unambiguous is EMPTY, and that stays a reject. Below this share the guard says so
# loudly and still ships.
DIVERGENCE_WARN_RETENTION = 0.5


def index_stats(category_index, divergence_index):
    """Summarize both indexes for the guard. PURE.

    Non-dict inputs read as EMPTY rather than raising: these files are rebuilt by
    crawling third parties, and `load_index_json` fails soft, so a truncated or
    malformed write must reach the guard as "nothing" and be rejected as such.
    """
    cats = category_index if isinstance(category_index, dict) else {}
    divs = divergence_index if isinstance(divergence_index, dict) else {}
    return {"categories": len(cats), "category_keys": sorted(cats),
            "divergences": len(divs), "divergence_keys": sorted(divs)}


def assess_index_refresh(old, new, *, min_category_retention=MIN_CATEGORY_RETENTION,
                         divergence_warn_retention=DIVERGENCE_WARN_RETENTION):
    """Decide whether freshly-built indexes may REPLACE the committed ones. PURE.

    Returns {accept, reasons[], warnings[], old, new}. `reasons` non-empty => REJECT
    (keep every committed artifact, store included -- see the module docstring for why
    the store rides along). `warnings` never block; they annotate an accept, and they are
    where a legitimate thinning gets recorded so the next reader does not have to re-run
    two crawls to find out which kind of shrink this was.
    """
    reasons, warnings = [], []

    # 1. EMPTY is a failed build, not a small one. An empty category index disables the
    #    category-price check for every category at once, silently.
    if old["categories"] and not new["categories"]:
        reasons.append(
            "category index is EMPTY (was %d) -- the index build produced nothing; the "
            "category-price check would be inert for every category"
            % old["categories"])
    if old["divergences"] and not new["divergences"]:
        reasons.append(
            "divergence index is EMPTY (was %d) -- the advertised-vs-settled check would "
            "have no baseline at all" % old["divergences"])

    # 2. COLLAPSE. Distinguishes a shallow/throttled crawl from honest thinning by
    #    magnitude, which is the only signal the finished artifact carries.
    if old["categories"] and new["categories"]:
        kept = new["categories"] / old["categories"]
        if kept < min_category_retention:
            reasons.append(
                "category baselines collapsed %d -> %d (kept %.0f%%, need >= %.0f%%) -- "
                "at this magnitude the likely cause is a shallow or throttled index "
                "crawl, not categories falling under MIN_CATEGORY_PAYEES; lost: %s"
                % (old["categories"], new["categories"], 100.0 * kept,
                   min_category_retention * 100,
                   ", ".join(sorted(set(old["category_keys"]) -
                                    set(new["category_keys"]))) or "(none)"))

    # WARNINGS. A shrink that clears the thresholds is accepted, but it is never silent:
    # every lost category is a check that stops running, and the operator is entitled to
    # see which one without diffing two JSON files by hand.
    lost_cats = sorted(set(old["category_keys"]) - set(new["category_keys"]))
    if lost_cats:
        warnings.append(
            "category baseline(s) no longer indexed: %s -- each is a category whose "
            "price check is now FAIL-OPEN (absent, not permissive-by-verdict). Expected "
            "when a category drops under MIN_CATEGORY_PAYEES; if it is unexpected, "
            "rebuild at higher --max-pages and compare before merging"
            % ", ".join(lost_cats))
    gained_cats = sorted(set(new["category_keys"]) - set(old["category_keys"]))
    if gained_cats:
        warnings.append("category baseline(s) newly indexed: %s" % ", ".join(gained_cats))

    # Divergence membership churns both ways on a healthy refresh; report the shape so an
    # accept still leaves a record, without pretending either direction is a problem.
    d_lost = len(set(old["divergence_keys"]) - set(new["divergence_keys"]))
    d_gained = len(set(new["divergence_keys"]) - set(old["divergence_keys"]))
    if d_lost or d_gained:
        warnings.append("divergence index churned: %d payee(s) lost, %d gained (%d -> %d)"
                        % (d_lost, d_gained, old["divergences"], new["divergences"]))
    if old["divergences"] and new["divergences"]:
        kept = new["divergences"] / old["divergences"]
        if kept < divergence_warn_retention:
            warnings.append(
                "divergence index shrank hard: %d -> %d (kept %.0f%%, below the %.0f%% "
                "notice line). NOT a reject -- see DIVERGENCE_WARN_RETENTION: measured "
                "churn on a healthy refresh replaced 56%% of this index's membership, so "
                "a count cannot tell churn from a failed crawl here. Worth an eye if it "
                "repeats"
                % (old["divergences"], new["divergences"], 100.0 * kept,
                   divergence_warn_retention * 100))

    return {"accept": not reasons, "reasons": reasons, "warnings": warnings,
            "old": old, "new": new}


def _load(path):
    """Read an index file. A missing or malformed file reads as {} -- see index_stats."""
    try:
        with open(path) as handle:
            blob = json.load(handle)
    except (OSError, ValueError):
        return {}
    return blob if isinstance(blob, dict) else {}


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="Gate the rebuilt seed indexes.")
    p.add_argument("--old-category", required=True, help="committed data/category_index.json")
    p.add_argument("--new-category", required=True, help="candidate category index")
    p.add_argument("--old-divergence", required=True, help="committed data/divergence_index.json")
    p.add_argument("--new-divergence", required=True, help="candidate divergence index")
    p.add_argument("--json", help="write the assessment JSON here")
    args = p.parse_args(argv)

    result = assess_index_refresh(
        index_stats(_load(args.old_category), _load(args.old_divergence)),
        index_stats(_load(args.new_category), _load(args.new_divergence)))
    for label in ("old", "new"):
        print("%s:" % label, {k: result[label][k] for k in ("categories", "divergences")})
    for w in result["warnings"]:
        print("WARN:", w)
    if result["accept"]:
        print("ACCEPT -- rebuilt indexes are safe to ship")
    else:
        print("REJECT -- keep the committed indexes (and the store beside them):")
        for r in result["reasons"]:
            print("  -", r)
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(result, handle, indent=2)
    return 0 if result["accept"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
