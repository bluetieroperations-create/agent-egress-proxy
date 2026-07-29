#!/usr/bin/env python3
"""
wallet_guard.py -- gate a wallet's SIGNING call with a Blackwall verdict.

The strongest place for a payment guard is the moment of signing, which lives in
the wallet. This is the provider-agnostic core: given the payment a wallet is about
to sign, get the Blackwall verdict + payload screen and either let the signature
proceed (GO), require a human (HOLD), or WITHHOLD it (STOP). The per-provider
adapters (turnkey_signer.py, privy_signer.py) are thin shims that map a provider's
request into the `check_payment`/`guard_sign` call here.

AVAILABILITY (the "tax" of sitting in the signing path): when the verdict engine is
UNREACHABLE, the behaviour is a TOGGLE, set per deployment and changeable at
runtime:

    FAIL_CLOSED -> withhold the signature (payments halt; safest, but Blackwall
                   becomes a hard dependency of the wallet)
    FAIL_OPEN   -> sign anyway (degrade to advisory; keeps money moving, loses the
                   guarantee during the outage)

A STOP *verdict* always withholds -- the toggle only governs the "can't reach
Blackwall" case. Stdlib only; the verdict comes from blackwall.forecast in-process
or a deployed /v1/forecast-payment over HTTP.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from decimal import Decimal

# repo root on path -> import the verdict engine + calldata decoder.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Policy modes
OBSERVE = "observe"
ENFORCE = "enforce"
# Availability toggle (engine unreachable)
FAIL_CLOSED = "fail_closed"
FAIL_OPEN = "fail_open"
# Actions
ALLOW = "allow"
CONFIRM = "confirm"
BLOCK = "block"


# Customer-facing copy for the availability toggle, so a wallet provider's settings
# UI can render the choice straight from the library (plain language, not dev docs).
_POLICY_COPY = {
    FAIL_CLOSED: {
        "label": "Pause payments",
        "tagline": "Safest — default",
        "customer": ("If Blackwall's safety check is ever briefly unavailable, your "
                     "wallet waits and won't send a payment until it's back. Nothing "
                     "risky slips through — but during that short window, payments "
                     "are on hold."),
        "best_for": "Treasuries and high-value wallets — correctness over speed.",
    },
    FAIL_OPEN: {
        "label": "Keep paying",
        "tagline": "Keeps things moving",
        "customer": ("If Blackwall's safety check is briefly unavailable, your wallet "
                     "keeps sending payments as normal. You're never held up — but "
                     "for that short window, payments go out without the extra check."),
        "best_for": "Everyday agent spending wallets — liveness over the guarantee.",
    },
}
_ALWAYS_BLOCKS_NOTE = ("Either way, a payment flagged as dangerous is always blocked. "
                       "This setting only changes what happens in the rare moment "
                       "Blackwall itself can't be reached.")
_TOGGLE_QUESTION = ("If Blackwall can't be reached for a moment, what should your "
                    "wallet do?")


def describe_policy(policy=None):
    """Customer-facing description of the availability toggle, for a wallet UI.

    With a `policy` (FAIL_CLOSED/FAIL_OPEN) -> that option's copy dict
    {value, label, tagline, customer, best_for}. Without one -> the whole control:
    {question, default, options:[both option dicts], note} where `note` is the
    "a dangerous payment is always blocked" reassurance. Raises on an unknown policy."""
    if policy is None:
        return {
            "question": _TOGGLE_QUESTION,
            "default": FAIL_CLOSED,
            "options": [dict(_POLICY_COPY[FAIL_CLOSED], value=FAIL_CLOSED),
                        dict(_POLICY_COPY[FAIL_OPEN], value=FAIL_OPEN)],
            "note": _ALWAYS_BLOCKS_NOTE,
        }
    if policy not in _POLICY_COPY:
        raise ValueError("policy must be FAIL_CLOSED or FAIL_OPEN")
    return dict(_POLICY_COPY[policy], value=policy)


class Decision:
    def __init__(self, *, action, verdict=None, hard_stop=False, score=None,
                 reasons=None, receipt_id=None, degraded=False):
        self.action = action
        self.verdict = verdict
        self.hard_stop = hard_stop
        self.score = score
        self.reasons = reasons or []
        self.receipt_id = receipt_id
        self.degraded = degraded          # True when the availability toggle decided it

    @property
    def allowed(self):
        return self.action == ALLOW

    def summary(self):
        head = "%s -> %s" % (self.verdict or "UNAVAILABLE", self.action.upper())
        if self.reasons:
            head += ": " + "; ".join(str(r) for r in self.reasons[:3])
        return head


class SignResult:
    def __init__(self, *, signed, decision, signature=None):
        self.signed = signed
        self.decision = decision
        self.signature = signature


def _atomic_to_decimal(atomic, decimals):
    try:
        d = Decimal(int(atomic)) / (Decimal(10) ** int(decimals))
    except Exception:
        return None
    return format(d, "f") if d > 0 else None


def claim_from_tx(tx, *, chain="base", token_decimals=6, native_decimals=18):
    """Best-effort: turn a transaction the wallet is about to sign into a Blackwall
    forecast payload. Decodes ERC-20 transfer/transferFrom to recover the real
    recipient + amount; falls back to the tx target for native/other calls. Always
    carries `transaction` so Blackwall's Phase-3 calldata screen runs regardless."""
    from calldata import decode_call
    tx = tx if isinstance(tx, dict) else {}
    to = tx.get("to")
    data = tx.get("data") or "0x"
    try:
        value = int(tx.get("value", 0) or 0)
    except (TypeError, ValueError):
        value = 0

    counterparty, asset, amount = to, (tx.get("asset") or ""), None
    dec = decode_call(data)
    if dec and dec.get("name") in ("transfer", "transferFrom"):
        counterparty = dec["args"].get("to")
        asset = to                         # the token contract being called
        amount = _atomic_to_decimal(dec["args"].get("amount", 0), token_decimals)
    elif value > 0:
        amount = _atomic_to_decimal(value, native_decimals)
        asset = asset or "native"

    # forecast needs a positive amount; use a nominal placeholder for non-value
    # calls (e.g. approve) so the verdict + calldata drainer-screen still run.
    if amount is None:
        amount = "0.000001"
    return {"counterparty": counterparty, "amount": amount,
            "asset": asset or "USDC", "chain": tx.get("chain") or chain,
            "transaction": {"to": to, "data": data, "value": value}}


