#!/usr/bin/env python3
"""
rwa_aggregate.py -- the signal-aggregation / confidence layer over the RWA gates.

Blackwall now computes ~8 RWA signals (transfer-restriction, RAMS authorization, peg,
executable/market peg, backing, trading-halt, holder-concentration, ...). Each is
conservative + HOLD-only, so STACKING them is SAFE (a signal can only add caution, never
falsely clear). But naively letting every signal independently flip GO->HOLD has two
costs: (1) the cumulative FALSE-HOLD rate climbs with each gate, and (2) an operator gets
a PILE of HOLD reasons instead of one legible view.

This layer fixes both, WITHOUT changing the strong gates:

  * TIERS. Each signal is `gate` (may flip GO->HOLD on its own -- the high-precision
    eligibility/backing/price signals) or `advisory` (contributes to the risk view but
    does NOT gate alone -- the noisier ones, e.g. holder-concentration on an RWA, where
    issuer custody false-flags). Advisory signals INFORM; they don't each gate.
  * COMPOSITE. `aggregate(signals)` -> a single weighted risk {score, level,
    primary_concern, concerns[], gating[], advisory[]} -- the "one well-reasoned view."
  * COLLECTIVE ESCALATION. `apply_aggregate` records `signals.rwa_risk` and, when the
    ADVISORY signals ALONE agree past a weight threshold (several weak signals pointing
    the same way), escalates GO->HOLD once -- controlled, not per-signal.

PURE + DESCRIPTIVE (the gates already decided; this summarizes + adds the controlled
collective rule). Conservative-only, never STOP, never upgrades. Stdlib.
"""
from __future__ import annotations

ADVISORY_HOLD_WEIGHT = 0.5      # advisory signals summing >= this -> collective HOLD


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


# (signal_key, tier, weight, label, is_bad(signal_dict) -> bool). Order is descending
# severity; weights set the composite + collective rule. Extend as signals are added.
SIGNAL_SPECS = (
    ("rwa_transfer_readiness", "gate", 0.9,
     "the security transfer would REVERT (eligibility / authorization)",
     lambda s: s.get("grade") == "blocked"),
    ("backing", "gate", 0.8,
     "under-collateralized (proof-of-reserves)",
     lambda s: s.get("grade") == "under_collateralized"),
    ("dex_market", "gate", 0.6,
     "trading off NAV / high slippage on-chain",
     lambda s: s.get("grade") == "off_peg"),
    ("peg", "gate", 0.6,
     "overpaying vs the underlying NAV",
     lambda s: _num(s.get("divergence_ratio")) is not None
     and _num(s.get("divergence_ratio")) >= 1.10),
    ("rwa_asset", "gate", 0.5,
     "underlying trading is halted",
     lambda s: bool(s.get("trading_halted"))),
    ("holder_concentration", "advisory", 0.3,
     "token supply concentrated in one non-contract wallet",
     lambda s: s.get("grade") == "concentrated"),
    ("aave", "advisory", 0.3,
     "Aave has frozen this token's lending reserve (protocol de-risked it)",
     lambda s: s.get("grade") == "frozen"),
)

_TOTAL_WEIGHT = sum(w for _k, _t, w, _l, _b in SIGNAL_SPECS)


def _safe_bad(fn, s):
    try:
        return bool(fn(s))
    except Exception:
        return False


def aggregate(signals):
    """PURE: the recorded RWA `signals` dict -> a composite risk view. NEVER raises.

    Returns {score 0..1, level low|elevated|high, primary_concern, concerns[], gating[],
    advisory[], advisory_bad_weight}. `score` = fired weight / total weight; `level` is
    high when a strong gate (or several) fired."""
    signals = signals if isinstance(signals, dict) else {}
    concerns, bad_w, adv_bad_w = [], 0.0, 0.0
    for key, tier, weight, label, bad in SIGNAL_SPECS:
        s = signals.get(key)
        if isinstance(s, dict) and _safe_bad(bad, s):
            concerns.append({"key": key, "tier": tier, "weight": weight, "label": label})
            bad_w += weight
            if tier == "advisory":
                adv_bad_w += weight
    concerns.sort(key=lambda c: -c["weight"])
    score = round(bad_w / _TOTAL_WEIGHT, 3) if _TOTAL_WEIGHT else 0.0
    level = "high" if bad_w >= 0.6 else ("elevated" if bad_w > 0 else "low")
    return {
        "score": score, "level": level,
        "primary_concern": concerns[0]["label"] if concerns else None,
        "concerns": [c["label"] for c in concerns],
        "gating": [c["label"] for c in concerns if c["tier"] == "gate"],
        "advisory": [c["label"] for c in concerns if c["tier"] == "advisory"],
        "advisory_bad_weight": round(adv_bad_w, 3),
    }


def apply_aggregate(verdict, agg, advisory_hold_weight=ADVISORY_HOLD_WEIGHT):
    """PURE fold: record `signals.rwa_risk` (the composite view) and, when ADVISORY
    signals alone agree past `advisory_hold_weight`, escalate GO->HOLD ONCE (the
    controlled collective rule -- several weak signals pointing the same way). Never
    upgrades / never STOP; non-mutating."""
    if not agg or not isinstance(agg, dict):
        return verdict
    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    v["signals"]["rwa_risk"] = {k: agg.get(k) for k in
                                ("score", "level", "primary_concern", "concerns")}
    reasons = list(v.get("reasons") or [])
    if (v.get("verdict") == "GO"
            and agg.get("advisory_bad_weight", 0) >= advisory_hold_weight):
        v["verdict"] = "HOLD"
        reasons.append("escalated GO->HOLD: multiple advisory risk signals agree (%s)"
                       % ", ".join(agg.get("advisory") or []))
    v["reasons"] = reasons
    return v
