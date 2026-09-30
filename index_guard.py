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

IT NOW READS VALUES, NOT ONLY KEYS -- added 2026-09-28, and the gap is worth recording
because it was the same shape one level down. Everything above reasons about how many
baselines survive. Nothing looked at what a baseline SAID, so a refresh could keep all
six and move every price arbitrarily and still draw a clean ACCEPT. That is what the
2026-09-28 refresh did: three of six baselines moved 25-56%, the guard had nothing to
say, and the moves turned out to track drift in the Bazaar's category MEMBERSHIP rather
than in settled prices (the old store crawled the same day already yields the new
values). Two checks came out of it, and they are deliberately asymmetric:

  - VALUE_DRIFT_WARN_RATIO only WARNS, because the only calibration available is a floor
    (2.25x, measured legitimate) with no measured ceiling.
  - A non-positive or unparseable baseline REJECTS, because that one is unambiguous and
    fails CLOSED: `50 * 0` puts the hold line at zero and holds every payment in the
    category.

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
from decimal import Decimal, InvalidOperation

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

# VALUE drift. This guard counted KEYS and never read a PRICE, so a refresh could keep
# every baseline and move all of them arbitrarily -- the same fail-open shape the module
# was built to close, one level down. Found by auditing the 2026-09-28 refresh, which
# moved three of six baselines 25-56% and drew an ACCEPT with nothing said.
#
# CALIBRATED FROM ONE SIDE ONLY, which is exactly why this WARNS and never rejects. That
# refresh was verified CORRECT -- rebuilt byte-identically from the shipped store against
# an independent crawl -- and it moved:
#
#     dev-tools      0.0045   -> 0.002      2.25x   LEGITIMATE
#     onchain        0.0025   -> 0.0035     1.40x   LEGITIMATE
#     search-data    0.008    -> 0.01       1.25x   LEGITIMATE
#     content-media  0.009104 -> 0.008552   1.06x   LEGITIMATE
#
# So 2.25x is a MEASURED-GOOD move and the line has to sit above it. There is no measured
# BAD value at all. Unlike MIN_CATEGORY_RETENTION, which sits between 7->6 (good) and
# 7->4 (bad), this has a floor and no ceiling, and a reject built on one anchor would be
# an invented number blocking a good store refresh. 3.0 annotates; it never gates.
#
# Worth recording WHY those three moved, because the artifact suggests otherwise: the OLD
# store crawled on the same day already yields the NEW values, so they tracked drift in
# the Bazaar's category MEMBERSHIP, not in settled prices. `category_pricing` takes its
# payee->category map from a live crawl and its amounts from the store, and the finished
# index cannot tell you which one moved.
VALUE_DRIFT_WARN_RATIO = 3.0

# THE ARTIFACT SIGNATURE, and the reason it needs no threshold at all.
#
# VALUE_DRIFT_WARN_RATIO asks HOW FAR a baseline moved, and the 2026-09-29 refresh
# showed that question cannot answer the one that matters. `onchain` moved 2.33x and was
# pure crawl drift; the 2026-09-28 refresh's `dev-tools` moved 2.25x and was legitimate.
# Three and a half percent apart, opposite verdicts. Magnitude does not separate them,
# which is why that line only ever annotates -- and why it stayed silent on both.
#
# WHAT DOES separate them is WHO the baseline is computed over. A category rate is a
# median-of-medians across the distinct payees a LIVE Bazaar crawl put in that category,
# priced from the store. So a move has exactly two possible sources, and the payee count
# tells them apart:
#
#   the same payees, a different price   -> the market moved            REAL
#   different payees, the same market    -> the crawl's membership moved ARTIFACT
#
# Measured on the two refreshes that produced the rule (payee counts from the sidecar):
#
#   onchain        0.0035 -> 0.0015   2.33x   10 -> 8 payees   ARTIFACT, proven: the
#                                                              OLD store crawled the same
#                                                              day already yields 0.0015
#   content-media  0.008552 -> 0.00675 1.27x   6 -> 6 payees   REAL, proven: rebuilding
#                                                              from the store alone
#                                                              reproduces it exactly
#   ai-agents      unchanged                  26 -> 25         churn, no move: silent
#   dev-tools      unchanged                   7 -> 5          churn, no move: silent
#   finance        unchanged                  29 -> 28         churn, no move: silent
#   search-data    unchanged                  19 -> 23         growth: silent
#
# The CONJUNCTION classifies all six correctly, so there is no number to fit and none is
# invented: a baseline that moved AT ALL while its payee set SHRANK is reporting a
# different population, not a different market. Either half alone is ordinary -- prices
# move, membership churns -- and neither fires on its own.
#
# Still a WARNING. The artifact is not wrong, it is just not what the artifact appears to
# say: the new rate does describe the payees now in the category. What an operator needs
# is to know which question the number answered before acting on a HOLD line built from
# it. Rejecting would throw away a whole store refresh over a naming question.


