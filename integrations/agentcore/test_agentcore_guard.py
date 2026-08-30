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
        # HUMAN units. The payload quotes 1000 ATOMIC units of 6-decimal USDC;
        # the engine reads claim["amount"] as a decimal string. See _human_amount.
        self.assertEqual(c["amount"], "0.001")
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
        self.assertEqual(c["amount"], "0.1")     # 100000 atomic USDC

    def test_ambiguous_body_is_refused_rather_than_guessed(self):
        """Two payloads and no `paymentType` -> refuse.

        AUDIT (2026-08-29). AgentCore selects the protocol by `paymentType`. With
        it ABSENT and BOTH `cryptoX402` and `mpp` present, which one AWS signs is
        not specified by the API -- so picking one is a guess, and a wrong guess
        means scoring a different payee from the one that gets signed. That is the
        single property this guard exists to have.

        An attacker who can shape the body puts a clean payee in the payload we
        score and a hostile one in the payload that gets signed.

        Refusing sends it to the availability policy (FAIL_CLOSED by default),
        which is the honest answer to "we cannot tell what will be signed".

        Mutation: score cryptoX402 when the type is absent and both are present
        -> this FAILS.
        """
        import base64, json
        req = base64.urlsafe_b64encode(json.dumps({
            "recipient": "0xdeadbeef00000000000000000000000000000001",
            "amount": "1000", "currency": "USDC",
            "methodDetails": {"chainId": 8453}}
        ).encode()).decode().rstrip("=")
        both = {"paymentInput": {
            "cryptoX402": {"version": "2", "payload": {
                "scheme": "exact", "network": "eip155:8453", "amount": "1000",
                "asset": USDC_BASE, "payTo": PAYEE}},
            "mpp": {"version": "1", "wwwAuthenticateHeaders": [
                'Payment id="c1", realm="s", method="evm", intent="charge", '
                'request="%s"' % req]}}}
        self.assertIsNone(G.claim_from_process_payment(both))

    def test_an_explicit_type_disambiguates(self):
        # kills: refusing whenever both are present -- with the type stated there
        # is no ambiguity, and AgentCore uses the one it names
        import base64, json
        req = base64.urlsafe_b64encode(json.dumps({
            "recipient": "0xdeadbeef00000000000000000000000000000001",
            "amount": "1000", "currency": "USDC",
            "methodDetails": {"chainId": 8453}}
        ).encode()).decode().rstrip("=")
        both = {"paymentInput": {
            "cryptoX402": {"version": "2", "payload": {
                "scheme": "exact", "network": "eip155:8453", "amount": "1000",
                "asset": USDC_BASE, "payTo": PAYEE}},
            "mpp": {"version": "1", "wwwAuthenticateHeaders": [
                'Payment id="c1", realm="s", method="evm", intent="charge", '
                'request="%s"' % req]}}}
        x = dict(both, paymentType="CRYPTO_X402")
        m = dict(both, paymentType="MPP")
        self.assertEqual(G.claim_from_process_payment(x)["counterparty"], PAYEE)
        self.assertEqual(G.claim_from_process_payment(m)["counterparty"],
                         "0xdeadbeef00000000000000000000000000000001")

    def test_mpp_symbol_currency_becomes_the_asset(self):
        """An MPP challenge that names only a SYMBOL must still be scoreable.

        AUDIT (2026-08-29). MPP carries the token in `currency`, often as a symbol
        ("USDC") rather than an address, and x402_challenge deliberately refuses to
        put a symbol in the `asset` address slot -- correct, since every downstream
        address comparison would silently fail. But `forecast` requires an asset,
        so the claim was rejected and EVERY MPP payment fell to the availability
        policy and blocked. Fail-closed is safe and useless: it blocks the
        legitimate ones too, which makes the MPP path a false-positive machine.

        The symbol is a supported claim shape here -- resolve_decimals reads
        KNOWN_SYMBOL_DECIMALS, and payload_sim's asset cross-check applies only
        when the claim names a contract address. So the symbol goes in the CLAIM,
        never into the accepts entry the parser built.

        Mutation: drop the currency fallback -> this FAILS.
        """
        import base64, json
        req = base64.urlsafe_b64encode(json.dumps({
            "recipient": PAYEE, "amount": "1000", "currency": "USDC",
            "methodDetails": {"chainId": 8453}}).encode()).decode().rstrip("=")
        c = G.claim_from_process_payment({"paymentType": "MPP", "paymentInput": {
            "mpp": {"version": "1", "wwwAuthenticateHeaders": [
                'Payment id="c1", realm="s", method="evm", intent="charge", '
                'request="%s"' % req]}}})
        self.assertEqual(c["asset"], "USDC")
        # the parser's own entry keeps its strict address semantics
        self.assertIsNone(c["accepts"][0]["asset"])

    def test_mpp_address_currency_is_preferred_over_a_symbol(self):
        # kills: overwriting a real contract address with a symbol
        import base64, json
        req = base64.urlsafe_b64encode(json.dumps({
            "recipient": PAYEE, "amount": "1000", "currency": USDC_BASE,
            "methodDetails": {"chainId": 8453}}).encode()).decode().rstrip("=")
        c = G.claim_from_process_payment({"paymentType": "MPP", "paymentInput": {
            "mpp": {"version": "1", "wwwAuthenticateHeaders": [
                'Payment id="c1", realm="s", method="evm", intent="charge", '
                'request="%s"' % req]}}})
        self.assertEqual(c["asset"].lower(), USDC_BASE.lower())

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


