#!/usr/bin/env python3
"""
directory_guard.py -- the release gate for an AUTOMATED `data/directory.json` refresh.

The sibling of `refresh_guard.py`, for the OTHER stale corpus. `refresh_guard` protects
the reputation store; nothing protected the directory, because nothing refreshed it:

    CLAUDE.md, before this module existed:
      "OPEN, NOT FIXED, and the operator should know: NOTHING AUTOMATICALLY REFRESHES
       data/directory.json. The weekly seed-refresh.yml regenerates the reputation seed,
       the category index and the divergence index -- not the directory. So this
       mechanism UN-REACHES ITSELF."

It un-reached itself on 2026-09-18: the corpus dated 2026-08-28 crossed
`payto_baseline.MAX_INDEX_AGE_DAYS` (21), the payTo gate went back to stale, and
`test_payto_baseline`'s tripwire turned the whole repository's CI red -- every unrelated
PR inherited a failure it had no part in. Automating the refresh is only safe if a bad
one can never ship, which is what this module decides.

A directory refresh can go bad four ways, and all four are REJECTs:

  1. PARTIAL CRAWL -- the Bazaar crawl hiccups and yields 12 entries instead of ~266.
     Shipping it would empty the payTo index, and an absent host reads as `unknown`, so
     the gate would silently stop covering the endpoints it used to cover.
  2. HOST LOSS -- the subtler shape of the same thing, and the one a count check cannot
     see. `build_payto_index` is keyed by HOST, not payee, because the claim the gate
     makes is about an endpoint's recipient; 58 of 266 payees advertise on more than one
     host. A candidate can retain 90% of its ENTRIES while dropping the multi-host
     payees that carry a disproportionate share of the index.
  3. UNDATED -- the candidate arrives with no content-pinned sidecar, or one whose hash
     does not match the bytes beside it. `payto_baseline._sidecar_age` returns None for
     both, which reads as stale, which gates nothing. Promoting an undated candidate
     over a dated one trades a corpus that works for one that is silently inert.
  4. NO PROGRESS, or fresh-but-already-stale. A candidate no newer than the committed
     one buys nothing. A candidate already past MAX_INDEX_AGE_DAYS buys nothing either:
     it would ship un-reachable, which is the exact state this refresh exists to leave.

On REJECT the automation keeps the committed directory untouched and opens a nag issue --
fail-safe, the same posture `refresh_seed.sh` takes. The pure decision is unit-tested;
the CLI loads two directories and exits 0 (accept) / non-zero (reject).
"""
from __future__ import annotations

import json

import payto_baseline as PB

# A refresh may legitimately shed endpoints that went offline, but a collapse means a
# partial crawl, not churn. Mirrors refresh_guard.MIN_RETENTION, and for the same
# reason: the shape being guarded against is a network failure mid-crawl, not drift.
MIN_RETENTION = 0.8

# HOST retention, the utility metric -- tighter than MIN_RETENTION because hosts are
# what the gate actually keys on. This is the directory's analogue of
# refresh_guard.MIN_GATING_RETENTION, which exists because the 2026-08-17 seed refresh
# kept 85% of payees while losing 12.7% of the ones that could earn a GO. The same blind
# spot applies here: entry count is not coverage.
MIN_HOST_RETENTION = 0.95

# Priced-entry retention. Its own constant rather than borrowing MIN_HOST_RETENTION:
# these measure different things and there is no reason they must move together, and a
# shared constant invites changing one and silently retuning the other.
MIN_PRICED_RETENTION = 0.95


def directory_stats(records, *, age_days=None, dated=None):
    """Summarize a directory corpus for the guard. PURE.

    `dated` is whether the corpus carries a sidecar that provably describes it -- kept
    SEPARATE from `age_days` being None, because the two mean different things to an
    operator: undated (no sidecar, or a hash mismatch) versus dated-but-unparseable.
    """
    records = records if isinstance(records, list) else []
    index = PB.build_payto_index(records)
    priced = sum(1 for r in records if isinstance(r, dict)
                 and r.get("min_price") is not None)
    screened = sum(1 for r in records if isinstance(r, dict)
                   and r.get("sanctioned"))
    return {"entries": len(records), "hosts": len(index), "priced": priced,
            "sanctioned": screened, "age_days": age_days,
            "dated": bool(dated) if dated is not None else age_days is not None}


