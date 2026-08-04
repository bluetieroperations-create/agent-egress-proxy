#!/usr/bin/env python3
"""
fuzz_verdict.py -- property-based fuzzer for blackwall.decide_payment.

The verdict now folds ~10 signals together (reputation, thin, Sybil, payer-graph,
temporal, going-bad, price own/peer/category/divergence, payload-sim, sanctions). The
unit tests cover the cases we THOUGHT of; this throws thousands of random-but-realistic
signal combinations at the engine and asserts the SAFETY INVARIANTS never break:

  P1  verdict is always one of GO / HOLD / STOP
  P2  a blockable input (sanctioned OR known_bad OR payload-mismatch OR recipient
      mismatch) is ALWAYS verdict==STOP AND hard_stop==True -- even with a
      verified_floor / any GO-leaning signal set (seller audit must never buy past a STOP)
  P3  hard_stop==True  =>  verdict==STOP
  P4  hard_stop==True  =>  score==0.0
  P5  0.0 <= score <= 1.0
  P6  verdict==GO      =>  NOT any blockable condition
  P7  decide_payment never raises on a well-typed input (fail-open contract)

Inputs are realistically TYPED (bools for flags, numbers/None for numerics) but
adversarially VALUED/combined -- the target is logic/interaction bugs, not type abuse.
Deterministic: a fixed seed reproduces the exact case, printed on any violation.
"""
from __future__ import annotations

import random

import blackwall as bw

_ADDRS = ["0x" + c * 40 for c in "123abc"]


def _pick(rng, *vals):
    return rng.choice(vals)


def _maybe(rng, gen, p=0.5):
    return gen() if rng.random() < p else None


def random_record(rng):
    rec = {}
    if rng.random() < 0.92:
        rec["settlement_count"] = _pick(rng, 0, 1, 5, 19, 20, 50, 100, 500, 1000)
    if rng.random() < 0.6:
        rec["confirmed_settlement_count"] = _pick(rng, 0, 1, 5, 19, 20, 50, 500)
    if rng.random() < 0.85:
        rec["distinct_payers"] = _pick(rng, 0, 1, 2, 3, 5, 10, 30, 100)
    if rng.random() < 0.85:
        rec["dispute_rate"] = _pick(rng, 0.0, 0.001, 0.01, 0.1, 0.3, 0.5, 1.0)
    # the rare, DANGEROUS flags -- weighted low so they mix with everything else
    if rng.random() < 0.25:
        rec["sanctioned"] = _pick(rng, True, False)
    if rng.random() < 0.25:
        rec["known_bad"] = _pick(rng, True, False)
    if rng.random() < 0.35:
        rec["recent_dispute_rate"] = _pick(rng, None, 0.0, 0.1, 0.3, 0.5, 1.0)
    if rng.random() < 0.35:
        rec["recent_outcomes"] = _pick(rng, 0, 1, 4, 10)
    return rec


def random_graph_signal(rng):
    return {
        "captive_sybil": _pick(rng, True, False),
        "sybil_ring": _pick(rng, True, False),
        "distinct_payers": _pick(rng, 0, 1, 3, 6, 13, 100),
        "established_payers": _pick(rng, 0, 1, 5),
        "reputable_payers": _pick(rng, 0, 1, 5),
    }


def random_temporal_signal(rng):
    return {
        "stale": _pick(rng, True, False),
        "recency_days": _pick(rng, None, 0, 30, 100, 200),
        "burst_sybil": _pick(rng, True, False),
        "peak_day_share": _pick(rng, None, 0.0, 0.5, 0.95, 1.0),
    }


