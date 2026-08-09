#!/usr/bin/env python3
"""
blackwall_guard.py -- framework-agnostic core of the Blackwall LangChain plugin.

"Call Blackwall before you sign." This module maps a Blackwall verdict
(GO/HOLD/STOP + hard_stop) into a guard DECISION (allow / confirm / block) under an
OBSERVE or ENFORCE policy, and provides two ways to obtain the verdict:

  * InProcessEngine -- import blackwall.forecast directly (deterministic, free, no
    network); the default for an embedded agent.
  * HttpEngine      -- POST /v1/forecast-payment to a deployed Blackwall service.

This layer has NO LangChain dependency and is fully unit-tested with stdlib only;
`langchain_blackwall.py` is the thin adapter on top. Fail-safe: if the engine is
unavailable, ENFORCE escalates to a human CONFIRM (never a silent allow) unless
`fail_open` is set.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# Import the verdict engine from the repo root regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Policy modes
OBSERVE = "observe"      # never blocks; annotates/logs the verdict
ENFORCE = "enforce"      # blocks STOP, human-confirms HOLD

# Guard actions
ALLOW = "allow"
CONFIRM = "confirm"      # hand to a human (HITL) before proceeding
BLOCK = "block"


class ForecastError(Exception):
    """The verdict could not be obtained (bad request, engine/HTTP error)."""


class PaymentRequired(ForecastError):
    """The Blackwall endpoint answered 402 (x402 fee not paid)."""


class PaymentBlocked(Exception):
    """Raised by an enforcing guard when a payment is blocked (STOP), or when a
    HOLD confirmation was declined."""
    def __init__(self, decision):
        self.decision = decision
        super().__init__(decision.summary())


@dataclass
class GuardDecision:
    action: str                       # ALLOW | CONFIRM | BLOCK
    verdict: str | None               # GO | HOLD | STOP | None (verdict unavailable)
    hard_stop: bool = False
    score: float | None = None
    reasons: list = field(default_factory=list)
    receipt_id: str | None = None

    @property
    def allowed(self) -> bool:
        """True only when the payment may proceed WITHOUT a human in the loop."""
        return self.action == ALLOW

    def summary(self) -> str:
        v = self.verdict or "UNKNOWN"
        head = "%s -> %s" % (v, self.action.upper())
        if self.reasons:
            head += ": " + "; ".join(str(r) for r in self.reasons[:3])
        return head


def decide(verdict_obj, mode=ENFORCE) -> GuardDecision:
    """Map a Blackwall verdict dict to a GuardDecision under `mode`.

    STOP -> BLOCK (enforce) / ALLOW (observe); HOLD -> CONFIRM (enforce) / ALLOW
    (observe); GO -> ALLOW. A missing/unknown verdict is treated conservatively
    like HOLD (CONFIRM under enforce)."""
    vo = verdict_obj if isinstance(verdict_obj, dict) else {}
    verdict = vo.get("verdict")
    hard_stop = bool(vo.get("hard_stop"))
    reasons = list(vo.get("reasons") or [])
    common = dict(verdict=verdict, hard_stop=hard_stop, score=vo.get("score"),
                  reasons=reasons, receipt_id=vo.get("receipt_id"))
    if verdict == "GO":
        return GuardDecision(action=ALLOW, **common)
    if verdict == "STOP":
        return GuardDecision(action=(BLOCK if mode == ENFORCE else ALLOW), **common)
    # HOLD, or a missing/unrecognized verdict -> escalate, never auto-allow.
    return GuardDecision(action=(CONFIRM if mode == ENFORCE else ALLOW), **common)


# ---------------------------------------------------------------------------
# Engines: obtain a verdict dict for a payment request.
# ---------------------------------------------------------------------------
class InProcessEngine:
    """Run blackwall.forecast() in-process (deterministic, free, no network)."""
    def __init__(self, reputation_source=None, **forecast_kwargs):
        import blackwall
        self._forecast = blackwall.forecast
        self._src = reputation_source or blackwall.MockReputationSource()
        self._kwargs = forecast_kwargs

    def forecast(self, request) -> dict:
        resp, err = self._forecast(dict(request or {}), self._src, **self._kwargs)
        if err is not None:
            raise ForecastError(err)
        return resp


class HttpEngine:
    """POST a payment request to a deployed Blackwall `/v1/forecast-payment`."""
    def __init__(self, base_url, *, path="/v1/forecast-payment", headers=None,
                 timeout=10, _transport=None):
        self.base_url = base_url.rstrip("/")
        self.path = path
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._transport = _transport

    def forecast(self, request) -> dict:
        url = self.base_url + self.path
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(self.headers)
        data = json.dumps(dict(request or {})).encode("utf-8")
        status, body = (self._transport or _urllib_post)(url, data, hdrs, self.timeout)
        body = body if isinstance(body, dict) else {}
        if status == 402:
            raise PaymentRequired("Blackwall requires an x402 fee (HTTP 402)")
        if status != 200:
            raise ForecastError("Blackwall HTTP %s: %s" % (status, body.get("error")))
        return body


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
# The guard: forecast -> decision, with a fail-safe on engine errors.
# ---------------------------------------------------------------------------
def check_payment(request, *, engine, mode=ENFORCE, fail_open=False) -> GuardDecision:
    """Obtain a verdict for `request` via `engine` and reduce it to a GuardDecision.

    If the engine raises (bad request, endpoint down, 402), the payment is NOT
    silently allowed: ENFORCE returns CONFIRM (hand to a human) unless `fail_open`,
    OBSERVE returns ALLOW. `PaymentRequired`/errors surface in the reasons."""
    try:
        verdict = engine.forecast(request)
    except Exception as e:
        action = ALLOW if (mode == OBSERVE or fail_open) else CONFIRM
        return GuardDecision(action=action, verdict=None,
                             reasons=["verdict unavailable: %s" % e])
    return decide(verdict, mode)


def guard_payment(request, *, engine, mode=ENFORCE, confirm=None, fail_open=False):
    """Enforce a decision: return the GuardDecision on ALLOW; call `confirm(decision)
    -> bool` on CONFIRM and raise PaymentBlocked if it declines (or if no confirm
    callback is given in ENFORCE mode); raise PaymentBlocked on BLOCK.

    In OBSERVE mode nothing is ever blocked -- the decision is returned as-is."""
    decision = check_payment(request, engine=engine, mode=mode, fail_open=fail_open)
    if decision.action == BLOCK:
        raise PaymentBlocked(decision)
    if decision.action == CONFIRM:
        ok = bool(confirm(decision)) if callable(confirm) else False
        if not ok:
            raise PaymentBlocked(decision)
    return decision
