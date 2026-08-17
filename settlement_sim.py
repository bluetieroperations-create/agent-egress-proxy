#!/usr/bin/env python3
"""
settlement_sim.py -- PRE-SIGNATURE settlement feasibility for the CORE x402 path: before
an agent signs an EIP-3009 authorization, ask the chain whether the USDC transfer would
actually succeed -- and if not, WHO is blocked.

This is the crypto-side application of everything the RWA arc taught us:

  1. SIMULATE, don't interrogate. Asking a token "do you expose a blocklist?" fails
     (0/535 tokens answered); `eth_call`ing the transfer answers definitively. Proven
     live: simulating a USDC transfer from an OFAC-blacklisted address returns the real
     string "Blacklistable: account is blacklisted".
  2. USE A CONTROL. A revert alone cannot tell you whether the PAYER or the PAYEE is the
     problem. Simulating a second transfer to a neutral control address separates them:
     payer->payee fails but payer->control succeeds  => the PAYEE is blocked;
     both fail                                        => the PAYER is blocked.
  3. MIND THE SEMANTICS. The RWA axis taught us a blocked transfer can mean "working as
     designed" (a permissioned security rejecting a non-KYC wallet), so we did NOT let it
     touch issuer trust. Here the meaning is different and much sharper: Circle freezes a
     USDC address for cause (sanctions, theft, fraud), and either way the payment WILL
     FAIL on-chain. So it is a legitimate payment-time signal -- but still HOLD-only.

HARD BOUNDARY (mirrors blockscout.py): this module NEVER produces a STOP and never clears
a verdict. `sanctions.py` remains the sole compliance/STOP authority; this only says "this
payment would not settle" and escalates GO -> HOLD. Fail-open everywhere: an unreachable
RPC, an unknown token, or an ambiguous revert is a no-op.

Underfunded payers are recorded but do NOT gate -- an agent may top up between the
forecast and the signature, so gating on balance would be noisy and wrong.

Opt-in via `BLACKWALL_SETTLEMENT_SIM=1` (+ an RPC URL). Pure core + injected transport.
"""
from __future__ import annotations

from transfer_sim import (OK, RECEIVER_BLOCKED, SENDER_BLOCKED, TransferSimulator,
                          attribute)

# Canonical USDC deployments we can simulate against, per chain. VERIFIED addresses only.
USDC_BY_CHAIN = {
    "base": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "ethereum": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
}

# Grades. `ready` = simulated clean; the two *_blocked grades gate; others are notes.
READY = "ready"
PAYEE_BLOCKED = "payee_blocked"
PAYER_BLOCKED = "payer_blocked"
UNDERFUNDED = "underfunded"
SIM_UNKNOWN = "unknown"

_GRADES = (READY, PAYEE_BLOCKED, PAYER_BLOCKED, UNDERFUNDED, SIM_UNKNOWN)
# Only these gate a verdict (GO -> HOLD). Underfunded is descriptive on purpose.
_GATING = (PAYEE_BLOCKED, PAYER_BLOCKED)


def assess_settlement(attribution):
    """PURE: attribution (from transfer_sim.attribute) -> a settlement signal.

    Returns {"grade", "reason", "revert_class", "blocked_party"}. Only a RESTRICTION-class
    revert attributes a block; a balance-class revert becomes `underfunded` (a note), and
    anything else is `unknown`. NEVER raises."""
    a = attribution if isinstance(attribution, dict) else {}
    outcome, cls, reason = a.get("outcome"), a.get("revert_class"), a.get("reason")

    if outcome == OK:
        return {"grade": READY, "reason": None, "revert_class": None,
                "blocked_party": None}
    if cls == "balance":
        return {"grade": UNDERFUNDED, "reason": reason, "revert_class": cls,
                "blocked_party": "payer"}
    if cls == "restriction":
        if outcome == RECEIVER_BLOCKED:
            return {"grade": PAYEE_BLOCKED, "reason": reason, "revert_class": cls,
                    "blocked_party": "payee"}
        if outcome == SENDER_BLOCKED:
            return {"grade": PAYER_BLOCKED, "reason": reason, "revert_class": cls,
                    "blocked_party": "payer"}
    return {"grade": SIM_UNKNOWN, "reason": reason, "revert_class": cls,
            "blocked_party": None}


