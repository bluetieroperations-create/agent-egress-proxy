#!/usr/bin/env python3
"""
langgraph_middleware.py -- Blackwall in front of AgentCore Payments on LangGraph.

AgentCore ships `AgentCorePaymentsMiddleware`, which wraps tool calls and handles
402s by calling `ProcessPayment`. Same shape as the Strands plugin and the same
blind spot: it enforces the session budget and never evaluates the payee.

This wraps the ProcessPayment call the middleware makes. Thin by design -- the
decision logic lives in agentcore_guard.py (stdlib only); this file is the only
part that knows about LangGraph.

    payments = AgentCorePaymentsMiddleware(config)
    guarded  = BlackwallPaymentsMiddleware(payments, guard=AgentCoreGuard(decider))
    agent    = create_agent(model=..., middleware=[guarded])
"""
from __future__ import annotations

from agentcore_guard import AgentCoreGuard, ENFORCE, FAIL_CLOSED, in_process_decider
from strands_plugin import PaymentBlocked


class BlackwallPaymentsMiddleware:
    """Wrap an AgentCore payments middleware so every ProcessPayment is gated.

    Delegates everything it does not gate, so it can be dropped in front of the
    real middleware without reimplementing it.
    """

    def __init__(self, inner, *, guard=None, on_hold=None,
                 mode=ENFORCE, on_unreachable=FAIL_CLOSED):
        self.inner = inner
        self.guard = guard or AgentCoreGuard(
            in_process_decider(), mode=mode, on_unreachable=on_unreachable)
        self.on_hold = on_hold or (lambda decision: False)
        self.last_decision = None

    def process_payment(self, body):
        decision = self.guard.decide(body)
        self.last_decision = decision
        approved = decision.action == "confirm" and bool(self.on_hold(decision))
        result = self.guard.process(body, self.inner.process_payment,
                                    approved=approved)
        if not result.processed:
            raise PaymentBlocked(decision)
        return result.response

    def __getattr__(self, name):
        # Anything we do not gate belongs to the wrapped middleware. Defined
        # explicitly rather than by subclassing so the gated surface stays
        # visible in this file.
        return getattr(self.inner, name)
