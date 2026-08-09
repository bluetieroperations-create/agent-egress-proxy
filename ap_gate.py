#!/usr/bin/env python3
"""
ap_gate.py -- treasury / accounts-payable payout gate.

A thin adapter that folds Blackwall's verdict into an AP "approve & release"
decision: screen a prospective payout BEFORE it is signed and return one of
RELEASE / REVIEW / BLOCK. No new engine -- it maps a payout onto the existing
`forecast()` call (payee -> counterparty, invoice_id -> resource) and translates
GO/HOLD/STOP into an action a payments system can act on:

  GO   -> RELEASE  (auto-approve)
  HOLD -> REVIEW   (route to a human approver -- pair with the confirmation HITL)
  STOP -> BLOCK    (sanctioned / price-gouge / known-bad -- never release)

Verdict-only, never custody: the gate returns a decision + the signed
`receipt_id` (the audit trail compliance wants); releasing funds is the caller's
job. The verdict->action mapping is a pure function, tested TDD-first. Stdlib
only.
"""
from __future__ import annotations

from blackwall import forecast

RELEASE = "release"
REVIEW = "review"
BLOCK = "block"

# STOP is the only auto-block; HOLD escalates to a human; GO auto-releases.
_VERDICT_ACTION = {"GO": RELEASE, "HOLD": REVIEW, "STOP": BLOCK}


def payout_action(verdict):
    """Pure: map a GO/HOLD/STOP verdict to an AP action.

    An unknown or missing verdict is treated conservatively as REVIEW -- the gate
    must never auto-RELEASE on an unclear signal (fail toward a human, not toward
    releasing funds).
    """
    return _VERDICT_ACTION.get(verdict, REVIEW)


def payout_payload(payout):
    """Pure: translate an AP payout dict into a `forecast()` request.

    payee -> counterparty ; invoice_id -> resource. `resource` now drives
    PER-INVOICE-CLASS price comparison: when the payee has >= MIN_CLASS_OBSERVATIONS
    same-class observations from DISTINCT payers (via the ledger flywheel), the
    amount is priced against that class's median instead of the payee's pooled full
    history -- so a legit first large invoice to a small-history vendor isn't a
    false gouge. On-chain-only histories (reputation store) carry no class and
    pool (conservative). Cross-counterparty peer-group comparison is still a
    roadmap item. `resource` also drives endpoint-readiness enrichment.
    """
    payload = {
        "counterparty": payout.get("payee"),
        "amount": payout.get("amount"),
        "asset": payout.get("asset"),
        "chain": payout.get("chain"),
    }
    if payout.get("payer"):
        payload["payer"] = payout["payer"]
    if payout.get("invoice_id"):
        payload["resource"] = payout["invoice_id"]
    return payload


def screen_payout(payout, reputation_source, ledger=None, readiness_source=None,
                  hold_above=None):
    """Screen a prospective payout; return an AP decision dict:

        {action, requires_human, verdict, score, reasons, signals,
         receipt_id, report_token, error?}

    `action` is RELEASE / REVIEW / BLOCK; `requires_human` is True only for
    REVIEW. On a validation error the decision FAILS CLOSED to REVIEW -- an
    unscoreable payout must never auto-release.

    `hold_above` raises the amount at which a payout escalates to REVIEW (default
    $10) -- set it to your treasury auto-release ceiling so routine in-line
    vendor payments auto-release while sanctions/price-anomaly still BLOCK.
    """
    verdict, err = forecast(payout_payload(payout), reputation_source,
                            ledger=ledger, readiness_source=readiness_source,
                            hold_above=hold_above)
    if err is not None:
        return {"action": REVIEW, "requires_human": True, "verdict": None,
                "score": None, "reasons": [err], "signals": {},
                "receipt_id": None, "report_token": None, "error": err}
    action = payout_action(verdict.get("verdict"))
    return {
        "action": action,
        "requires_human": action == REVIEW,
        "verdict": verdict.get("verdict"),
        "score": verdict.get("score"),
        "reasons": verdict.get("reasons", []),
        "signals": verdict.get("signals", {}),
        "receipt_id": verdict.get("receipt_id"),
        "report_token": verdict.get("report_token"),
    }
