#!/usr/bin/env python3
"""
coverage_eval.py -- Stage-1 instrument: does more ingestion coverage drive the
advisory `sybil_ring` false-flag rate on KNOWN-GOOD payees toward zero?

This is the GO/NO-GO measurement for promoting sybil_ring to a gate
(docs/DATA_COMPLETENESS.md, Stage 3). It changes NO verdict logic -- it reads the
reputation corpus and reports a convergence curve.

Why this specific experiment
----------------------------
`sybil_ring` fires when a payee has 3..RING_MAX_DISTINCT distinct payers and NOT ONE
is "reputable" (pays >= 2 trusted anchors). It over-flags at partial coverage because
a payer's anchor-paying is UNDER-OBSERVED until the anchors it pays are themselves
ingested. The real backfill ingests per TARGET PAYEE, so coverage grows payee-by-payee.

So we model coverage faithfully: at fraction f we have ingested a deterministic f-subset
of payees (ALL their inbound edges). We rebuild the graph on that subset, recompute
anchors and payer reputation WITHIN it, and ask: of the payees that are genuinely NOT
rings at FULL coverage (our best available ground truth -- the full data vouches for
them with >= 1 reputable payer), how many spuriously flag as `sybil_ring` at coverage f?

That ratio is the false-flag rate. If it -> 0 as f -> 1 AND the tail is flat (the last
slice of coverage stops clearing rings), the signal has stabilized and gating is
reachable. If the tail is still moving, the signal is coverage-sensitive and must stay
advisory -- exactly the honest stop condition.

Deterministic: payee inclusion is by a stable hash (sha256), so subsets are NESTED and
monotone as f grows (ingesting more never un-ingests). No clock, no randomness.
"""
from __future__ import annotations

import hashlib

import payer_graph as PG
import payer_reputation as PR


def _payee_rank(payee):
    """Stable [0,1) rank for a payee -- its position in the simulated discovery order.
    Deterministic (sha256), so `rank < f` gives nested, monotone coverage subsets."""
    h = hashlib.sha256(PG._norm(payee).encode()).hexdigest()
    return int(h[:16], 16) / float(1 << 64)


def _ring_set(edges, *, min_distinct=PG.MIN_DISTINCT_PAYERS,
              ring_max=PR.RING_MAX_DISTINCT):
    """Payees flagged `sybil_ring` on this exact edge set, plus the ring-band payees
    present (3..ring_max distinct payers -- the only ones that CAN flag). Returns
    (ring_flagged:set, ring_band:set)."""
    graph = PG.build_index(edges)
    payer_rep, _anchors = PR.payer_scores(graph)
    flagged, band = set(), set()
    for payee, payers in graph["payee_to_payers"].items():
        if not (min_distinct <= len(payers) <= ring_max):
            continue
        band.add(payee)
        corr = PR.payee_corroboration(graph, payer_rep, payee,
                                      min_distinct=min_distinct,
                                      ring_max_distinct=ring_max)
        if corr and corr["sybil_ring"]:
            flagged.add(payee)
    return flagged, band


def known_good_payees(edges):
    """Ground-truth set for the false-flag measurement: ring-band payees that are NOT
    a ring at FULL coverage (the full corpus vouches for them). These are precisely the
    payees a coverage-sensitive signal can wrongly flag while ingestion is incomplete."""
    flagged_full, band_full = _ring_set(edges)
    return band_full - flagged_full


def _subset_edges(edges, f, rank_fn=_payee_rank):
    """Edges whose PAYEE is within the f-fraction of the simulated discovery order.
    `rank_fn(payee) -> [0,1)` is injectable so tests can fix the discovery order."""
    return [(p, q) for (p, q) in edges if rank_fn(q) < f]