def _rate(raw):
    """`raw` as a strictly-positive Decimal rate, else None. FAIL-SOFT by the same
    argument as index_stats: these files are rebuilt by crawling third parties, so a
    malformed value must arrive as "not a rate" rather than as an exception."""
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    # `Decimal("NaN")` PARSES and then raises InvalidOperation on comparison, so the
    # finiteness check has to come before the `> 0`. Caught by the test rather than by
    # reading, which is the argument for having written it.
    return d if d.is_finite() and d > 0 else None


def _hold_ratio():
    """`blackwall.CATEGORY_HOLD_RATIO`, or 50.0 if blackwall will not import.

    The drift warning quotes the resulting HOLD LINE in dollars, which is a claim ABOUT
    blackwall's constant -- so hardcoding it here would let this module report a hold line
    that does not exist the day someone retunes the gate. Imported LAZILY with a fallback
    rather than at module scope: index_guard runs inside refresh_seed.sh, where staying
    stdlib-only is the reason it can run at all (blackwall's dependency chain has raised a
    pyo3 PanicException in constrained environments, which no `except ImportError` catches).
    test_the_warning_uses_blackwalls_own_ratio pins the fallback to the real constant, so
    the two cannot drift silently.
    """
    try:
        from blackwall import CATEGORY_HOLD_RATIO
        return float(CATEGORY_HOLD_RATIO)
    except BaseException:
        return 50.0


def _payee_note(stats, category):
    """Explain a thin baseline when the sidecar is present, else say nothing."""
    n = (stats.get("category_payees") or {}).get(category)
    if not isinstance(n, int):
        return ""
    return ("; it rests on %d distinct payee(s), so it is a median-of-medians over a "
            "small set and moves when one enters or leaves" % n)


def index_stats(category_index, divergence_index, category_payees=None):
    """Summarize both indexes for the guard. PURE.

    Non-dict inputs read as EMPTY rather than raising: these files are rebuilt by
    crawling third parties, and `load_index_json` fails soft, so a truncated or
    malformed write must reach the guard as "nothing" and be rejected as such.
    """
    cats = category_index if isinstance(category_index, dict) else {}
    divs = divergence_index if isinstance(divergence_index, dict) else {}
    counts = category_payees if isinstance(category_payees, dict) else {}
    return {"categories": len(cats), "category_keys": sorted(cats),
            # The BASELINES, not just their names. A guard that reads only keys cannot
            # see a price move -- see VALUE_DRIFT_WARN_RATIO.
            "category_values": {str(k): str(v) for k, v in cats.items()},
            # Optional {category: distinct_payee_count} sidecar from category_pricing.
            # Absent it, drift is still DETECTED; it just cannot be EXPLAINED.
            "category_payees": {str(k): v for k, v in counts.items()
                                if isinstance(v, int) and not isinstance(v, bool)},
            "divergences": len(divs), "divergence_keys": sorted(divs)}