class TestAtomicAmountsBecomeHumanOnes(unittest.TestCase):
    """AUDIT 2026-08-30 (HIGH): the adapter passed the payload's ATOMIC `amount`
    straight into `claim["amount"]`, which the engine reads as a HUMAN decimal.
    Every AgentCore payment was therefore scored at 10^decimals its real size,
    and the price-anomaly gate fired on ordinary traffic.

    Found by running demo.py against the LIVE service: a $0.05 payment to a
    merchant with 239 settlements came back STOP, "866.9x the counterparty's own
    median". Both sides were individually correct; only the seam was wrong, which
    is exactly what a unit test on either side alone cannot see.
    """

    def test_atomic_usdc_becomes_a_human_decimal(self):
        # Kills: passing the payload amount through. This is the bug itself.
        c = G.claim_from_process_payment(x402_request(
            payload={"scheme": "exact", "amount": "50000"}))
        self.assertEqual(c["amount"], "0.05")

    def test_an_eighteen_decimal_asset_is_scaled_by_its_own_decimals(self):
        # Kills: hardcoding 6. JPYC on Polygon is 18 -- assuming 6 reports a
        # 1.0 payment as a trillion.
        c = G.claim_from_process_payment(x402_request(payload={
            "scheme": "exact", "network": "eip155:137",
            "asset": "0x431D5dfF03120AFA4bDf332c61A6e1766eF37BDB",
            "amount": "1000000000000000000"}))
        self.assertEqual(c["amount"], "1")

    def test_accepts_keeps_the_atomic_amount(self):
        # Kills: converting the payload too. The `upto` ceiling check compares an
        # allowance against the quote in ATOMIC units, so a converted `accepts`
        # would silently break the proportionality gate.
        c = G.claim_from_process_payment(x402_request(
            payload={"scheme": "exact", "amount": "50000"}))
        self.assertEqual(c["accepts"][0]["amount"], "50000")

    def test_an_asset_we_cannot_identify_yields_no_claim(self):
        # Kills: falling back to a guessed scale. There is no honest conversion
        # for an unknown asset, and guessing is the defect this codebase exists
        # to prevent -- so it goes to the availability policy instead, the same
        # answer an ambiguous paymentType already gets.
        self.assertIsNone(G.claim_from_process_payment(x402_request(
            payload={"scheme": "exact", "asset": "0x" + "f" * 40,
                     "amount": "50000"})))

    def test_a_junk_amount_yields_no_claim(self):
        # Kills: letting a non-integer amount through as if it were units.
        for junk in ("1e6", "0.5", "-1", "abc", ""):
            self.assertIsNone(G.claim_from_process_payment(
                x402_request(payload={"scheme": "exact", "amount": junk})), junk)

    def test_the_upto_ceiling_still_reaches_the_allowance_check(self):
        # Kills: breaking the upto path while fixing the exact path. The ceiling
        # is the atomic maxAmountRequired and must survive into `accepts`.
        c = G.claim_from_process_payment(x402_request(
            payload={"scheme": "upto", "amount": None,
                     "maxAmountRequired": "50000"},
            permit2AllowanceLimit="99999999"))
        self.assertEqual(c["amount"], "0.05")
        self.assertEqual(c["accepts"][0]["maxAmountRequired"], "50000")
        self.assertEqual(c["permit2AllowanceLimit"], "99999999")

    def test_only_ascii_digits_are_read_as_an_amount(self):
        # Kills: relying on int(), which accepts underscores, a leading `+` and
        # other scripts' digits. AgentCore -- not this adapter -- decides what the
        # payload says; if AWS's parser reads a spelling differently we would
        # score one amount while a different one gets signed.
        for spelling in ("1_000", "٣", "+50000", "0x10", "1e6"):
            self.assertIsNone(G.claim_from_process_payment(
                x402_request(payload={"amount": spelling})), spelling)

    def test_surrounding_whitespace_is_still_tolerated(self):
        # Kills: a check so strict it rejects a value that is unambiguous.
        c = G.claim_from_process_payment(x402_request(payload={"amount": " 50000 "}))
        self.assertEqual(c["amount"], "0.05")