def coverage_curve(edges, fractions=(0.2, 0.4, 0.6, 0.8, 0.9, 0.95, 1.0),
                   rank_fn=_payee_rank):
    """The convergence curve. For each coverage fraction f, of the KNOWN-GOOD payees
    that are INGESTED at f, how many spuriously flag `sybil_ring`?"""
    good = known_good_payees(edges)
    rows = []
    for f in fractions:
        sub = _subset_edges(edges, f, rank_fn)
        flagged, _band = _ring_set(sub)
        graph = PG.build_index(sub)
        ingested_good = good & set(graph["payee_to_payers"])
        false_flagged = flagged & ingested_good
        n = len(ingested_good)
        rows.append({
            "fraction": round(f, 3),
            "ingested_known_good": n,
            "false_flagged": len(false_flagged),
            "false_flag_rate": round(len(false_flagged) / n, 4) if n else 0.0,
        })
    return rows


def convergence_verdict(curve, *, tail_rate_max=0.02, tail_delta_max=0.02):
    """Is the signal stable enough to gate? Returns the call + the numbers behind it --
    the auditable Stage-1 -> Stage-3 gate.

    HONESTY NOTE: `known_good` is defined at FULL coverage, so the f=1.0 false-flag rate
    is DEFINITIONALLY 0 and proves nothing. The real evidence is the SUB-FULL tail: if
    the false-flag rate is already ~0 at the highest f < 1.0 and barely moved over the
    prior step, then the last slice of coverage isn't clearing rings -- the signal has
    stabilized BEFORE full coverage. That is a genuine (non-circular) convergence."""
    sub = [r for r in curve if r["fraction"] < 1.0]
    if len(sub) < 2:
        return {"gating_reachable": False, "reason": "insufficient sub-full curve"}
    last, prev = sub[-1], sub[-2]                       # highest two f < 1.0
    final_rate = last["false_flag_rate"]
    tail_delta = abs(prev["false_flag_rate"] - final_rate)
    reachable = final_rate <= tail_rate_max and tail_delta <= tail_delta_max
    return {
        "gating_reachable": reachable,
        "evidence_fraction": last["fraction"],          # the highest sub-full f used
        "subfull_false_flag_rate": final_rate,
        "tail_delta": round(tail_delta, 4),
        "tail_rate_max": tail_rate_max,
        "tail_delta_max": tail_delta_max,
        "reason": ("stabilized before full coverage: rate at f=%.2f is %.2f%% (<= %.0f%%) "
                   "and the prior step moved it only %.2f%% -- the last slice of coverage "
                   "no longer clears rings"
                   % (last["fraction"], final_rate * 100, tail_rate_max * 100,
                      tail_delta * 100)
                   if reachable else
                   "coverage-sensitive: keep sybil_ring ADVISORY (rate at f=%.2f is "
                   "%.2f%%, tail_delta=%.2f%%)"
                   % (last["fraction"], final_rate * 100, tail_delta * 100)),
    }


def _load_seed_edges(path="data/reputation_seed.db.gz"):
    import gzip
    import os
    import shutil
    import tempfile
    import reputation_store as RS
    if path.endswith(".gz"):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        with gzip.open(path, "rb") as f, open(tmp, "wb") as o:
            shutil.copyfileobj(f, o)
        try:
            return list(RS.ReputationStore(tmp).iter_settlement_edges())
        finally:
            os.unlink(tmp)
    return list(RS.ReputationStore(path).iter_settlement_edges())


def main(argv=None):
    import argparse
    import json
    p = argparse.ArgumentParser(description="sybil_ring coverage-convergence eval.")
    p.add_argument("--seed", default="data/reputation_seed.db.gz")
    p.add_argument("--json", help="write curve+verdict JSON here")
    args = p.parse_args(argv)
    edges = _load_seed_edges(args.seed)
    curve = coverage_curve(edges)
    verdict = convergence_verdict(curve)
    print("coverage  ingested_known_good  false_flagged  false_flag_rate")
    print("-" * 60)
    for r in curve:
        print("  %-7.0f%% %-20d %-14d %.2f%%"
              % (r["fraction"] * 100, r["ingested_known_good"],
                 r["false_flagged"], r["false_flag_rate"] * 100))
    print("-" * 60)
    print("gating reachable:", verdict["gating_reachable"], "--", verdict["reason"])
    if args.json:
        json.dump({"curve": curve, "verdict": verdict}, open(args.json, "w"), indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
