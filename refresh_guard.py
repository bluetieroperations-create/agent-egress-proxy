#!/usr/bin/env python3
"""
refresh_guard.py -- the release gate for an AUTOMATED seed refresh (Stage 2 of
docs/DATA_COMPLETENESS.md: lock in durability -- kill the stale cliff without a human
in the loop, and without ever shipping a WORSE corpus).

Today the free-tier store is refreshed by hand (scripts/refresh_seed.sh) after a nag
issue. Automating that is only safe if a bad refresh can never ship. A refresh can go
bad two ways:
  1. PARTIAL CRAWL -- a network hiccup mid-backfill yields a sparse store (5 payees
     instead of ~290). Shipping it would send the whole corpus cold-start (HOLD).
  2. NO PROGRESS -- the crawl produced a store no fresher than the current one, so the
     commit/redeploy churn buys nothing.

`assess_refresh(old, new)` is a PURE decision that REJECTS both: the new store must
retain most of the old store's size AND be genuinely fresher (and actually fresh). A
convergence regression (Stage-1 property) is surfaced as a WARNING, not a reject --
freshness still wins (a stale store is worse), and the SEPARATE seed-regression test
(test_coverage_eval) is what keeps sybil_ring advisory on a regressed corpus. That
separation is deliberate: the guard protects the DATA; the gate protects the RULE.

On REJECT the automation keeps the current committed store untouched and opens a nag
issue -- fail-safe. The pure logic is unit-tested with mutation notes; the CLI loads
two stores and exits 0 (accept) / non-zero (reject).
"""
from __future__ import annotations

import coverage_eval as CE
import payer_graph as PG
import payer_reputation as PR
from check_seed_age import REFRESH_WARN_DAYS

# A refresh may legitimately shed inactive payees, but a large drop means a partial
# crawl, not churn. Keep >= this share of the old corpus's payees AND edges.
MIN_RETENTION = 0.8

# A refresh must not meaningfully shrink the set of payees that can actually EARN a GO.
# MEASURED GAP: the 2026-08-17 refresh kept 85% of payees and 87% of edges -- clearing
# MIN_RETENTION comfortably -- while gating-capable payees fell 237 -> 207 (-12.7%), so
# 43 counterparties that previously cleared the gates would have started returning a
# cold-start/Sybil HOLD. Size retention and `gating_reachable` (a BOOLEAN, true while any
# single payee qualifies) were both blind to it. This is the utility metric: tighter than
# MIN_RETENTION because it measures what users actually experience.
MIN_GATING_RETENTION = 0.95

# Mirrors blackwall's verdict gates -- imported lazily in gating_capable() to keep this
# module importable without pulling in the whole engine.
_GATE_MIN_SETTLEMENTS = 20      # blackwall.THIN_HISTORY_SETTLEMENTS
_GATE_MIN_PAYERS = 3            # blackwall.MIN_DISTINCT_PAYERS


def gating_capable(edges, graph=None, *, min_settlements=None, min_payers=None):
    """PURE: how many payees could actually EARN a GO -- i.e. clear BOTH the thin-history
    gate (>= N settlements) and the Sybil gate (>= M distinct payers)?

    This is the corpus's UTILITY, as opposed to its size. Normalization matches
    build_index exactly (lowercased, blanks and self-vouch edges dropped) so the counts
    line up with the graph. NEVER raises."""
    from collections import Counter
    try:
        from blackwall import MIN_DISTINCT_PAYERS, THIN_HISTORY_SETTLEMENTS
    except Exception:                      # keep the guard usable standalone
        THIN_HISTORY_SETTLEMENTS, MIN_DISTINCT_PAYERS = (_GATE_MIN_SETTLEMENTS,
                                                         _GATE_MIN_PAYERS)
    need_n = min_settlements if min_settlements is not None else THIN_HISTORY_SETTLEMENTS
    need_p = min_payers if min_payers is not None else MIN_DISTINCT_PAYERS
    if graph is None:
        # build_index assumes well-formed (payer, payee) pairs and raises on junk; this
        # function promises it never does, so absorb it here rather than loosening a
        # contract other callers rely on.
        try:
            graph = PG.build_index(edges)
        except Exception:
            return 0
    counts = Counter()
    for edge in edges or ():
        try:
            payer, payee = PG._norm(edge[0]), PG._norm(edge[1])
        except Exception:
            continue
        if not payer or not payee or payer == payee:
            continue
        counts[payee] += 1
    return sum(1 for payee, payers in graph.get("payee_to_payers", {}).items()
               if counts.get(payee, 0) >= need_n and len(payers) >= need_p)


def store_stats(edges, *, age_days=None):
    """Pure structural summary of a store's settlement edges. `age_days` (freshness) is
    passed in so this stays clock-free; the CLI computes it. Includes the Stage-1
    coverage-convergence verdict so a refresh that breaks it is visible."""
    graph = PG.build_index(edges)
    payees = len(graph["payee_to_payers"])
    payers = len(graph["payer_to_payees"])
    if payees:
        v = CE.convergence_verdict(CE.coverage_curve(edges))
        reachable = bool(v.get("gating_reachable"))
        rate = v.get("subfull_false_flag_rate")
    else:
        reachable, rate = False, None
    return {
        "payees": payees,
        "payers": payers,
        "edges": len(edges),
        "gating_capable": gating_capable(edges, graph),
        "anchors": len(PR.anchor_payees(graph)),
        "age_days": age_days,
        "gating_reachable": reachable,
        "subfull_false_flag_rate": rate,
    }