def assess_directory_refresh(old, new, *, min_retention=MIN_RETENTION,
                             min_host_retention=MIN_HOST_RETENTION,
                             min_priced_retention=MIN_PRICED_RETENTION,
                             max_age_days=None):
    """Decide whether a freshly-crawled directory may REPLACE the committed one. PURE.

    Returns {accept, reasons[], warnings[], old, new}. `reasons` non-empty => REJECT
    (keep the committed directory). `warnings` never block -- they annotate an accept.
    """
    max_age_days = PB.MAX_INDEX_AGE_DAYS if max_age_days is None else max_age_days
    reasons, warnings = [], []

    # 1. the candidate must exist at all. A zero-entry directory is not a small
    #    refresh, it is a failed crawl whose output happens to be valid JSON.
    if not new["entries"]:
        reasons.append("candidate directory is EMPTY -- the crawl produced nothing")

    # 2. entry retention -- a collapse means a partial crawl.
    if old["entries"] and new["entries"] < old["entries"] * min_retention:
        reasons.append(
            "entry count collapsed %d -> %d (kept %.0f%%, need >= %.0f%%) -- likely a "
            "partial crawl" % (old["entries"], new["entries"],
                               100.0 * new["entries"] / old["entries"],
                               min_retention * 100))

    # 3. HOST retention -- the metric that survives a healthy-looking entry count.
    #    An absent host reads as `unknown`, so losing hosts silently narrows the gate.
    if old["hosts"] and new["hosts"] < old["hosts"] * min_host_retention:
        reasons.append(
            "payTo-index hosts collapsed %d -> %d (kept %.0f%%, need >= %.0f%%) -- %d "
            "endpoint host(s) the gate used to cover would read as `unknown`; an entry "
            "count cannot see this" % (old["hosts"], new["hosts"],
                                       100.0 * new["hosts"] / old["hosts"],
                                       min_host_retention * 100,
                                       old["hosts"] - new["hosts"]))

    # 4. the candidate must be DATED, and provably about itself. An undated corpus
    #    reads as stale and gates nothing, so promoting one over a dated corpus
    #    replaces a working gate with an inert one -- worse than not refreshing.
    if not new["dated"]:
        reasons.append(
            "candidate is UNDATED -- no content-pinned sidecar, or its sha256 does not "
            "match the bytes beside it; payto_baseline would read it as stale and "
            "would not gate at all")

    # 5. freshness: real progress, and a result that is actually reachable.
    na, oa = new.get("age_days"), old.get("age_days")
    if na is not None:
        if na >= max_age_days:
            reasons.append(
                "candidate is already stale: %.1f days old (>= MAX_INDEX_AGE_DAYS %d) "
                "-- promoting it would ship an un-reachable gate, which is the state "
                "this refresh exists to leave" % (na, max_age_days))
        if oa is not None and na >= oa:
            reasons.append(
                "no progress: candidate age %.1f d >= committed %.1f d -- nothing to "
                "ship" % (na, oa))

    # WARNINGS -- these annotate an accept, they never block it.
    #
    # A refresh that keeps its hosts but loses PRICES still costs the gate something:
    # payto_baseline compares an advertised price against the settled baseline, so an
    # unpriced entry is in the index but contributes no comparison. Not a reject,
    # because the host coverage that `unknown` depends on is intact.
    if old["priced"] and new["priced"] < old["priced"] * min_priced_retention:
        warnings.append(
            "priced entries fell %d -> %d -- those endpoints stay in the index but "
            "carry no advertised price to compare against"
            % (old["priced"], new["priced"]))

    # A SANCTIONED entry shipping in the corpus the gate reads is worth saying out loud.
    #
    # This warning used to be the other way round -- it fired when the sanctioned count
    # FELL to zero, on the theory that zero means NOT SCREENED rather than clean. That
    # was wrong twice over, and the audit caught it before this shipped. It was dead
    # code: every entry in the real corpus carries `sanctioned: false`, so the count is
    # 0 on both sides and the condition could never fire. And it was a category error:
    # the artifact genuinely cannot distinguish "screened, none sanctioned" from "the
    # list was unavailable", because a False is written either way. Only
    # `ecosystem_scan._load_sanctioned` sees that difference, and it warns on stderr at
    # crawl time. A guard reading the finished artifact has no access to it, and
    # pretending otherwise would put a reassuring-but-unfounded line in the record.
    if new["sanctioned"]:
        warnings.append(
            "%d candidate entry(ies) are flagged SANCTIONED and would ship in the "
            "directory the payTo gate reads" % new["sanctioned"])

    return {"accept": not reasons, "reasons": reasons, "warnings": warnings,
            "old": old, "new": new}


def _load(path):
    """Read a directory corpus and its sidecar age. Returns (records, age, dated)."""
    try:
        with open(path) as handle:
            records = json.load(handle)
    except (OSError, ValueError):
        return [], None, False
    if not isinstance(records, list):
        return [], None, False
    age = PB.index_age_days(path)
    return records, age, age is not None


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="Gate an automated directory refresh.")
    p.add_argument("--old", required=True, help="current committed data/directory.json")
    p.add_argument("--new", required=True, help="freshly-crawled candidate directory")
    p.add_argument("--json", help="write the assessment JSON here")
    args = p.parse_args(argv)

    def _stats(path):
        records, age, dated = _load(path)
        return directory_stats(records, age_days=age, dated=dated)

    result = assess_directory_refresh(_stats(args.old), _stats(args.new))
    for label in ("old", "new"):
        print("%s:" % label, {k: result[label][k] for k in
                              ("entries", "hosts", "priced", "age_days", "dated")})
    for w in result["warnings"]:
        print("WARN:", w)
    if result["accept"]:
        print("ACCEPT -- directory refresh is safe to ship")
    else:
        print("REJECT -- keep the committed directory:")
        for r in result["reasons"]:
            print("  -", r)
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(result, handle, indent=2)
    return 0 if result["accept"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
