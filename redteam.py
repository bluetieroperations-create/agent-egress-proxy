#!/usr/bin/env python3
"""
redteam.py -- adversarial coverage scorecard for the verdict engine.

Runs a battery of attacks and legit controls through blackwall.decide_payment and
tabulates the outcome, HONESTLY -- including the documented gaps where an attack
gets through (large Sybil farms above the graph ceiling; the advisory-only signals).
The point is an auditable "what we catch and what we don't", not a green checkmark.

Dispositions:
  CAUGHT       -- an attack was blocked (HOLD/STOP) as intended.
  KNOWN GAP    -- an attack got GO; a documented limitation, not a surprise.
  CLEAN        -- a legit payment correctly GOes.
  FALSE POSITIVE -- a legit payment was wrongly blocked (a real bug if it appears).

`run()` is importable (used by test_redteam to guard that the CAUGHT set stays caught
and no FALSE POSITIVE appears); `main()` prints the table / writes JSON.
"""
from __future__ import annotations

import blackwall as bw

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
LEGIT = "0x" + "1" * 40
ATTACKER = "0x" + "2" * 40
STABLE = ["0.09", "0.09", "0.088", "0.092"]     # median 0.09
# a reputable, in-budget, fair-priced baseline record.
GOOD = {"settlement_count": 500, "dispute_rate": 0.0, "distinct_payers": 30}

# (name, category, expect: "block"|"allow", known_gap: bool, decide_payment kwargs)
SCENARIOS = [
    # --- attacks that MUST be caught ---
    ("sanctioned counterparty", "sanctions", "block", False,
     dict(amount="0.09", record=dict(GOOD, sanctioned=True), price_history=STABLE,
          counterparty=LEGIT)),
    ("known-bad counterparty", "known-bad", "block", False,
     dict(amount="0.09", record=dict(GOOD, known_bad=True), price_history=STABLE,
          counterparty=LEGIT)),
    ("recipient mismatch vs 402", "payload-sim", "block", False,
     dict(amount="0.09", record=GOOD, price_history=STABLE,
          counterparty=ATTACKER, expected_recipient=LEGIT)),
    ("price gouge (>=8x median)", "price", "block", False,
     dict(amount="0.72", record=GOOD, price_history=STABLE, counterparty=LEGIT)),
    ("price anomaly (moderate)", "price", "block", False,
     dict(amount="0.30", record=GOOD, price_history=STABLE, counterparty=LEGIT)),
    ("thin history (<20 settlements)", "thin", "block", False,
     dict(amount="0.09", record={"settlement_count": 5, "dispute_rate": 0.0},
          price_history=STABLE, counterparty=LEGIT)),
    ("Sybil: <3 distinct payers", "sybil", "block", False,
     dict(amount="0.09", record={"settlement_count": 500, "distinct_payers": 2,
                                 "dispute_rate": 0.0},
          price_history=STABLE, counterparty=LEGIT)),
    ("captive Sybil (graph)", "sybil-graph", "block", False,
     dict(amount="0.09", record=GOOD, price_history=STABLE, counterparty=LEGIT,
          payer_graph_signal={"captive_sybil": True, "distinct_payers": 4})),
    ("going bad (recent disputes)", "outcome", "block", False,
     dict(amount="0.09", record=dict(GOOD, recent_dispute_rate=0.5, recent_outcomes=10),
          price_history=STABLE, counterparty=LEGIT)),
    ("stale / dormant endpoint", "temporal", "block", False,
     dict(amount="0.09", record=GOOD, price_history=STABLE, counterparty=LEGIT,
          temporal_signal={"stale": True, "recency_days": 200})),
    ("over budget (amount > $10)", "amount", "block", False,
     dict(amount="25.00", record=GOOD, price_history=["25", "25", "24"],
          counterparty=LEGIT)),
    ("cold-start unknown", "cold-start", "block", False,
     dict(amount="0.09", record={}, price_history=STABLE, counterparty=LEGIT)),
    # self-reported wash: claims 999 settlements but 0 are chain-CONFIRMED -> the
    # thin gate counts only confirmed, so it can't talk its way out of HOLD.
    ("self-reported wash (0 confirmed)", "thin", "block", False,
     dict(amount="0.09",
          record={"settlement_count": 999, "confirmed_settlement_count": 0,
                  "distinct_payers": 0, "dispute_rate": 0.0},
          price_history=STABLE, counterparty=LEGIT)),
    # cold-start gouge caught by the CATEGORY baseline: reputable, no own price
    # history, but quoting 60x the on-chain median for its service category.
    ("category price gouge (cold-start)", "category", "block", False,
     dict(amount="0.30", record=GOOD, price_history=[], counterparty=LEGIT,
          category="finance", category_median="0.005")),   # 0.30 / 0.005 = 60x

    # --- documented GAPS (attack gets GO) ---
    ("large captive farm (>ceiling)", "sybil-graph", "block", True,
     dict(amount="0.09", record=dict(GOOD, distinct_payers=13), price_history=STABLE,
          counterparty=LEGIT,
          # graph does NOT set captive_sybil above the ceiling -> no gate fires
          payer_graph_signal={"captive_sybil": False, "distinct_payers": 13,
                              "established_payers": 0})),
    ("Sybil ring (advisory only)", "sybil-graph", "block", True,
     dict(amount="0.09", record=GOOD, price_history=STABLE, counterparty=LEGIT,
          payer_graph_signal={"captive_sybil": False, "sybil_ring": True,
                              "distinct_payers": 6, "established_payers": 5,
                              "reputable_payers": 0})),
    ("burst-acquired Sybil (diagnostic)", "temporal", "block", True,
     dict(amount="0.09", record=GOOD, price_history=STABLE, counterparty=LEGIT,
          temporal_signal={"stale": False, "burst_sybil": True, "peak_day_share": 0.95})),

    # --- legit controls: must NOT be blocked ---
    ("established, fair price", "control", "allow", False,
     dict(amount="0.09", record=GOOD, price_history=STABLE, counterparty=LEGIT)),
    ("established w/ ring advisory", "control", "allow", False,
     dict(amount="0.09", record=GOOD, price_history=STABLE, counterparty=LEGIT,
          payer_graph_signal={"captive_sybil": False, "sybil_ring": True,
                              "distinct_payers": 6, "established_payers": 5,
                              "reputable_payers": 0})),
    # premium (15x the category median) is legit, not a gouge -> the category gate
    # (50x) must NOT false-positive on it. Guards the eval-calibrated threshold.
    ("premium price (15x, under category bar)", "control", "allow", False,
     dict(amount="0.30", record=GOOD, price_history=[], counterparty=LEGIT,
          category="finance", category_median="0.02")),   # 0.30 / 0.02 = 15x
]