def assess_index_refresh(old, new, *, min_category_retention=MIN_CATEGORY_RETENTION,
                         divergence_warn_retention=DIVERGENCE_WARN_RETENTION,
                         value_drift_warn_ratio=VALUE_DRIFT_WARN_RATIO):
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

    # 3. A NON-POSITIVE or unparseable baseline is a broken build, and this one fails
    #    CLOSED rather than open, which is why it rejects where a price MOVE only warns.
    #    blackwall holds at `quoted >= CATEGORY_HOLD_RATIO * median`, so a median of 0
    #    puts the hold line at 0 and EVERY payment in that category is held. There is
    #    nothing to calibrate: no market rate is zero, and an unparseable one is not a
    #    rate at all. Checked on `new` alone -- a bad value is bad regardless of history.
    for cat in sorted(new.get("category_values") or {}):
        raw = new["category_values"][cat]
        if _rate(raw) is None:
            reasons.append(
                "category baseline %s is %r -- not a usable market rate; at "
                "CATEGORY_HOLD_RATIO this puts the hold line at or below zero, which "
                "holds EVERY payment in the category (fail-CLOSED, not fail-open)"
                % (cat, raw))

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

    # VALUE DRIFT, on the baselines that SURVIVED. Never a reject: see
    # VALUE_DRIFT_WARN_RATIO for why this threshold is calibrated from one side only.
    # A baseline that moves takes the HOLD line with it, so the warning states the line
    # in dollars rather than leaving the reader to multiply.
    old_vals = old.get("category_values") or {}
    new_vals = new.get("category_values") or {}
    for cat in sorted(set(old_vals) & set(new_vals)):
        before, after = _rate(old_vals[cat]), _rate(new_vals[cat])
        if before is None or after is None:
            continue          # rejected above, or absent from a hand-built stats dict
        ratio = max(before / after, after / before)
        if ratio >= Decimal(str(value_drift_warn_ratio)):
            hold = Decimal(str(_hold_ratio()))
            warnings.append(
                "category baseline %s moved %s -> %s (%.2fx %s), taking the HOLD line "
                "with it: a quote is held at >= %s instead of >= %s. NOT a reject -- a "
                "2.25x move was measured LEGITIMATE on 2026-09-28, so this line only "
                "annotates%s"
                % (cat, old_vals[cat], new_vals[cat], ratio,
                   "down" if after < before else "up",
                   after * hold, before * hold, _payee_note(new, cat)))

    # ARTIFACT SIGNATURE: the baseline moved AND the payee set it is computed over shrank.
    # See the comment on VALUE_DRIFT_WARN_RATIO for why this needs no threshold and why
    # magnitude alone cannot do this job. Silent without BOTH sidecars, because a missing
    # count is not evidence of a stable population.
    old_counts = old.get("category_payees") or {}
    new_counts = new.get("category_payees") or {}
    for cat in sorted(set(old_vals) & set(new_vals)):
        before, after = _rate(old_vals[cat]), _rate(new_vals[cat])
        if before is None or after is None or before == after:
            continue                      # unchanged rate: nothing to attribute
        op, np_ = old_counts.get(cat), new_counts.get(cat)
        if not isinstance(op, int) or not isinstance(np_, int) or np_ >= op:
            continue                      # population held or grew: the move is the market
        hold = Decimal(str(_hold_ratio()))
        warnings.append(
            "category baseline %s moved %s -> %s WHILE its payee set shrank %d -> %d -- "
            "this rate is computed over the payees a live crawl put in the category, so a "
            "move that arrives alongside a smaller population is reporting a DIFFERENT "
            "POPULATION, not a different market. The HOLD line moved with it (>= %s "
            "instead of >= %s). NOT a reject: the new rate does describe the payees now in "
            "the category. Confirm before treating it as a price signal -- rebuild the "
            "index against the OLD store; if it still yields the new rate, the crawl moved "
            "and the market did not"
            % (cat, old_vals[cat], new_vals[cat], op, np_, after * hold, before * hold))

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
    p.add_argument("--old-meta", help="committed category_index.meta.json (optional; with "
                   "--new-meta it supplies the BEFORE payee counts, which is what lets a "
                   "move be attributed to the market or to the crawl's membership)")
    p.add_argument("--new-meta", help="candidate category_index.meta.json (optional; "
                   "supplies distinct-payee counts so a drift warning can say whether "
                   "the baseline was thin)")
    p.add_argument("--json", help="write the assessment JSON here")
    args = p.parse_args(argv)

    # The sidecar is OPTIONAL and only annotates: a guard that refused to run without
    # it would make a new file a prerequisite for gating an old one.
    old_meta = _load(args.old_meta) if args.old_meta else {}
    new_meta = _load(args.new_meta) if args.new_meta else {}
    result = assess_index_refresh(
        index_stats(_load(args.old_category), _load(args.old_divergence),
                    category_payees=old_meta.get("payees")),
        index_stats(_load(args.new_category), _load(args.new_divergence),
                    category_payees=new_meta.get("payees")))
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
