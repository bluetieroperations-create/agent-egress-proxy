#!/usr/bin/env python3
"""
langchain_blackwall.py -- LangChain adapter for the Blackwall payment guard.

Thin wrapper over blackwall_guard.py (which holds all the logic and is
LangChain-free). Provides:

  * BlackwallPaymentGuardTool -- a tool the agent CALLS before signing a payment;
    returns a GO/HOLD/STOP verdict with reasons.
  * BlackwallGuardrailCallback -- a callback handler that ENFORCES the verdict: it
    intercepts your payment tool(s) and blocks a STOP / human-confirms a HOLD before
    the tool runs, even if the agent "forgets" to check.

Requires `langchain-core`. The core (blackwall_guard) does not.
"""
from __future__ import annotations

from typing import Any, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from blackwall_guard import (ENFORCE, OBSERVE, GuardDecision, PaymentBlocked,
                             check_payment, guard_payment)


class PaymentInput(BaseModel):
    """The prospective payment to screen."""
    counterparty: str = Field(description="recipient wallet address from the 402")
    amount: str = Field(description='amount as a decimal string, e.g. "0.09"')
    asset: str = Field(default="USDC", description="asset, e.g. USDC")
    chain: str = Field(default="base", description="chain, e.g. base")
    payer: Optional[str] = Field(default=None, description="the agent's own wallet (optional)")
    resource: Optional[str] = Field(default=None, description="what's being paid for (URL)")
    payment_authorization: Optional[str] = Field(
        default=None, description="the actual signed X-PAYMENT (base64) you're about "
                                  "to send, for signed-payload cross-check (optional)")
    transaction: Optional[dict] = Field(
        default=None, description="a raw contract-call payment {to,data,value} to "
                                  "screen for drainer calldata (optional)")


def _request_from(kw) -> dict:
    return {k: kw.get(k) for k in
            ("counterparty", "amount", "asset", "chain", "payer", "resource",
             "payment_authorization", "transaction") if kw.get(k) is not None}


def render_decision(decision: GuardDecision) -> str:
    """A compact, agent-readable rendering of a guard decision."""
    verb = {"allow": "You may proceed and sign this payment.",
            "confirm": "HOLD -- get explicit human approval before signing.",
            "block": "STOP -- do NOT sign this payment."}.get(decision.action, "")
    parts = ["VERDICT: %s (%s)." % (decision.verdict or "UNKNOWN",
                                    decision.action.upper()), verb]
    if decision.reasons:
        parts.append("Reasons: " + "; ".join(str(r) for r in decision.reasons[:4]))
    if decision.score is not None:
        parts.append("trust score %.2f." % decision.score)
    return " ".join(p for p in parts if p)


class BlackwallPaymentGuardTool(BaseTool):
    """Screen an x402/on-chain payment BEFORE signing it."""
    name: str = "blackwall_payment_guard"
    description: str = (
        "Screen an x402 or on-chain payment BEFORE you sign or send it. Returns a "
        "GO/HOLD/STOP verdict from counterparty reputation, price anomaly, sanctions, "
        "and (if you pass the signed payload) a cross-check that the signature really "
        "pays who/what you think. ALWAYS call this before any tool that moves funds; "
        "do not pay on STOP, and get human approval on HOLD.")
    args_schema: type = PaymentInput
    engine: Any = None
    mode: str = ENFORCE
    model_config = {"arbitrary_types_allowed": True}

    def _run(self, counterparty, amount, asset="USDC", chain="base", payer=None,
             resource=None, payment_authorization=None, transaction=None,
             run_manager=None) -> str:
        if self.engine is None:
            raise ValueError("BlackwallPaymentGuardTool needs an `engine=` "
                             "(InProcessEngine or HttpEngine)")
        request = _request_from(locals())
        decision = check_payment(request, engine=self.engine, mode=self.mode)
        return render_decision(decision)


class BlackwallGuardrailCallback(BaseCallbackHandler):
    """Enforce the verdict on designated payment tools.

    `extract(serialized, tool_input) -> request|None` pulls the payment fields from a
    tool's invocation (`tool_input` is the structured args dict when available, else
    the raw input string); return None for tools that aren't payments (they pass
    through). On a STOP the tool is blocked (PaymentBlocked raised, aborting the run);
    on a HOLD `confirm(decision) -> bool` gates it. Set `raise_error` so the
    exception aborts the tool rather than being swallowed."""
    raise_error: bool = True

    def __init__(self, *, engine, extract, mode=ENFORCE, confirm=None,
                 fail_open=False):
        super().__init__()
        self._engine = engine
        self._extract = extract
        self._mode = mode
        self._confirm = confirm
        self._fail_open = fail_open

    def on_tool_start(self, serialized, input_str, **kwargs):
        # structured tools deliver their args dict via kwargs['inputs']; fall back
        # to the raw input string for string tools.
        tool_input = kwargs.get("inputs")
        if not isinstance(tool_input, dict):
            tool_input = input_str
        try:
            request = self._extract(serialized or {}, tool_input)
        except Exception:
            request = None
        if not request:
            return
        # raises PaymentBlocked on a STOP or a declined HOLD -> aborts the tool.
        guard_payment(request, engine=self._engine, mode=self._mode,
                      confirm=self._confirm, fail_open=self._fail_open)