# ---------------------------------------------------------------------------
# Deciders: obtain a verdict dict for a forecast payload.
# ---------------------------------------------------------------------------
def in_process_decider(reputation_source=None, **forecast_kwargs):
    """A decider backed by blackwall.forecast (free, deterministic, no network)."""
    import blackwall
    src = reputation_source or blackwall.MockReputationSource()

    def _fn(payload):
        resp, err = blackwall.forecast(dict(payload or {}), src, **forecast_kwargs)
        if err is not None:
            raise ValueError(err)
        return resp
    return _fn


def http_decider(base_url, *, path="/v1/forecast-payment", headers=None, timeout=10,
                 _transport=None):
    """A decider that POSTs to a deployed Blackwall service."""
    base = base_url.rstrip("/")

    def _fn(payload):
        url = base + path
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        data = json.dumps(dict(payload or {})).encode("utf-8")
        status, body = (_transport or _urllib_post)(url, data, hdrs, timeout)
        body = body if isinstance(body, dict) else {}
        if status == 402:
            raise RuntimeError("Blackwall requires an x402 fee (HTTP 402)")
        if status != 200:
            raise RuntimeError("Blackwall HTTP %s: %s" % (status, body.get("error")))
        return body
    return _fn


def _urllib_post(url, data, headers, timeout):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


# ---------------------------------------------------------------------------
# The wallet guard.
# ---------------------------------------------------------------------------
class WalletGuard:
    """Gate a signing call. `decide_fn(payload) -> verdict_dict` obtains the verdict
    (in_process_decider / http_decider). `mode` OBSERVE never blocks. `on_unreachable`
    is the availability TOGGLE (FAIL_CLOSED | FAIL_OPEN), changeable at runtime via
    `set_availability()`. `confirm(decision) -> bool` gates a HOLD."""

    def __init__(self, decide_fn, *, mode=ENFORCE, on_unreachable=FAIL_CLOSED,
                 confirm=None):
        self._decide = decide_fn
        self.mode = mode
        self.on_unreachable = on_unreachable
        self.confirm = confirm

    def set_availability(self, policy):
        """Toggle FAIL_CLOSED <-> FAIL_OPEN at runtime."""
        if policy not in (FAIL_CLOSED, FAIL_OPEN):
            raise ValueError("policy must be FAIL_CLOSED or FAIL_OPEN")
        self.on_unreachable = policy
        return self.on_unreachable

    def describe_availability(self):
        """Customer-facing copy for the CURRENTLY-selected availability policy
        (for a 'your setting: ...' UI). See describe_policy()."""
        return describe_policy(self.on_unreachable)

    def check(self, payload) -> Decision:
        try:
            verdict = self._decide(payload) or {}
        except Exception as e:
            # engine unreachable / bad payload -> the availability toggle decides.
            if self.mode == OBSERVE or self.on_unreachable == FAIL_OPEN:
                action = ALLOW
            else:
                action = BLOCK          # FAIL_CLOSED under ENFORCE
            return Decision(action=action, verdict=None, degraded=True,
                            reasons=["verdict unavailable: %s" % e])
        v = verdict.get("verdict")
        common = dict(verdict=v, hard_stop=bool(verdict.get("hard_stop")),
                      score=verdict.get("score"),
                      reasons=list(verdict.get("reasons") or []),
                      receipt_id=verdict.get("receipt_id"))
        if self.mode == OBSERVE:
            return Decision(action=ALLOW, **common)
        if v == "STOP":
            return Decision(action=BLOCK, **common)
        if v == "GO":
            return Decision(action=ALLOW, **common)
        return Decision(action=CONFIRM, **common)         # HOLD / unknown

    def guard_sign(self, payload, sign_fn) -> SignResult:
        """Run the check; call `sign_fn()` and return its signature only if the
        payment may proceed (GO, or an approved HOLD). Otherwise withhold."""
        decision = self.check(payload)
        if decision.action == BLOCK:
            return SignResult(signed=False, decision=decision)
        if decision.action == CONFIRM:
            ok = bool(self.confirm(decision)) if callable(self.confirm) else False
            if not ok:
                return SignResult(signed=False, decision=decision)
        return SignResult(signed=True, decision=decision, signature=sign_fn())
