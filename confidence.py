#!/usr/bin/env python3
"""
confidence.py -- how much do we actually KNOW behind a verdict?

The verdict engine fuses many signals (reputation, price, sanctions, payer graph,
temporal, outcomes). GO/HOLD/STOP says what to do; `counterparty_reputation` says how
trustworthy. Neither says how much EVIDENCE backs the call -- a GO on 146 settlements
+ graph corroboration + a live dispute signal is worlds apart from a GO on a
cold-start default. This module makes that legible.

`assess_confidence(record, signals)` is PURE and DESCRIPTIVE -- it NEVER changes the
verdict (a low-confidence GO is still GO; the gates already decided on the evidence
that existed). It returns:
  {level: high|medium|low, score: 0..1, backed_by: [...], missing: [...]}
so a caller can tell "trust this" from "thin -- confirm out of band", and see exactly
which evidence is present vs absent.

Five orthogonal evidence dimensions, each with a fixed weight (sum = 1.0):
  history depth (0.30) · payer breadth (0.20) · cross-counterparty corroboration
  (0.20) · outcome/dispute depth (0.20) · freshness (0.10).
"""
from __future__ import annotations

THIN_HISTORY_SETTLEMENTS = 20     # mirror blackwall's thin-history gate
MIN_DISTINCT_PAYERS = 3           # mirror blackwall's Sybil gate

HIGH = 0.70
MEDIUM = 0.40


def assess_confidence(record, signals):
    """Evidence-backing assessment for a verdict. Pure; never alters the verdict."""
    record = record or {}
    signals = signals or {}
    backed, missing = [], []
    score = 0.0

    # 1. history depth (chain-confirmed settlements) -- 0.30
    confirmed = record.get("confirmed_settlement_count")
    if confirmed is None:
        confirmed = record.get("settlement_count") or 0
    if confirmed >= THIN_HISTORY_SETTLEMENTS:
        score += 0.30
        backed.append("%d confirmed settlements" % confirmed)
    elif confirmed > 0:
        score += 0.10
        missing.append("thin history (%d < %d settlements)"
                       % (confirmed, THIN_HISTORY_SETTLEMENTS))
    else:
        missing.append("no on-chain history (cold start)")

    # 2. payer breadth (distinct funders) -- 0.20
    distinct = record.get("distinct_payers")
    if distinct is not None and distinct >= MIN_DISTINCT_PAYERS:
        score += 0.20
        backed.append("%d distinct payers" % distinct)
    elif distinct:
        score += 0.05
        missing.append("few distinct payers (%d)" % distinct)
    else:
        missing.append("no distinct-payer data")

    # 3. cross-counterparty corroboration (payer graph / reputation) -- 0.20
    cc = signals.get("cross_counterparty") or {}
    reputable = cc.get("reputable_payers")
    established = cc.get("established_payers")
    if reputable:
        score += 0.20
        backed.append("graph-corroborated (%d reputable payers)" % reputable)
    elif established:
        score += 0.10
        backed.append("graph-corroborated (%d established payers)" % established)
    else:
        missing.append("no cross-counterparty corroboration")

    # 4. outcome / dispute depth -- 0.20. The only signal from what payments DID.
    has_outcome = bool(record.get("recent_outcomes")) \
        or record.get("dispute_rate") is not None
    if has_outcome:
        score += 0.20
        backed.append("outcome data (dispute signal live)")
    else:
        missing.append("no outcome/dispute data (breadth only)")

    # 5. freshness -- 0.10
    temporal = signals.get("temporal") or {}
    if temporal.get("stale"):
        missing.append("stale (no recent settlement)")
    elif temporal.get("recency_days") is not None:
        score += 0.10
        backed.append("fresh (last settled %.0fd ago)" % temporal["recency_days"])
    else:
        missing.append("no settlement timeline")

    score = round(score, 3)
    level = "high" if score >= HIGH else "medium" if score >= MEDIUM else "low"
    return {"level": level, "score": score, "backed_by": backed, "missing": missing}
