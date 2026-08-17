#!/usr/bin/env python3
"""
issuer_trust_gate.py -- fold the EARNED per-issuer trust grade into the RWA verdict.

The payoff of the accumulation arc: `rwa_ledger.issuer_trust` grades an issuer from its
LABELED outcomes (settlement reliability + underwater rate, hard-to-fake volume). This
module surfaces that grade in every RWA verdict and -- once calibrated -- lets a LOW-grade
issuer add caution.

GRADUATION DISCIPLINE (mirrors SYBIL_RING_GATES / calibration_lock.py):
  * `ISSUER_TRUST_GATES` is a reversibility LOCK, default False. While False the grade is
    DESCRIPTIVE ONLY -- recorded under signals.issuer_trust, never affecting the verdict.
    This is the observation mode: run the backfill, watch grades stabilize on known-good
    issuers (Backed/Ondo), prove ~0 false-flags, THEN flip the lock.
  * When True, a LOW issuer grade becomes an ADVISORY signal (weighed COLLECTIVELY by
    rwa_aggregate -- it never gates alone). HOLD-only, never STOP, never clears a verdict.
    (The reverse -- a HIGH grade granting an earned FLOOR that clears a cold-start HOLD,
    the seller_audit pattern -- is deferred; clearing needs more calibration than adding.)

Grades are precomputed from the corpus ONCE (startup/refresh) into a {issuer: grade} map,
so the hot path is an O(1) lookup, not a full-corpus aggregation per verdict. PURE fold +
a cached source. Stdlib.
"""
from __future__ import annotations

from rwa_ledger import issuer_trust

# Reversibility lock. Flip True ONLY after the backfilled corpus is calibrated and the
# false-flag rate on known-good issuers is proven ~0. Off = descriptive (safe default).
ISSUER_TRUST_GATES = False

_GRADES = ("high", "medium", "low", "insufficient")


def build_issuer_grades(ledger, events=None):
    """Precompute {issuer: {grade, outcomes, settlement_success_rate}} from the corpus.
    Run once at startup / on refresh -- NOT per verdict. NEVER raises."""
    try:
        evs = events if events is not None else ledger.load()
    except Exception:
        return {}
    issuers = {e.get("issuer") for e in evs
               if isinstance(e, dict) and e.get("issuer")}
    out = {}
    for iss in issuers:
        prof = ledger.issuer_profile(iss, events=evs)
        if not prof:
            continue
        out[iss] = {"grade": issuer_trust(prof)["grade"],
                    "outcomes": prof.get("outcomes"),
                    "settlement_success_rate": prof.get("settlement_success_rate")}
    return out


class IssuerTrustSource:
    """O(1) issuer-grade lookup over a precomputed snapshot of the corpus."""

    def __init__(self, grades=None):
        self.grades = grades or {}

    @classmethod
    def from_ledger(cls, ledger, events=None):
        return cls(build_issuer_grades(ledger, events=events))

    def grade(self, issuer):
        return self.grades.get(issuer)

    def __len__(self):
        return len(self.grades)


def apply_issuer_trust(verdict, grade_info, gates_on=None):
    """PURE fold: record signals.issuer_trust (always, descriptive). When `gates_on`
    (defaults to ISSUER_TRUST_GATES) AND the grade is LOW, mark it `gated` so
    rwa_aggregate weighs it as an advisory signal. Never gates alone, never STOP, never
    upgrades. Non-mutating; NEVER raises."""
    if gates_on is None:
        gates_on = ISSUER_TRUST_GATES
    if not grade_info or not isinstance(grade_info, dict):
        return verdict
    grade = grade_info.get("grade")
    if grade not in _GRADES:
        return verdict
    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    gated = bool(gates_on and grade == "low")
    v["signals"]["issuer_trust"] = {"grade": grade, "gated": gated,
                                    "outcomes": grade_info.get("outcomes")}
    if grade == "low":
        reasons = list(v.get("reasons") or [])
        reasons.append(
            "issuer earned-trust grade is LOW (%s labeled outcomes)%s"
            % (grade_info.get("outcomes"),
               "" if gated else " -- advisory only; issuer-trust gating not yet "
               "calibrated (ISSUER_TRUST_GATES off)"))
        v["reasons"] = reasons
    return v
