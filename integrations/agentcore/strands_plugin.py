#!/usr/bin/env python3
"""
strands_plugin.py -- Blackwall in front of AgentCore Payments on Strands Agents.

AgentCore ships `AgentCorePaymentsPlugin`, a hook-based plugin that intercepts a
402, calls `ProcessPayment`, and retries with the proof. It checks the session
budget and signs. It never looks at the payee.

This wraps that call. Point it at your existing PaymentManager and it gates the
`process_payment` you already make -- GO calls through, HOLD asks, STOP withholds
so no proof is ever generated.

Thin by design: the decision logic is in agentcore_guard.py, which imports
nothing beyond the stdlib. This file is the only part that knows about Strands.

    from blackwall_strands import BlackwallPaymentsPlugin
    plugin = BlackwallPaymentsPlugin(manager, guard=AgentCoreGuard(decider))
    agent = Agent(tools=[http_request], plugins=[plugin])
"""
from __future__ import annotations

from agentcore_guard import AgentCoreGuard, ENFORCE, FAIL_CLOSED, in_process_decider


class BlackwallPaymentsPlugin:
    """Gate `PaymentManager.process_payment` with a Blackwall verdict.

    `manager`  your bedrock_agentcore PaymentManager (or anything exposing
               `process_payment(**kwargs)`).
    `guard`    an AgentCoreGuard; one is built with the in-process decider when
               omitted.
    `on_hold`  called with the Decision when a payment needs human confirmation.
               Return True to proceed. Default refuses -- a HOLD that nobody
               answers is not an approval.
    """

    def __init__(self, manager, *, guard=None, on_hold=None,
                 mode=ENFORCE, on_unreachable=FAIL_CLOSED):
        self.manager = manager
        self.guard = guard or AgentCoreGuard(
            in_process_decider(), mode=mode, on_unreachable=on_unreachable)
        self.on_hold = on_hold or (lambda decision: False)
        self.last_decision = None

    def process_payment(self, **kwargs):
        """Drop-in for PaymentManager.process_payment, gated.

        Returns the AgentCore response on GO (or an approved HOLD). On STOP it
        raises `PaymentBlocked` -- raising rather than returning None so a caller
        that forgets to check cannot mistake a refusal for a failed payment and
        retry it.
        """
        decision = self.guard.decide(kwargs)
        self.last_decision = decision
        approved = False
        if decision.action == "confirm":
            approved = bool(self.on_hold(decision))
        result = self.guard.process(
            kwargs, lambda body: self.manager.process_payment(**body),
            approved=approved)
        if not result.processed:
            raise PaymentBlocked(decision)
        return result.response


class PaymentBlocked(Exception):
    """Raised when Blackwall withheld a ProcessPayment call."""

    def __init__(self, decision):
        self.decision = decision
        reason = (decision.reasons or ["blocked"])[0]
        super().__init__("Blackwall %s: %s" % (decision.verdict or "unavailable",
                                               reason))
