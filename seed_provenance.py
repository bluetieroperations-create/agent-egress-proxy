#!/usr/bin/env python3
"""
seed_provenance.py -- the corpus states its own limits, in the artifact itself.

WHY THIS EXISTS
---------------
`data/reputation_seed.db.gz` is a BOUNDED sample and for a long time nothing said
so. `chain_backfill` walks a payee newest-first and stops at a page cap, so every
high-volume payee's record is a recent WINDOW. A reader -- human or another
session -- opening the store sees settlement counts and dollar totals with no
indication that they are floors.

The cost of that silence is measured, not hypothetical. The largest x402 payee on
Base entered the corpus as **250 settlements from 1 payer**, dated to a 4-day
window. Its real history at the time of writing is **27,264,465 payments worth
$459,078.56 from 2,098 payers, spanning five months** -- the corpus held 0.0009%
of it. A four-chain merchant entered at 239 of its 29,231 Base transfers (0.8%).
Both numbers were then quoted in a published census as facts about the network.

Truncation is also not merely an undercount: because the walk is NEWEST-FIRST, it
INVERTS AGE. The most established payees present as the newest, because the only
rows kept are the most recent ones.

WHAT THIS DOES NOT DO
---------------------
It does not make the corpus complete, and it is not a step toward that. Full
depth for the 281 known payees is ~11.2M rows / ~1.8 GB, which cannot be
committed to a public repo. The corpus is bounded on purpose. This module makes
the bound EXPLICIT and MACHINE-READABLE, so a consumer can see the cap, how many
payees hit it, and how many failed to fetch at all.

Pure: `build_provenance` takes already-gathered facts and returns a dict. The CLI
is the only impure part.
"""
from __future__ import annotations

import json

#: Written into every record so a consumer never has to infer the shape.
COMPLETENESS = "BOUNDED"

#: The sentence a consumer must not be able to miss.
FLOOR_WARNING = (
    "Every settlement count and dollar total in this corpus is a FLOOR, not a "
    "measurement. Payees at the page cap hold a recent WINDOW of their history, "
    "so their totals are understated and their first_seen is too recent -- the "
    "walk is newest-first, which inverts age for the most established payees."
)


def build_provenance(store, crawl, *, max_pages, built_at, source="base-x402",
                     asset=None, method=None):
    """PURE: facts in, a provenance record out.

    `store` is a `refresh_guard.store_stats` dict; `crawl` a
    `chain_backfill.backfill` summary, or None when the corpus was not rebuilt by
    a crawl. A missing crawl yields nulls rather than zeros -- "we did not
    measure" must never serialize as "we measured zero", which is the same
    mistake as a guard treating an absent summary as a healthy one.
    """
    crawl = crawl or {}
    at_cap = crawl.get("truncated")
    failed = crawl.get("errors")
    attempted = crawl.get("payees")
    if attempted is not None and failed is not None:
        attempted = attempted + failed
    rec = {
        "corpus": source,
        "completeness": COMPLETENESS,
        "max_pages_per_payee": max_pages,
        "payees": store.get("payees"),
        "settlements": store.get("edges"),
        "distinct_payers": store.get("payers"),
        "gating_capable_payees": store.get("gating_capable"),
        "age_days": store.get("age_days"),
        "crawl_payees_attempted": attempted,
        "crawl_payees_at_page_cap": at_cap,
        "crawl_payees_failed": failed,
        "built_at": built_at,
        "method": method or (
            "chain_backfill.py: Blockscout token-transfer pages per known payee, "
            "newest-first, stopped at max_pages_per_payee"),
        "warning": FLOOR_WARNING,
        "measured_cost_of_the_bound": (
            "0xe9030014f5dae217d0a152f02a043567b16c1abf entered this corpus as 250 "
            "settlements from 1 payer over 4 days; its real history is 27,264,465 "
            "payments worth $459,078.56 from 2,098 payers over 5 months (verified "
            "EIP-3009 x402 settlements, read directly from Base). A four-chain "
            "merchant entered at 239 of 29,231 Base transfers."),
    }
    if not crawl:
        # Distinguish "no summary was recorded" from "the crawl reported nothing".
        # The nulls above are already the honest encoding; this says WHY, because
        # a reader seeing three nulls could otherwise read them as a clean crawl.
        rec["crawl_summary"] = (
            "NOT RECORDED for this build -- the crawl counts above are unmeasured, "
            "not zero. The refresh pipeline captures chain_backfill's summary from "
            "2026-09-16; records built after that carry real counts.")
    if asset:
        rec["asset"] = asset
    return rec


def main(argv=None):
    import argparse
    import datetime
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--store", required=True, help="the store the provenance describes (.gz/.db)")
    p.add_argument("--crawl", help="chain_backfill summary JSON for the run that built it")
    p.add_argument("--max-pages", type=int, required=True, help="the per-payee cap used")
    p.add_argument("--out", required=True, help="write the provenance JSON here")
    args = p.parse_args(argv)

    import refresh_guard as RG
    from check_seed_age import seed_age_days
    now = datetime.datetime.now(datetime.timezone.utc)
    edges, newest = RG._load(args.store)
    stats = RG.store_stats(edges, age_days=seed_age_days(newest, now) if newest else None)

    crawl = None
    if args.crawl:
        # chain_backfill writes JSON then a prose line; decode the leading object.
        with open(args.crawl) as fh:
            crawl, _ = json.JSONDecoder().raw_decode(fh.read().lstrip())

    rec = build_provenance(stats, crawl, max_pages=args.max_pages,
                           built_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    with open(args.out, "w") as fh:
        json.dump(rec, fh, indent=1)
        fh.write("\n")
    print("seed_provenance: wrote %s (%s, cap %d/payee, %s at cap, %s failed)"
          % (args.out, rec["completeness"], args.max_pages,
             rec["crawl_payees_at_page_cap"], rec["crawl_payees_failed"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
