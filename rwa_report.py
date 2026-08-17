#!/usr/bin/env python3
"""
rwa_report.py -- turn the accumulation corpus into operator intelligence.

`rwa_ledger.py` WRITES the corpus (every RWA buy + its T+N outcome). This module READS
it back into a legible "state of tokenized RWAs as Blackwall sees it" -- the payoff that
makes the accumulated moat actionable, not just stored:

  * TOTALS + verdict mix + restriction-posture distribution.
  * ISSUER DIRECTORY -- each issuer's earned trust grade (issuer_trust) + settlement
    reliability / underwater rate, ranked best-first.
  * LEADERBOARDS -- assets most OVERPRICED vs their underlying (peg divergence) and most
    UNDERWATER after purchase (the vindicated-warning signal).

PURE derivation over ledger events (reuses RwaLedger's aggregators) + a CLI. DESCRIPTIVE
reporting only -- it never gates. Stdlib.
"""
from __future__ import annotations

from collections import Counter

from rwa_ledger import RwaLedger, issuer_trust

_GRADE_ORDER = {"high": 0, "medium": 1, "low": 2, "insufficient": 3}


def _asset_key(e):
    return (e.get("token"), e.get("chain"))


def build_report(events, top=10):
    """PURE: derive the intelligence report from a list of ledger events. NEVER raises."""
    events = [e for e in (events or []) if isinstance(e, dict)]
    led = RwaLedger("")                      # aggregators accept explicit events (no I/O)
    buys = [e for e in events if e.get("event", "buy") == "buy"]
    outs = [e for e in events if e.get("event") == "outcome"]
    issuers = sorted({e.get("issuer") for e in buys if e.get("issuer")})
    asset_keys = sorted({_asset_key(e) for e in buys if e.get("token")},
                        key=lambda k: (k[0] or "", k[1] or ""))

    directory = []
    for iss in issuers:
        prof = led.issuer_profile(iss, events=events) or {}
        grade = issuer_trust(prof)["grade"]
        directory.append({
            "issuer": iss, "trust": grade,
            "buys": prof.get("count"), "distinct_assets": prof.get("distinct_assets"),
            "outcomes": prof.get("outcomes"),
            "settlement_success_rate": prof.get("settlement_success_rate"),
            "underwater_rate": prof.get("underwater_rate"),
        })
    directory.sort(key=lambda d: (_GRADE_ORDER.get(d["trust"], 9), -(d["buys"] or 0)))

    assets = [led.asset_profile(t, c, events=events) for (t, c) in asset_keys]
    assets = [a for a in assets if a]

    def _rank(key):
        rows = [a for a in assets if a.get(key) is not None]
        rows.sort(key=lambda a: -a[key])
        return [{"symbol": a.get("symbol"), "issuer": a.get("issuer"),
                 "token": a.get("token"), "chain": a.get("chain"), key: a[key]}
                for a in rows[:top]]

    return {
        "totals": {"buys": len(buys), "outcomes": len(outs),
                   "distinct_assets": len(asset_keys), "distinct_issuers": len(issuers)},
        "verdict_mix": dict(Counter(e.get("verdict") for e in buys)),
        "restriction_posture": dict(Counter(e.get("restriction_grade") for e in buys
                                            if e.get("restriction_grade"))),
        "issuer_directory": directory,
        "most_overpriced": _rank("max_peg_ratio"),
        "most_underwater": _rank("underwater_rate"),
    }


def main(argv=None):
    """CLI: print the corpus intelligence report as JSON.
    `python rwa_report.py rwa.jsonl [--top N]`"""
    import argparse
    import json
    import sys
    ap = argparse.ArgumentParser(description="Report on the RWA accumulation corpus.")
    ap.add_argument("ledger", help="path to the rwa_ledger JSONL corpus")
    ap.add_argument("--top", type=int, default=10, help="leaderboard length (default 10)")
    args = ap.parse_args(argv)
    report = build_report(RwaLedger(args.ledger).load(), top=args.top)
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