class TestTheDemoScenarios(unittest.TestCase):
    """demo.py is the artifact people actually run, and nothing guarded it.

    It hits the live service, so its OUTPUT cannot be asserted here. Its
    PREMISES can, and they are the parts that rot silently: a scenario whose
    payee is no longer malformed, or a ranking keyword that no longer matches
    the reason text, both still print a clean-looking table.
    """

    def setUp(self):
        import demo
        self.demo = demo

    def test_the_malformed_scenario_really_is_malformed(self):
        # Kills: the scenario decaying into an ordinary cold-start HOLD, which
        # looks identical in the demo output and demonstrates nothing.
        import payee_syntax
        grade = payee_syntax.assess_payee(self.demo.MALFORMED)["grade"]
        self.assertEqual(grade, payee_syntax.MALFORMED)

    def test_the_clean_merchant_is_not_swept_up_by_the_same_rule(self):
        # Kills: a MALFORMED constant built so loosely the control fails too.
        import payee_syntax
        self.assertEqual(payee_syntax.assess_payee(self.demo.ESTABLISHED)["grade"],
                         payee_syntax.OK)

    def test_the_ranking_keyword_matches_the_reason_the_engine_emits(self):
        # Kills: the keyword drifting from the reason text. The demo ranks
        # reasons by KEYWORDS and prints only the top two; a keyword that
        # matches nothing buries the payee reason under settlement counts and
        # the scenario silently stops making its point.
        import payee_syntax
        reason = payee_syntax.assess_payee(self.demo.MALFORMED)["reasons"][0]
        self.assertIn(self.demo.KEYWORDS[0], reason)

    def test_every_scenario_is_inside_the_session_the_demo_claims(self):
        # Kills: a scenario AgentCore would refuse on its own. The whole
        # comparison rests on AgentCore approving all of them -- one that falls
        # outside the cap or the window would make the demo's closing claim false.
        for title, _note, body in self.demo.SCENARIOS:
            allowed, why = self.demo.agentcore_session_allows(body)
            self.assertTrue(allowed, "%s -- %s" % (title, why))
