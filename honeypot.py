#!/usr/bin/env python3
"""
honeypot.py -- can the agent SELL what it is about to buy?

WHY THIS EXISTS. Every acquisition gate in this repo asks whether the BUY will
succeed: `rwa_readiness` asks whether the receiver may hold the token,
`settlement_sim` whether the stablecoin leg will clear, `holder_concentration`
whether one wallet can dump on you, `dex_price` whether the price is real. None
of them asks the question a honeypot exploits -- whether the position can ever be
EXITED. A honeypot token buys perfectly and cannot be sold: the contract permits
transfers to ordinary wallets and blocks the one address that matters, the market.

THE DISCRIMINATOR, and the reason this is not just another revert check. This
repo has already made the mistake once: `revert_scan`'s settlement-reliability
axis tried to downgrade BlackRock because BUIDL rejects non-allowlisted wallets
-- that is, because a permissioned security WORKS AS DESIGNED. A transfer revert
alone cannot tell a trap from compliance, so `REVERT_AXIS_GATES` stays off.

The control simulation separates them, and the rule is sharp:

    A restriction that PERMITS an arbitrary fresh wallet and FORBIDS the market
    is not compliance. It is a trap.

So the honeypot flag needs `RECEIVER_BLOCKED` from `transfer_sim.attribute` --
the pool reverts while a fresh control EOA succeeds. A permissioned security
blocks BOTH (nothing is allowlisted), which attributes to SENDER_BLOCKED and is
reported as `restricted`, deferring to `rwa_readiness` rather than being called
a scam. That asymmetry is the whole signal, and it is why the honeypot grade is
allowed to gate where the bare revert axis is not.

SECOND, SOFTER AXIS: the round trip. A token can be technically sellable and
still take 95% on the way out. Quoting buy then sell through the SAME pool gives
one number -- retention -- that captures sell tax, fee and slippage together. A
normal v3 round trip retains ~0.98-0.99; `RETENTION_HOLD` is set far below that
so only egregious extraction trips it.

DISPOSITION. `unsellable` GATES (GO -> HOLD). `high_sell_tax` sits behind the
reversibility lock `SELL_TAX_GATES`, DEFAULT OFF -- exactly the graduation
discipline `upto_scheme.EXCESSIVE_GATES` follows, because legitimate
fee-on-transfer tokens exist and the threshold wants measuring on a real corpus
before it refuses anyone's payment. HOLD-only in both cases, NEVER STOP:
sanctions and payload-mismatch keep the STOP authority, and this is inference
from a simulation, not proof. FAIL-OPEN throughout: an unreadable chain, a
missing pool, or an indeterminate transport all return `unknown`, which never
gates.

The decision-critical logic is pure and network-free; the network is injected.
"""
from decimal import Decimal, InvalidOperation

from transfer_sim import OK, RECEIVER_BLOCKED, SENDER_BLOCKED

SELLABLE = "sellable"
UNSELLABLE = "unsellable"
HIGH_SELL_TAX = "high_sell_tax"
RESTRICTED = "restricted"          # permissioned security -- rwa_readiness's job
UNKNOWN = "unknown"

# Reversibility lock, mirroring upto_scheme.EXCESSIVE_GATES. Flip to True only
# once the false-flag rate on legitimate fee-on-transfer tokens is measured.
SELL_TAX_GATES = False

# Round-trip retention below this is extraction, not fee. A 1% v3 pool round
# trips at ~0.98; 0.50 means "half the money vanished going out and back".
RETENTION_HOLD = Decimal("0.50")

# Revert classes that mean "the issuer restricts transfers", from revert_scan's
# calibration on real strings. Only consulted for the SENDER-blocked case.
_RESTRICTION_CLASSES = ("restriction",)


# ===========================================================================
# PURE core
# ===========================================================================
def round_trip_retention(spent, returned):
    """PURE: fraction of the quote asset that survives buy-then-sell, or None.

    `spent` is what goes IN to the buy; `returned` is what comes back OUT of the
    sell of everything the buy produced. Both in the same units (atomic quote).
    None whenever the ratio would be meaningless -- non-numeric, negative, or a
    zero denominator -- so an unreadable quote can never manufacture a finding.
    NEVER raises.
    """
    try:
        s = Decimal(str(spent))
        r = Decimal(str(returned))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if isinstance(spent, bool) or isinstance(returned, bool):
        return None
    # NaN/Infinity survive Decimal(str(...)) -- `Decimal(str(float("nan")))` is a
    # valid Decimal('NaN') -- and then COMPARING one raises InvalidOperation. A
    # function documented never to raise must reject them before the comparison,
    # not after. Found by fuzz, not by the unit tests.
    if s.is_nan() or r.is_nan() or s.is_infinite() or r.is_infinite():
        return None
    if s <= 0 or r < 0:
        return None
    return r / s