def random_case(rng):
    """A realistically-typed but adversarially-valued decide_payment kwargs dict."""
    cp = rng.choice(_ADDRS)
    kw = {
        "amount": _pick(rng, "0", "0.0001", "0.001", "0.09", "0.5", "1.00",
                        "5.00", "10.00", "25.00", "100.0"),
        "record": random_record(rng),
        "price_history": [_pick(rng, "0.09", "0.088", "0.1", "1.0")
                          for _ in range(rng.randint(0, 5))],
        "counterparty": cp,
        # expected_recipient: often same (no mismatch), sometimes different, sometimes None
        "expected_recipient": _pick(rng, cp, rng.choice(_ADDRS), None),
        "hold_above": _maybe(rng, lambda: _pick(rng, "1.00", "10.00", "100000"), 0.5),
        "resource": _maybe(rng, lambda: _pick(rng, "https://x/price/btc",
                                              "https://x/chat", "r"), 0.5),
        "peer_median": _maybe(rng, lambda: _pick(rng, "0.001", "0.09", "100"), 0.4),
        "payload_mismatch_reasons": (["signed payment pays the wrong party"]
                                     if rng.random() < 0.15 else []),
        "verified_floor": _maybe(rng, lambda: _pick(rng, 0.5, 0.7, 0.9), 0.3),
        "verified_grade": _maybe(rng, lambda: _pick(rng, "A", "B"), 0.3),
        "payer_graph_signal": _maybe(rng, lambda: random_graph_signal(rng), 0.5),
        "temporal_signal": _maybe(rng, lambda: random_temporal_signal(rng), 0.5),
        "category": _maybe(rng, lambda: _pick(rng, "finance", "ai-agents"), 0.4),
        "category_median": _maybe(rng, lambda: _pick(rng, "0.001", "0.005"), 0.4),
        "divergence_ratio": _maybe(rng, lambda: _pick(rng, "1.0", "5.0", "12.0", "90.0"), 0.3),
    }
    return kw


def _blockable(kw):
    rec = kw.get("record") or {}
    cp, exp = kw.get("counterparty"), kw.get("expected_recipient")
    mismatch = (exp is not None and cp is not None
                and not bw.addresses_equal(cp, exp))
    return (bool(rec.get("sanctioned")) or bool(rec.get("known_bad"))
            or bool(kw.get("payload_mismatch_reasons")) or mismatch)


def invariant_violations(kw, result):
    """Return a list of invariant-violation strings for one (input, result). Empty ==
    all invariants held. PURE."""
    v = []
    verdict = result.get("verdict")
    hard = result.get("hard_stop")
    score = result.get("score")
    if verdict not in ("GO", "HOLD", "STOP"):
        v.append("P1 verdict not in set: %r" % verdict)
    if _blockable(kw):
        if verdict != "STOP":
            v.append("P2 blockable input but verdict=%r (must STOP)" % verdict)
        if hard is not True:
            v.append("P2 blockable input but hard_stop=%r (must be True)" % hard)
    if hard is True and verdict != "STOP":
        v.append("P3 hard_stop but verdict=%r" % verdict)
    if hard is True and score != 0.0:
        v.append("P4 hard_stop but score=%r (must be 0.0)" % score)
    try:
        if not (0.0 <= float(score) <= 1.0):
            v.append("P5 score out of range: %r" % score)
    except (TypeError, ValueError):
        v.append("P5 score not numeric: %r" % score)
    if verdict == "GO" and _blockable(kw):
        v.append("P6 GO on a blockable input!")
    return v


def run(iterations=5000, seed=1337):
    """Fuzz `iterations` cases; return {seed, iterations, violations:[{case,result,
    problems}]}. A raise inside decide_payment is a P7 violation (fail-open contract)."""
    rng = random.Random(seed)
    violations = []
    for _ in range(iterations):
        kw = random_case(rng)
        try:
            result = bw.decide_payment(**kw)
        except Exception as e:  # P7: must not raise on a well-typed input
            violations.append({"case": kw, "result": None,
                               "problems": ["P7 raised %s: %s" % (type(e).__name__, e)]})
            continue
        probs = invariant_violations(kw, result)
        if probs:
            violations.append({"case": kw, "result": result, "problems": probs})
    return {"seed": seed, "iterations": iterations, "violations": violations}


def main(argv=None):
    import argparse
    import json
    p = argparse.ArgumentParser(description="Property-based fuzzer for decide_payment.")
    p.add_argument("-n", "--iterations", type=int, default=20000)
    p.add_argument("--seed", type=int, default=1337)
    p.add_argument("--json", help="write the full report JSON here")
    args = p.parse_args(argv)
    rep = run(args.iterations, args.seed)
    n = len(rep["violations"])
    print("fuzz_verdict: %d cases, seed %d -> %d violation(s)"
          % (rep["iterations"], rep["seed"], n))
    for x in rep["violations"][:10]:
        print("  PROBLEMS:", x["problems"])
        print("    case:", json.dumps(x["case"], default=str))
        print("    result:", json.dumps(x["result"], default=str)[:200])
    if args.json:
        json.dump(rep, open(args.json, "w"), default=str, indent=2)
    return 1 if n else 0


if __name__ == "__main__":
    raise SystemExit(main())
