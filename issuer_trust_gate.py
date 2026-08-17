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

from revert_scan import restriction_axis
from rwa_ledger import issuer_trust

# Reversibility lock. Flip True ONLY after the backfilled corpus is calibrated and the
# false-flag rate on known-good issuers is proven ~0. Off = descriptive (safe default).
ISSUER_TRUST_GATES = False

# Second, INDEPENDENT lock for the settlement-reliability axis (restriction-revert rate,
# revert_scan.py). Off = the axis is recorded but never changes the grade.
#
# DO NOT FLIP THIS. Permissioned issuers were ingested and the axis activated (BlackRock
# BUIDL 20 restriction reverts / 9.1%, Matrixdock STBT 7 / 3.4%). The measured counterfactual
# is that flipping it downgrades **BlackRock BUIDL to LOW** -- because its lock-up and
# registry-service checks revert transfers, i.e. because it is a properly permissioned
# security working AS DESIGNED. A restriction revert measures TRANSFER FRICTION, not issuer
# untrustworthiness, so this signal is MIS-HOMED here. Re-home it as an asset-level readiness
# signal beside rwa_readiness; do not turn it into an issuer-trust downgrade.
REVERT_AXIS_GATES = False
# A restriction-revert rate at/above this (with sufficient evidence) drags an issuer to LOW
# once REVERT_AXIS_GATES is on. 5% of transfer ATTEMPTS reverting on restrictions is a
# materially gated (permissioned) token an agent buyer should be cautioned about.
RESTRICTION_REVERT_LOW_RATE = 0.05

_GRADES = ("high", "medium", "low", "insufficient")


def _fold_revert_axis(grade_info, axis_info, gates_on):
    """Attach the settlement-reliability axis to a grade dict (always, descriptive) and,
    only when `gates_on` AND the axis has sufficient restriction-revert evidence AND the
    rate is material, drag the grade to LOW. Dormant otherwise. NEVER raises."""
    if not axis_info:
        return grade_info
    if not isinstance(grade_info, dict) or not isinstance(axis_info, dict):
        return grade_info
    info = dict(grade_info)
    info["restriction_axis"] = axis_info
    # Coerce the rate: the axis can be rebuilt from an operator-edited JSON summary, so a
    # string here must not raise (comparing str >= float is a TypeError).
    rate = axis_info.get("restriction_revert_rate")
    try:
        rate = float(rate) if rate is not None else 0.0
    except (TypeError, ValueError):
        rate = 0.0
    if gates_on and axis_info.get("evidence_sufficient") \
            and rate >= RESTRICTION_REVERT_LOW_RATE:
        info["grade"] = "low"
        info["restriction_downgraded"] = True
    return info


def build_issuer_grades(ledger, events=None, revert_summaries=None):
    """Precompute {issuer: {grade, outcomes, settlement_success_rate[, restriction_axis]}}
    from the corpus. Run once at startup / on refresh -- NOT per verdict.

    `revert_summaries` (optional) is a precomputed {issuer: revert_class_counts} map from
    `revert_scan` (an offline scan of the issuer's tokens). When present, the
    settlement-reliability axis is folded in -- DORMANT unless REVERT_AXIS_GATES is on and
    the issuer has material restriction-revert evidence. NEVER raises."""
    try:
        evs = events if events is not None else ledger.load()
    except Exception:
        return {}
    revert_summaries = revert_summaries or {}
    issuers = {e.get("issuer") for e in evs
               if isinstance(e, dict) and e.get("issuer")}
    out = {}
    for iss in issuers:
        # Fail-soft PER ISSUER: this snapshot is built at server startup, so one corrupt
        # row must not take down the whole map (and with it the server).
        try:
            prof = ledger.issuer_profile(iss, events=evs)
            if not prof:
                continue
            gi = {"grade": issuer_trust(prof)["grade"],
                  "outcomes": prof.get("outcomes"),
                  "settlement_success_rate": prof.get("settlement_success_rate")}
            if iss in revert_summaries:
                ax = restriction_axis(prof.get("outcomes") or 0, revert_summaries[iss])
                gi = _fold_revert_axis(gi, ax, REVERT_AXIS_GATES)
            out[iss] = gi
        except Exception:
            continue
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
    if not isinstance(verdict, dict):        # honour the "NEVER raises" contract
        return verdict
    grade = grade_info.get("grade")
    if grade not in _GRADES:
        return verdict
    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    gated = bool(gates_on and grade == "low")
    sig = {"grade": grade, "gated": gated, "outcomes": grade_info.get("outcomes")}
    # Surface the settlement-reliability axis when present (descriptive; dormant on today's
    # corpus). Lets an operator SEE the restriction-revert posture even while it can't gate.
    if grade_info.get("restriction_axis"):
        sig["restriction_axis"] = grade_info["restriction_axis"]
    v["signals"]["issuer_trust"] = sig
    if grade == "low":
        reasons = list(v.get("reasons") or [])
        reasons.append(
            "issuer earned-trust grade is LOW (%s labeled outcomes)%s"
            % (grade_info.get("outcomes"),
               "" if gated else " -- advisory only; issuer-trust gating not yet "
               "calibrated (ISSUER_TRUST_GATES off)"))
        v["reasons"] = reasons
    return v