def assess_honeypot(sell_path=None, retention=None, retention_hold=RETENTION_HOLD):
    """PURE: a sell-path attribution + a round-trip retention -> a honeypot signal.

    `sell_path` is the dict `transfer_sim.attribute` returns for a simulated
    transfer of the token TO ITS OWN DEX POOL, with a fresh EOA as the control.

    Grades:
      unsellable    -- the pool is blocked while the fresh control is not. A trap.
      restricted    -- BOTH blocked with a restriction-class revert: a permissioned
                       security behaving correctly. Deferred to rwa_readiness,
                       explicitly NOT called a honeypot.
      high_sell_tax -- sellable, but the round trip retains less than the floor.
      sellable      -- transfers to the pool and round trips within the floor.
      unknown       -- anything indeterminate. Never gates.

    Returns {grade, reasons[], retention, revert_reason}. NEVER raises.
    """
    out = {"grade": UNKNOWN, "reasons": [], "retention": None,
           "revert_reason": None}
    sp = sell_path if isinstance(sell_path, dict) else {}
    outcome = sp.get("outcome")
    reason = sp.get("reason")
    revert_class = sp.get("revert_class")

    if outcome == RECEIVER_BLOCKED:
        # The sharp case. The control succeeded, so the contract is not refusing
        # everyone -- it is refusing the market specifically. The revert class is
        # deliberately NOT consulted here: a honeypot is free to borrow
        # compliance-shaped wording, and "allowlisted wallets only, except any
        # wallet at all, except the pool" is not a coherent compliance posture.
        out["grade"] = UNSELLABLE
        out["revert_reason"] = reason
        out["reasons"] = [
            "token blocks transfers to its own liquidity pool while permitting an "
            "arbitrary fresh wallet -- the position cannot be exited (honeypot)"]
        return out

    if outcome == SENDER_BLOCKED:
        if revert_class in _RESTRICTION_CLASSES:
            # Both blocked, restriction-class revert: a permissioned security.
            # NOT a honeypot. rwa_readiness owns this and grades it properly.
            out["grade"] = RESTRICTED
            out["revert_reason"] = reason
            out["reasons"] = [
                "token restricts transfers for the holder as well as the pool -- "
                "a permissioned security, not a honeypot (see rwa_readiness)"]
            return out
        # A balance/gas/opaque sender-side failure says nothing about sellability.
        return out

    if outcome != OK:
        return out

    # The transfer path is clear. Fall through to the round trip.
    if retention is None:
        out["grade"] = SELLABLE
        return out
    try:
        r = Decimal(str(retention))
    except (InvalidOperation, TypeError, ValueError):
        return out
    if r.is_nan() or r.is_infinite():
        return out          # same NaN-comparison trap as round_trip_retention
    out["retention"] = r
    if r < Decimal(str(retention_hold)):
        out["grade"] = HIGH_SELL_TAX
        out["reasons"] = [
            "buying and immediately selling returns %.1f%% of the amount spent -- "
            "the exit is taxed far beyond fee and slippage" % (r * 100)]
        return out
    out["grade"] = SELLABLE
    return out


def apply_honeypot(verdict, signal, sell_tax_gates=None):
    """PURE fold: annotate `signals.honeypot` and escalate GO -> HOLD.

    `unsellable` always gates. `high_sell_tax` gates only when SELL_TAX_GATES is
    on (default OFF -- recorded and reasoned, but not enforced). `restricted`
    NEVER gates: rwa_readiness owns permissioned securities and double-counting
    would penalise a token twice for one property.

    CONSERVATIVE-ONLY: never upgrades a verdict, never produces STOP or
    hard_stop, and a missing or malformed signal leaves the verdict untouched.
    Non-mutating. NEVER raises.
    """
    if not isinstance(signal, dict) or not isinstance(verdict, dict):
        return verdict
    grade = signal.get("grade")
    if grade not in (SELLABLE, UNSELLABLE, HIGH_SELL_TAX, RESTRICTED, UNKNOWN):
        return verdict
    gates = SELL_TAX_GATES if sell_tax_gates is None else sell_tax_gates

    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    rec = {"grade": grade}
    if signal.get("retention") is not None:
        rec["retention"] = str(signal["retention"])
    if signal.get("revert_reason"):
        rec["revert_reason"] = signal["revert_reason"]
    v["signals"]["honeypot"] = rec

    reasons = list(v.get("reasons") or [])
    reasons.extend(signal.get("reasons") or [])
    should_gate = (grade == UNSELLABLE) or (grade == HIGH_SELL_TAX and gates)
    if should_gate and v.get("verdict") == "GO":
        v["verdict"] = "HOLD"
        reasons.append("escalated GO->HOLD: the acquired token may not be sellable")
    v["reasons"] = reasons
    return v


# ===========================================================================
# LIVE source -- network injected, fail-open
# ===========================================================================
class HoneypotSource:
    """Live sell-path check. FAIL-OPEN: every failure path returns `unknown`.

    Needs three things a live request never carries, all injected so the pure
    core stays testable: a `TransferSimulator`, a `holder_lookup(token, chain)`
    for a funded sender (an empty wallet reverts on BALANCE, which says nothing),
    and a `pool_lookup(token, chain)` for the token's own deepest pool.

    OPT-IN at the call site. NEVER raises out of check().
    """

    def __init__(self, simulator=None, holder_lookup=None, pool_lookup=None,
                 quoter=None, probe_amount=None):
        self.simulator = simulator
        self.holder_lookup = holder_lookup
        self.pool_lookup = pool_lookup
        self.quoter = quoter
        self.probe_amount = probe_amount

    def check(self, token, chain):
        unknown = {"grade": UNKNOWN, "reasons": [], "retention": None,
                   "revert_reason": None}
        if not token or self.simulator is None or self.pool_lookup is None:
            return unknown
        try:
            pool = self.pool_lookup(token, chain)
            if not pool:
                return unknown
            holder = None
            if self.holder_lookup is not None:
                holder = self.holder_lookup(token, chain)
            if not holder:
                return unknown
            amount = self.probe_amount or 1
            sell_path = self.simulator.assess(token, holder, pool, amount)
            retention = None
            if self.quoter is not None:
                retention = self._retention(token, chain)
            return assess_honeypot(sell_path, retention)
        except Exception:
            return unknown

    def _retention(self, token, chain):
        """Buy-then-sell through the same pool -> retention, or None. Fail-open."""
        try:
            spent, got = self.quoter(token, chain, "buy")
            if not got:
                return None
            _, back = self.quoter(token, chain, "sell", got)
            return round_trip_retention(spent, back)
        except Exception:
            return None