def assess_refresh(old, new, *, min_retention=MIN_RETENTION, warn_days=REFRESH_WARN_DAYS,
                   min_gating_retention=MIN_GATING_RETENTION):
    """Decide whether a freshly-crawled store may REPLACE the committed one. PURE.
    Returns {accept, reasons[], warnings[], old, new}. `reasons` non-empty => REJECT
    (keep the current store). `warnings` never block -- they annotate an accept."""
    reasons, warnings = [], []

    # 1. size retention -- a collapse means a partial/failed crawl, not healthy churn.
    if old["payees"] and new["payees"] < old["payees"] * min_retention:
        reasons.append(
            "payee count collapsed %d -> %d (kept %.0f%%, need >= %.0f%%) -- likely a "
            "partial crawl" % (old["payees"], new["payees"],
                               100.0 * new["payees"] / old["payees"], min_retention * 100))
    if old["edges"] and new["edges"] < old["edges"] * min_retention:
        reasons.append(
            "edge count collapsed %d -> %d (kept %.0f%%, need >= %.0f%%)"
            % (old["edges"], new["edges"],
               100.0 * new["edges"] / old["edges"], min_retention * 100))

    # 1b. UTILITY retention -- the metric that survives a "healthy-looking" size check.
    #     A corpus can keep 85% of its payees while losing 13% of the ones that can
    #     actually clear the gates; those are the payees a user notices.
    og, ng = old.get("gating_capable"), new.get("gating_capable")
    if og and ng is not None and ng < og * min_gating_retention:
        reasons.append(
            "gating-capable payees collapsed %d -> %d (kept %.0f%%, need >= %.0f%%) -- "
            "%d counterparty(ies) that could earn a GO would now be HELD; a size-only "
            "check cannot see this" % (og, ng, 100.0 * ng / og,
                                       min_gating_retention * 100, og - ng))

    # 2. freshness -- the refresh must make progress AND yield an actually-fresh store.
    na, oa = new.get("age_days"), old.get("age_days")
    if na is not None:
        if na > warn_days:
            reasons.append(
                "not fresh: new store is %d days old (> %d-day warn) -- crawl produced "
                "stale timestamps" % (na, warn_days))
        if oa is not None and na >= oa:
            reasons.append(
                "no progress: new store age %d d >= current %d d -- nothing to ship" % (na, oa))

    # 3. convergence regression -- WARN only (freshness wins; the gate is protected
    #    independently by test_coverage_eval's seed-regression check).
    if old.get("gating_reachable") and not new.get("gating_reachable"):
        warnings.append(
            "coverage convergence REGRESSED on the new corpus -- sybil_ring must stay "
            "ADVISORY (test_coverage_eval enforces this); refresh still accepted for "
            "freshness")

    return {"accept": not reasons, "reasons": reasons, "warnings": warnings,
            "old": old, "new": new}


def _load(path):
    """(edges, newest_ts_iso) for a .db or .gz store."""
    import gzip
    import os
    import shutil
    import sqlite3
    import tempfile
    import reputation_store as RS
    tmp = None
    try:
        if path.endswith(".gz"):
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
            with gzip.open(path, "rb") as f, open(tmp, "wb") as o:
                shutil.copyfileobj(f, o)
            db = tmp
        else:
            db = path
        edges = list(RS.ReputationStore(db).iter_settlement_edges())
        row = sqlite3.connect(db).execute("SELECT MAX(ts) FROM settlements").fetchone()
        return edges, (row[0] if row else None)
    finally:
        if tmp:
            os.unlink(tmp)


def main(argv=None):
    import argparse
    import datetime
    import json
    from check_seed_age import seed_age_days
    p = argparse.ArgumentParser(description="Gate an automated seed refresh.")
    p.add_argument("--old", required=True, help="current committed store (.gz/.db)")
    p.add_argument("--new", required=True, help="freshly-crawled candidate store (.gz/.db)")
    p.add_argument("--json", help="write the assessment JSON here")
    args = p.parse_args(argv)
    now = datetime.datetime.now(datetime.timezone.utc)

    def _stats(path):
        edges, newest = _load(path)
        age = seed_age_days(newest, now) if newest else None
        return store_stats(edges, age_days=age)

    result = assess_refresh(_stats(args.old), _stats(args.new))
    print("old:", {k: result["old"][k] for k in ("payees", "edges", "age_days",
                                                  "gating_reachable")})
    print("new:", {k: result["new"][k] for k in ("payees", "edges", "age_days",
                                                  "gating_reachable")})
    for w in result["warnings"]:
        print("WARN:", w)
    if result["accept"]:
        print("ACCEPT -- refresh is safe to ship")
    else:
        print("REJECT -- keep the current store:")
        for r in result["reasons"]:
            print("  -", r)
    if args.json:
        json.dump(result, open(args.json, "w"), indent=2)
    return 0 if result["accept"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
