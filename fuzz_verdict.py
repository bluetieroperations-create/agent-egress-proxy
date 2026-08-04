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
        # free on-chain enrichment (blockscout): REVIEW-only. The boundary is fuzzed by
        # P2b/P8 -- it must never set hard_stop or clear a STOP, only push GO->HOLD.
        "enrichment": _maybe(rng, lambda: {"review": _pick(rng, True, False),
                                           "is_scam": _pick(rng, True, False),
                                           "reasons": ["scam tag"]}, 0.4),
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
    else:
        # P2b (converse / the enrichment BOUNDARY): only a real blockable condition may
        # set hard_stop. A REVIEW-only signal (enrichment, category, divergence, ...)
        # must NEVER reach the hard-stop path.
        if hard is True:
            v.append("P2b non-blockable input but hard_stop=True (a soft signal STOPped?)")
    # P8: a REVIEW-only enrichment signal can push GO->HOLD but must never yield GO.
    if (kw.get("enrichment") or {}).get("review") and verdict == "GO":
        v.append("P8 enrichment.review set but verdict==GO (review ignored)")
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


# ---------------------------------------------------------------------------
# forecast() end-to-end fuzz -- covers validation, source lookup, payload-sim /
# calldata integration, and source->signal wiring that decide_payment fuzzing misses.
# ---------------------------------------------------------------------------
class _SingleSource:
    """reputation_source returning a fixed record for any counterparty."""
    def __init__(self, record):
        self._r = record

    def lookup(self, cp):
        return dict(self._r)


class _MockGraph:
    def __init__(self, rng, raises=False):
        self._rng, self._raises = rng, raises

    def cross_signal(self, cp):
        if self._raises:
            raise RuntimeError("graph source down")   # fail-open contract: must not crash forecast
        return random_graph_signal(self._rng)


class _MockVelocity:
    def __init__(self, rng, raises=False):
        self._rng, self._raises = rng, raises

    def temporal(self, cp):
        if self._raises:
            raise RuntimeError("velocity source down")
        return random_temporal_signal(self._rng)


def random_payload(rng):
    """A /v1/forecast-payment request body -- valid, edge, and ~15% malformed (to
    exercise the validation error paths)."""
    cp = rng.choice(_ADDRS)
    p = {"counterparty": cp,
         "amount": _pick(rng, "0.001", "0.09", "0.5", "1.00", "5.00", "10.00", "25.00", "100.0"),
         "asset": "USDC", "chain": "base"}
    if rng.random() < 0.5:
        p["resource"] = _pick(rng, "https://x/price/btc", "https://x/chat", "r")
    if rng.random() < 0.3:
        p["resource_class"] = _pick(rng, "weather", "data", "x")
    if rng.random() < 0.4:   # context.expected_recipient: same (ok), different (mismatch->STOP)
        p["context"] = {"expected_recipient": _pick(rng, cp, rng.choice(_ADDRS))}
    if rng.random() < 0.2:
        p["payer"] = rng.choice(_ADDRS)
    if rng.random() < 0.25:  # garbage signed-payment inputs -> payload-sim/calldata must fail-open
        p["payment_authorization"] = _pick(rng, "not-b64!!", "AAAA", "", "eyJ4Ijp9")
    if rng.random() < 0.25:
        p["transaction"] = _pick(rng, {"to": rng.choice(_ADDRS), "data": "0x", "value": "0"},
                                 {"to": "0x", "data": "0xdeadbeef"}, {}, {"data": "0x" + "f" * 200})
    r = rng.random()          # malformed variants
    if r < 0.05:
        p.pop("amount", None)                                   # missing required field
    elif r < 0.09:
        p["amount"] = _pick(rng, "notnum", "0", "-1", "")       # bad/non-positive amount
    elif r < 0.12:
        p["asset"] = _pick(rng, None, 123, "")                  # bad type / empty
    elif r < 0.14:
        p["context"] = "notadict"                              # bad context type
    return p