def _disposition(expect, known_gap, verdict):
    blocked = verdict in ("HOLD", "STOP")
    if expect == "allow":
        return "CLEAN" if not blocked else "FALSE POSITIVE"
    # expect == "block"
    if blocked:
        return "CAUGHT"
    return "KNOWN GAP" if known_gap else "MISS (BUG)"


def run():
    results = []
    for name, cat, expect, known_gap, kw in SCENARIOS:
        v = bw.decide_payment(**kw)
        results.append({"name": name, "category": cat, "expect": expect,
                        "known_gap": known_gap, "verdict": v["verdict"],
                        "disposition": _disposition(expect, known_gap, v["verdict"])})
    return results


def main(argv=None):
    import argparse
    import collections
    import json
    p = argparse.ArgumentParser(description="Adversarial coverage scorecard.")
    p.add_argument("--json", help="write results JSON here")
    args = p.parse_args(argv)
    results = run()
    counts = collections.Counter(r["disposition"] for r in results)
    w = 34
    print("%-34s %-13s %-6s %s" % ("SCENARIO", "CATEGORY", "VERD", "DISPOSITION"))
    print("-" * 72)
    for r in results:
        print("%-34s %-13s %-6s %s" % (r["name"][:w], r["category"], r["verdict"],
                                       r["disposition"]))
    print("-" * 72)
    print("caught=%d  known-gap=%d  clean=%d  false-positive=%d  miss=%d"
          % (counts["CAUGHT"], counts["KNOWN GAP"], counts["CLEAN"],
             counts["FALSE POSITIVE"], counts["MISS (BUG)"]))
    if args.json:
        json.dump(results, open(args.json, "w"), indent=2)
    return 1 if (counts["FALSE POSITIVE"] or counts["MISS (BUG)"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
