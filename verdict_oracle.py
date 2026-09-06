#!/usr/bin/env python3
"""
verdict_oracle.py -- a frozen behavioral snapshot of decide_payment.

Purpose: prove that a change did NOT alter how the verdict works. This is the
Stage-0 regression instrument for the data-completeness / sybil_ring-gating work
(see docs/DATA_COMPLETENESS.md). It exercises a broad, deterministic grid of
synthetic inputs across EVERY gate -- reputation, thin-history, Sybil, price
anomaly/gouge, budget, category, divergence, payload/recipient mismatch,
sanctions, known-bad, payer-graph, temporal, enrichment, verified-floor -- and
records the decision-critical outputs for each.

Unlike redteam.py (a curated attack list) and fuzz_verdict.py (property
invariants), this catches ANY unintended verdict drift the curated cases don't
anticipate: it snapshots the WHOLE grid and diffs byte-for-byte.

Design constraints (what makes it a trustworthy oracle):
  * PURE + DB-FREE + deterministic -- no network, clock, randomness, or store.
    So it is invariant to DATA changes (widening ingestion) and only moves when
    the DECISION LOGIC moves. That is exactly the property we need to separate
    "the data changed" from "the rule changed".
  * Captures verdict / hard_stop / score / blast_radius / reason-count -- the
    decision-critical surface. Reason PROSE is deliberately excluded (wording
    drift is caught by unit tests; here it would be noise).

Workflow:
  * `python verdict_oracle.py --write`  regenerates data/verdict_oracle.json.
  * test_verdict_oracle.py re-runs snapshot() and diffs against that golden.
  * An INTENTIONAL behavior change (e.g. promoting sybil_ring to a gate) is
    shipped by regenerating the golden in the SAME commit -- the golden diff is
    then the human-reviewable record of exactly which verdicts changed.
"""
from __future__ import annotations

import blackwall as bw

# canonical addresses -- fixed so the snapshot is stable.
CP = "0x" + "1" * 40          # the counterparty being paid
OTHER = "0x" + "2" * 40       # a different address (recipient-mismatch cases)

# --- record profiles: the reputation-store shape for the counterparty ---
REC_GOOD = {"settlement_count": 500, "dispute_rate": 0.0, "distinct_payers": 30}
REC_THIN = {"settlement_count": 5, "dispute_rate": 0.0, "distinct_payers": 3}
REC_COLD = {}
REC_FEW_PAYERS = {"settlement_count": 500, "dispute_rate": 0.0, "distinct_payers": 2}
REC_GOING_BAD = {"settlement_count": 500, "dispute_rate": 0.0, "distinct_payers": 30,
                 "recent_dispute_rate": 0.5, "recent_outcomes": 10}
REC_SANCTIONED = {"settlement_count": 500, "dispute_rate": 0.0, "distinct_payers": 30,
                  "sanctioned": True}
REC_KNOWN_BAD = {"settlement_count": 500, "dispute_rate": 0.0, "distinct_payers": 30,
                 "known_bad": True}
REC_WASH = {"settlement_count": 999, "confirmed_settlement_count": 0,
            "distinct_payers": 0, "dispute_rate": 0.0}

RECORDS = [
    ("good", REC_GOOD),
    ("thin", REC_THIN),
    ("cold", REC_COLD),
    ("few_payers", REC_FEW_PAYERS),
    ("going_bad", REC_GOING_BAD),
    ("sanctioned", REC_SANCTIONED),
    ("known_bad", REC_KNOWN_BAD),
    ("wash", REC_WASH),
]

# --- price context: (amount, price_history) ---
STABLE = ["0.09", "0.09", "0.088", "0.092"]     # median ~0.09
PRICES = [
    ("fair", "0.09", STABLE),
    ("empty_hist", "0.09", []),
    ("moderate_anomaly", "0.30", STABLE),        # ~3.3x
    ("gouge", "0.72", STABLE),                   # 8x -> STOP-anomaly
    ("over_budget", "25.00", ["25", "25", "24"]),
]