def forecast_violations(payload, record, resp, err):
    """Invariants for one forecast() result. F1 exactly one of (resp, err) is set; on
    success the P1/P3/P4/P5 safety invariants + the blockable=>STOP guarantee + the
    response schema hold. PURE."""
    v = []
    if (resp is None) == (err is None):
        v.append("F1 exactly one of (response, error) must be set: resp=%s err=%r"
                 % ("None" if resp is None else "set", err))
    if err is not None:
        if not (isinstance(err, str) and err):
            v.append("F1 error must be a non-empty string: %r" % err)
        return v
    if resp is None:
        return v
    verdict, hard, score = resp.get("verdict"), resp.get("hard_stop"), resp.get("score")
    if verdict not in ("GO", "HOLD", "STOP"):
        v.append("P1 verdict not in set: %r" % verdict)
    try:
        if not (0.0 <= float(score) <= 1.0):
            v.append("P5 score out of range: %r" % score)
    except (TypeError, ValueError):
        v.append("P5 score not numeric: %r" % score)
    if hard is True and verdict != "STOP":
        v.append("P3 hard_stop but verdict=%r" % verdict)
    if hard is True and score != 0.0:
        v.append("P4 hard_stop but score=%r" % score)
    cp = payload.get("counterparty") or ""
    ctx = payload.get("context")
    exp = ctx.get("expected_recipient") if isinstance(ctx, dict) else None
    mism = isinstance(exp, str) and exp and not bw.addresses_equal(cp, exp)
    blockable = bool(record.get("sanctioned")) or bool(record.get("known_bad")) or mism
    if blockable and verdict != "STOP":
        v.append("F3 blockable input but verdict=%r (must STOP)" % verdict)
    if blockable and hard is not True:
        v.append("F3 blockable input but hard_stop=%r" % hard)
    if verdict == "GO" and blockable:
        v.append("F3 GO on a blockable input!")
    for k in ("verdict", "score", "signals", "reasons", "receipt_id"):
        if k not in resp:
            v.append("F4 success response missing key: %s" % k)
    return v


def run_forecast(iterations=5000, seed=1337):
    """Fuzz forecast() end-to-end. A raise is a P7 violation (fail-open contract --
    even a raising graph/velocity source or garbage signed-payment must not crash it).

    SCOPE: the reputation_source here never raises. A raising CORE source (locked
    SQLite) IS allowed to propagate from forecast() -- fail-open there could drop the
    sanctions flag -- so the SERVICE handles it at the HTTP layer instead: the handler
    catches it and returns 503 (fail CLOSED, never a GO). See test_blackwall
    TestFailClosedOnCrash."""
    rng = random.Random(seed)
    violations = []
    for _ in range(iterations):
        payload = random_payload(rng)
        record = random_record(rng)
        graph = (_MockGraph(rng, raises=(rng.random() < 0.1))
                 if rng.random() < 0.6 else None)
        vel = (_MockVelocity(rng, raises=(rng.random() < 0.1))
               if rng.random() < 0.6 else None)
        cat = ({_pick(rng, "finance", "ai-agents"): _pick(rng, "0.001", "0.005")}
               if rng.random() < 0.3 else None)
        div = ({rng.choice(_ADDRS).lower(): _pick(rng, "1.0", "12.0", "90.0")}
               if rng.random() < 0.3 else None)
        try:
            resp, err = bw.forecast(payload, _SingleSource(record), None,
                                    graph_source=graph, velocity_source=vel,
                                    category_index=cat, divergence_index=div)
        except Exception as e:
            violations.append({"case": payload, "result": None,
                               "problems": ["P7 forecast raised %s: %s"
                                            % (type(e).__name__, e)]})
            continue
        probs = forecast_violations(payload, record, resp, err)
        if probs:
            violations.append({"case": payload, "result": {"resp": resp, "err": err},
                               "problems": probs})
    return {"seed": seed, "iterations": iterations, "violations": violations}


def main(argv=None):
    import argparse
    import json
    p = argparse.ArgumentParser(description="Property-based fuzzer for the verdict engine.")
    p.add_argument("-n", "--iterations", type=int, default=20000)
    p.add_argument("--seed", type=int, default=1337)
    p.add_argument("--target", choices=("decide", "forecast", "both"), default="both")
    p.add_argument("--json", help="write the full report JSON here")
    args = p.parse_args(argv)
    reps = {}
    if args.target in ("decide", "both"):
        reps["decide_payment"] = run(args.iterations, args.seed)
    if args.target in ("forecast", "both"):
        reps["forecast"] = run_forecast(args.iterations, args.seed)
    total = 0
    for name, rep in reps.items():
        n = len(rep["violations"])
        total += n
        print("fuzz %s: %d cases, seed %d -> %d violation(s)"
              % (name, rep["iterations"], rep["seed"], n))
        for x in rep["violations"][:10]:
            print("  PROBLEMS:", x["problems"])
            print("    case:", json.dumps(x["case"], default=str)[:300])
    if args.json:
        json.dump(reps, open(args.json, "w"), default=str, indent=2)
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
