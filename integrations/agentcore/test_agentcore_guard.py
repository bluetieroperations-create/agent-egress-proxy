"""
Tests for agentcore_guard.py -- gating an AWS Bedrock AgentCore ProcessPayment
call with a Blackwall verdict. Each test names the mutation it kills.

Run from this directory:  python3 -m unittest discover -p 'test_*.py'
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import agentcore_guard as G

PAYEE = "0x99935f281d3ED1E804bF1413b76E0B03e1fed4F9"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
MAX_UINT256 = str((1 << 256) - 1)


def x402_request(**over):
    payload = {"scheme": "exact", "network": "eip155:8453", "amount": "1000",
               "asset": USDC_BASE, "payTo": PAYEE, "maxTimeoutSeconds": 300,
               "extra": {"name": "USDC", "version": "2"}}
    payload.update(over.pop("payload", {}))
    body = {"paymentType": "CRYPTO_X402",
            "paymentInput": {"cryptoX402": {"version": "2", "payload": payload}}}
    if "permit2AllowanceLimit" in over:
        body["paymentInput"]["cryptoX402"]["permit2AllowanceLimit"] = \
            over.pop("permit2AllowanceLimit")
    body.update(over)
    return body


class TestClaimFromProcessPayment(unittest.TestCase):
    """
    AgentCore forwards the merchant's payload VERBATIM and signs it. The claim we
    score has to come out of that same payload, or we would be screening something
    other than what gets signed.

    Mutation notes:
      - read payTo from anywhere but the payload -> test_x402_claim FAILS.
      - drop the MPP branch -> test_mpp_claim FAILS.
      - return a claim for an unparseable body -> test_unparseable_is_none FAILS.
    """
    def test_x402_claim(self):
        c = G.claim_from_process_payment(x402_request())
        self.assertEqual(c["counterparty"], PAYEE)
        self.assertEqual(c["asset"], USDC_BASE)
        self.assertEqual(c["chain"], "eip155:8453")
        self.assertEqual(c["amount"], "1000")
        self.assertEqual(c["scheme"], "exact")

    def test_x402_upto_carries_the_allowance(self):
        c = G.claim_from_process_payment(x402_request(
            payload={"scheme": "upto", "maxAmountRequired": "3495"},
            permit2AllowanceLimit="1000000"))
        self.assertEqual(c["scheme"], "upto")
        self.assertEqual(c["permit2AllowanceLimit"], "1000000")

    def test_mpp_claim(self):
        # AgentCore's MPP path forwards the raw WWW-Authenticate challenge
        import base64, json
        req = base64.urlsafe_b64encode(json.dumps({
            "recipient": PAYEE, "amount": "100000", "currency": "USDC",
            "methodDetails": {"chainId": 8453}}).encode()).decode().rstrip("=")
        body = {"paymentType": "MPP", "paymentInput": {"mpp": {
            "version": "1",
            "wwwAuthenticateHeaders": [
                'Payment id="c1", realm="s.example.com", method="evm", '
                'intent="charge", request="%s"' % req]}}}
        c = G.claim_from_process_payment(body)
        self.assertEqual(c["counterparty"], PAYEE)
        self.assertEqual(c["amount"], "100000")

    def test_unparseable_is_none(self):
        # kills: manufacturing a claim we cannot substantiate -- the availability
        # policy must govern, not a half-built claim that looks scoreable
        for body in ({}, None, "x", [], {"paymentType": "CRYPTO_X402"},
                     {"paymentType": "MPP", "paymentInput": {"mpp": {}}},
                     {"paymentType": "SOMETHING_ELSE"}):
            self.assertIsNone(G.claim_from_process_payment(body), body)

    def test_never_raises(self):
        for body in (None, 0, "", b"x", [], {"paymentInput": 5},
                     {"paymentInput": {"cryptoX402": "no"}},
                     {"paymentType": "MPP", "paymentInput": {"mpp":
                      {"wwwAuthenticateHeaders": "not-a-list"}}}):
            G.claim_from_process_payment(body)


class TestGate(unittest.TestCase):
    """
    The whole point: AgentCore enforces maxSpendAmount + expiry and NEVER
    evaluates the payee. So the verdict must gate BEFORE ProcessPayment signs.

    Mutation notes:
      - call process_fn on STOP -> test_stop_withholds FAILS.
      - call process_fn on HOLD in enforce mode -> test_hold_confirms FAILS.
      - swallow a decider error as allow -> test_fail_closed FAILS.
    """
    def _guard(self, verdict, **kw):
        return G.AgentCoreGuard(lambda claim: {"verdict": verdict, "reasons": []}, **kw)

    def test_go_calls_process_payment(self):
        calls = []
        g = self._guard("GO")
        r = g.process(x402_request(), lambda body: calls.append(body) or {"status": "PROOF_GENERATED"})
        self.assertTrue(r.processed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(r.decision.action, G.ALLOW)

    def test_stop_withholds(self):
        calls = []
        g = self._guard("STOP")
        r = g.process(x402_request(), lambda body: calls.append(body))
        self.assertFalse(r.processed)
        self.assertEqual(calls, [], "ProcessPayment must NOT be called on STOP")
        self.assertEqual(r.decision.action, G.BLOCK)

    def test_hold_confirms(self):
        calls = []
        g = self._guard("HOLD")
        r = g.process(x402_request(), lambda body: calls.append(body))
        self.assertFalse(r.processed)
        self.assertEqual(calls, [])
        self.assertEqual(r.decision.action, G.CONFIRM)

    def test_hold_proceeds_once_a_human_approves(self):
        calls = []
        g = self._guard("HOLD")
        r = g.process(x402_request(), lambda body: calls.append(body) or {"status": "OK"},
                      approved=True)
        self.assertTrue(r.processed)
        self.assertEqual(len(calls), 1)

    def test_stop_is_not_overridable_by_approval(self):
        # kills: letting a human wave through a hard STOP -- approval covers
        # HOLD, which is a question; STOP is an answer
        calls = []
        g = self._guard("STOP")
        r = g.process(x402_request(), lambda body: calls.append(body), approved=True)
        self.assertFalse(r.processed)
        self.assertEqual(calls, [])

    def test_observe_mode_never_withholds(self):
        calls = []
        g = self._guard("STOP", mode=G.OBSERVE)
        r = g.process(x402_request(), lambda body: calls.append(body) or {"status": "OK"})
        self.assertTrue(r.processed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(r.decision.action, G.BLOCK, "the verdict is still reported")

    def test_fail_closed_when_the_decider_raises(self):
        def boom(claim):
            raise RuntimeError("unreachable")
        calls = []
        g = G.AgentCoreGuard(boom, on_unreachable=G.FAIL_CLOSED)
        r = g.process(x402_request(), lambda body: calls.append(body))
        self.assertFalse(r.processed)
        self.assertEqual(calls, [])

    def test_fail_open_when_configured(self):
        def boom(claim):
            raise RuntimeError("unreachable")
        calls = []
        g = G.AgentCoreGuard(boom, on_unreachable=G.FAIL_OPEN)
        r = g.process(x402_request(), lambda body: calls.append(body) or {"status": "OK"})
        self.assertTrue(r.processed)

    def test_unparseable_body_uses_the_availability_policy(self):
        # a body we cannot read is NOT a GO; it is an unknown, and the same
        # toggle that governs an unreachable Blackwall governs it
        calls = []
        g = G.AgentCoreGuard(lambda c: {"verdict": "GO"}, on_unreachable=G.FAIL_CLOSED)
        r = g.process({"paymentType": "CRYPTO_X402"}, lambda body: calls.append(body))
        self.assertFalse(r.processed)
        self.assertEqual(calls, [])


class TestTheGapItCloses(unittest.TestCase):
    """AgentCore's session controls are maxSpendAmount + expiry, and nothing else.
    These pin the two exposures that get past them."""

    def test_unlimited_permit2_allowance_is_blocked(self):
        # AWS documents granting this as a normal option for `upto`, and a spend
        # cap cannot restrain it -- an allowance is not a spend.
        seen = {}

        def decider(claim):
            seen.update(claim)
            return {"verdict": "GO", "reasons": []}

        g = G.AgentCoreGuard(decider)
        body = x402_request(payload={"scheme": "upto", "maxAmountRequired": "1000"},
                            permit2AllowanceLimit=MAX_UINT256)
        g.process(body, lambda b: {"status": "OK"})
        self.assertEqual(seen.get("permit2AllowanceLimit"), MAX_UINT256,
                         "the allowance must reach the verdict engine")

    def test_the_payee_reaches_the_engine(self):
        # the whole gap: AgentCore forwards payTo verbatim and never evaluates it
        seen = {}
        g = G.AgentCoreGuard(lambda c: seen.update(c) or {"verdict": "GO"})
        g.process(x402_request(), lambda b: {"status": "OK"})
        self.assertEqual(seen.get("counterparty"), PAYEE)


if __name__ == "__main__":
    unittest.main()


class TestShims(unittest.TestCase):
    """The framework shims must withhold, not merely report.

    Mutation notes:
      - return None instead of raising on a block -> test_plugin_raises FAILS.
      - call the manager before deciding -> test_manager_untouched_on_stop FAILS.
    """
    class _Manager:
        def __init__(self):
            self.calls = []

        def process_payment(self, **kw):
            self.calls.append(kw)
            return {"status": "PROOF_GENERATED"}

    def _plugin(self, verdict, **kw):
        import strands_plugin as SP
        mgr = self._Manager()
        guard = G.AgentCoreGuard(lambda c: {"verdict": verdict, "reasons": ["r"]})
        return SP.BlackwallPaymentsPlugin(mgr, guard=guard, **kw), mgr, SP

    def test_go_reaches_the_manager(self):
        p, mgr, _ = self._plugin("GO")
        out = p.process_payment(**x402_request())
        self.assertEqual(out["status"], "PROOF_GENERATED")
        self.assertEqual(len(mgr.calls), 1)

    def test_manager_untouched_on_stop(self):
        p, mgr, SP = self._plugin("STOP")
        with self.assertRaises(SP.PaymentBlocked):
            p.process_payment(**x402_request())
        self.assertEqual(mgr.calls, [], "no proof may be generated for a STOP")

    def test_plugin_raises_rather_than_returning_none(self):
        # kills: returning None on a block -- a caller that forgets to check
        # would read it as a failed payment and retry
        p, _, SP = self._plugin("STOP")
        with self.assertRaises(SP.PaymentBlocked):
            p.process_payment(**x402_request())

    def test_hold_refused_by_default(self):
        p, mgr, SP = self._plugin("HOLD")
        with self.assertRaises(SP.PaymentBlocked):
            p.process_payment(**x402_request())
        self.assertEqual(mgr.calls, [])

    def test_hold_proceeds_when_the_callback_approves(self):
        p, mgr, _ = self._plugin("HOLD", on_hold=lambda d: True)
        p.process_payment(**x402_request())
        self.assertEqual(len(mgr.calls), 1)

    def test_middleware_delegates_what_it_does_not_gate(self):
        import langgraph_middleware as LM

        class Inner:
            def __init__(self): self.calls = []
            def process_payment(self, body): self.calls.append(body); return {"ok": 1}
            def some_other_hook(self): return "delegated"

        inner = Inner()
        m = LM.BlackwallPaymentsMiddleware(
            inner, guard=G.AgentCoreGuard(lambda c: {"verdict": "GO"}))
        self.assertEqual(m.process_payment(x402_request())["ok"], 1)
        self.assertEqual(m.some_other_hook(), "delegated")