def apply_settlement_sim(verdict, signal):
    """PURE fold. CONSERVATIVE-ONLY: escalates GO -> HOLD when the simulated payment would
    revert because the PAYER or PAYEE is restricted on the stablecoin. NEVER upgrades,
    NEVER STOPs (sanctions.py stays the compliance authority), never mutates the input.
    A None / non-dict / unrecognized-grade signal is a no-op. NEVER raises."""
    if not signal or not isinstance(signal, dict):
        return verdict
    if not isinstance(verdict, dict):
        return verdict
    grade = signal.get("grade")
    if grade not in _GRADES:
        return verdict

    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    v["signals"]["settlement_sim"] = {
        "grade": grade, "blocked_party": signal.get("blocked_party"),
        "revert_class": signal.get("revert_class"), "reason": signal.get("reason"),
        "gated": grade in _GATING,
    }
    reasons = list(v.get("reasons") or [])
    detail = (" (on-chain revert: %s)" % signal["reason"]) if signal.get("reason") else ""

    if grade == PAYEE_BLOCKED:
        reasons.append(
            "settlement simulation: the PAYEE is restricted on the stablecoin contract -- "
            "this payment would REVERT on-chain%s" % detail)
        if v.get("verdict") == "GO":
            v["verdict"] = "HOLD"
            reasons.append(
                "escalated GO->HOLD: the recipient cannot receive this stablecoin "
                "(frozen/blacklisted by the issuer) -- do not sign; the transfer fails and "
                "a blacklisted counterparty is itself a serious risk signal")
    elif grade == PAYER_BLOCKED:
        reasons.append(
            "settlement simulation: YOUR wallet is restricted on the stablecoin contract "
            "-- this payment would REVERT on-chain%s" % detail)
        if v.get("verdict") == "GO":
            v["verdict"] = "HOLD"
            reasons.append(
                "escalated GO->HOLD: your paying wallet is frozen/blacklisted on this "
                "stablecoin -- resolve with the issuer before signing")
    elif grade == UNDERFUNDED:
        # Descriptive ONLY: balance can change between forecast and signature.
        reasons.append(
            "settlement simulation: the paying wallet appears underfunded for this "
            "amount%s (not gated -- balance may change before you sign)" % detail)
    elif grade == READY:
        reasons.append("settlement simulation: the stablecoin transfer simulates cleanly")

    v["reasons"] = reasons
    return v


class SettlementSimSource:
    """Live settlement simulation for an x402 payment. `check(claim)` -> signal or None.

    `claim` needs {counterparty (payee), amount, asset, chain, payer}. Requires a `payer`
    -- you cannot simulate a transfer out of an unknown wallet -- and a recognized
    stablecoin on a supported chain. Returns None (no-op) otherwise. NEVER raises."""

    def __init__(self, rpc_by_chain=None, simulator=None, tokens=None, timeout=4.0):
        self.rpc_by_chain = rpc_by_chain or {}
        self.tokens = tokens or USDC_BY_CHAIN
        self.timeout = timeout
        self._simulator = simulator

    def _sim_for(self, chain):
        if self._simulator is not None:
            return self._simulator
        url = self.rpc_by_chain.get(chain)
        return TransferSimulator(rpc_url=url, timeout=self.timeout) if url else None

    def check(self, claim):
        if not isinstance(claim, dict):
            return None
        payer, payee = claim.get("payer"), claim.get("counterparty")
        chain = (claim.get("chain") or "").strip().lower()
        asset = (claim.get("asset") or "").strip().upper()
        if not payer or not payee or asset != "USDC":
            return None
        token = self.tokens.get(chain)
        sim = self._sim_for(chain)
        if not token or sim is None:
            return None
        try:
            amount = _base_units(claim.get("amount"))
            att = sim.assess(token, payer, payee, amount)
        except Exception:
            return None
        return assess_settlement(att)


def _base_units(amount, decimals=6):
    """USDC display amount -> integer base units. Defensive; falls back to 1 unit."""
    try:
        from decimal import Decimal
        from transfer_sim import clamp_amount
        return clamp_amount(int(Decimal(str(amount)) * (10 ** decimals)))
    except Exception:
        return 1