# --- single-signal overlays: each adds one extra gate's kwargs on top of a base ---
# (name, extra kwargs). None-valued entries mean "no overlay".
OVERLAYS = [
    ("none", {}),
    ("recipient_mismatch", {"expected_recipient": OTHER}),
    ("payload_mismatch", {"payload_mismatch_reasons": ["recipient field != signed"]}),
    ("captive_sybil", {"payer_graph_signal": {"captive_sybil": True, "distinct_payers": 4}}),
    ("sybil_ring_advisory", {"payer_graph_signal": {
        "captive_sybil": False, "sybil_ring": True, "distinct_payers": 6,
        "established_payers": 5, "reputable_payers": 0}}),
    ("stale", {"temporal_signal": {"stale": True, "recency_days": 200}}),
    ("burst_advisory", {"temporal_signal": {
        "stale": False, "burst_sybil": True, "peak_day_share": 0.95}}),
    ("category_gouge", {"category": "finance", "category_median": "0.005"}),  # vs 0.30 -> 60x
    ("category_premium", {"category": "finance", "category_median": "0.02"}),  # vs 0.30 -> 15x
    ("divergence", {"divergence_ratio": "12.0"}),
    ("enrichment_review", {"enrichment": {"review": True, "is_scam": True,
                                          "reasons": ["scam-associated"]}}),
    ("enrichment_clean", {"enrichment": {"review": False}}),
    ("verified_floor", {"verified_floor": 0.6, "verified_grade": "B"}),
    # A non-dollar amount, which the DOLLAR auto-approve threshold cannot judge.
    # Added because the gate shipped without an overlay here: this grid is the
    # frozen tripwire for `decide_payment`, and a new verdict-affecting branch
    # that it does not exercise is a branch it cannot protect. It is also the
    # only overlay that moves `blast_radius` off bounded/unbounded.
    ("non_usd_amount", {"non_usd_amount": True}),
    ("secret_high", {"secret_findings": [{"type": "aws_access_key_id",
                                          "severity": "high", "field": "memo",
                                          "hint": "AKIA***"}]}),   # -> STOP
    ("secret_medium", {"secret_findings": [{"type": "ssn", "severity": "medium",
                                            "field": "note", "hint": "1***9"}]}),  # -> HOLD
]


def _scenarios():
    """Deterministic grid. Yields (id, kwargs). Bounded & interpretable: every
    (record x price) base crossed with each single-signal overlay."""
    for rname, rec in RECORDS:
        for pname, amount, hist in PRICES:
            for oname, extra in OVERLAYS:
                kw = dict(amount=amount, record=dict(rec), price_history=list(hist),
                          counterparty=CP)
                kw.update(extra)
                sid = "%s|%s|%s" % (rname, pname, oname)
                yield sid, kw


def snapshot():
    """Run the whole grid and return a list of decision-critical output rows.
    Deterministic ordering (sorted by id)."""
    rows = []
    for sid, kw in _scenarios():
        v = bw.decide_payment(**kw)
        rows.append({
            "id": sid,
            "verdict": v["verdict"],
            "hard_stop": v["hard_stop"],
            "score": round(float(v["score"]), 4),
            "blast_radius": v["signals"].get("blast_radius"),
            "num_reasons": len(v.get("reasons", [])),
        })
    rows.sort(key=lambda r: r["id"])
    return rows


GOLDEN_PATH = "data/verdict_oracle.json"


def main(argv=None):
    import argparse
    import json
    import os
    p = argparse.ArgumentParser(description="Frozen behavioral snapshot of decide_payment.")
    p.add_argument("--write", action="store_true",
                   help="(re)write the golden file (INTENTIONAL behavior changes only)")
    p.add_argument("--path", default=GOLDEN_PATH)
    args = p.parse_args(argv)
    rows = snapshot()
    if args.write:
        os.makedirs(os.path.dirname(args.path) or ".", exist_ok=True)
        with open(args.path, "w") as f:
            json.dump(rows, f, indent=2, sort_keys=True)
            f.write("\n")
        counts = {}
        for r in rows:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        print("wrote %d rows -> %s  (%s)" % (len(rows), args.path, counts))
    else:
        print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
