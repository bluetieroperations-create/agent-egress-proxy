"""
Tests for langchain_blackwall.py -- the LangChain adapter. Gated on langchain-core
(skips cleanly without it); the core logic is covered by test_blackwall_guard.py.

Run:  python -m unittest test_langchain_blackwall.py
"""
import unittest

import blackwall_guard as G

try:
    from langchain_core.tools import tool
    import langchain_blackwall as LB
    _HAVE_LC = True
except Exception:  # pragma: no cover
    _HAVE_LC = False

GOOD = "0xKNOWNGOOD000000000000000000000000000001"
SANCTIONED = "0xSANCTIONED00000000000000000000000000003"


class _FakeEngine:
    def __init__(self, verdict):
        self._v = verdict

    def forecast(self, request):
        return self._v


def _v(verdict):
    return {"verdict": verdict, "hard_stop": verdict == "STOP", "score": 1.0,
            "reasons": ["counterparty is on a sanctions list"] if verdict == "STOP"
            else ["ok"], "receipt_id": "bw_x"}


@unittest.skipUnless(_HAVE_LC, "langchain-core not installed")
class TestPaymentGuardTool(unittest.TestCase):
    def test_go_renders_allow(self):
        t = LB.BlackwallPaymentGuardTool(engine=_FakeEngine(_v("GO")))
        out = t.invoke({"counterparty": GOOD, "amount": "0.09"})
        self.assertIn("GO", out)
        self.assertIn("proceed", out.lower())

    def test_stop_renders_block(self):
        t = LB.BlackwallPaymentGuardTool(engine=_FakeEngine(_v("STOP")))
        out = t.invoke({"counterparty": SANCTIONED, "amount": "0.09"})
        self.assertIn("STOP", out)
        self.assertIn("do not", out.lower())

    def test_real_inprocess_engine(self):
        t = LB.BlackwallPaymentGuardTool(engine=G.InProcessEngine())
        self.assertIn("GO", t.invoke({"counterparty": GOOD, "amount": "0.09",
                                      "asset": "USDC", "chain": "base"}))
        self.assertIn("STOP", t.invoke({"counterparty": SANCTIONED, "amount": "0.09",
                                        "asset": "USDC", "chain": "base"}))


@unittest.skipUnless(_HAVE_LC, "langchain-core not installed")
class TestGuardrailCallback(unittest.TestCase):
    def _pay_tool(self):
        @tool
        def pay(counterparty: str, amount: str) -> str:
            """Send a USDC payment."""
            return "PAID %s to %s" % (amount, counterparty)
        return pay

    def _extract(self, serialized, ti):
        if (serialized or {}).get("name") != "pay" or not isinstance(ti, dict):
            return None
        return {"counterparty": ti.get("counterparty"), "amount": ti.get("amount"),
                "asset": "USDC", "chain": "base"}

    def test_blocks_sanctioned_payment(self):
        pay = self._pay_tool()
        cb = LB.BlackwallGuardrailCallback(engine=G.InProcessEngine(),
                                           extract=self._extract, mode=G.ENFORCE)
        with self.assertRaises(G.PaymentBlocked):
            pay.invoke({"counterparty": SANCTIONED, "amount": "0.09"},
                       config={"callbacks": [cb]})

    def test_allows_good_payment(self):
        pay = self._pay_tool()
        cb = LB.BlackwallGuardrailCallback(engine=G.InProcessEngine(),
                                           extract=self._extract, mode=G.ENFORCE)
        out = pay.invoke({"counterparty": GOOD, "amount": "0.09"},
                         config={"callbacks": [cb]})
        self.assertIn("PAID", out)

    def test_hold_confirm_declined_blocks(self):
        pay = self._pay_tool()
        cb = LB.BlackwallGuardrailCallback(engine=G.InProcessEngine(),
                                           extract=self._extract, mode=G.ENFORCE,
                                           confirm=lambda d: False)
        with self.assertRaises(G.PaymentBlocked):     # unknown counterparty -> HOLD
            pay.invoke({"counterparty": "0x" + "1" * 40, "amount": "0.09"},
                       config={"callbacks": [cb]})

    def test_non_payment_tool_passes_through(self):
        @tool
        def search(q: str) -> str:
            """Search."""
            return "results"
        cb = LB.BlackwallGuardrailCallback(engine=G.InProcessEngine(),
                                           extract=self._extract, mode=G.ENFORCE)
        self.assertEqual(search.invoke({"q": "hi"}, config={"callbacks": [cb]}),
                         "results")

    def test_observe_mode_never_blocks(self):
        pay = self._pay_tool()
        cb = LB.BlackwallGuardrailCallback(engine=G.InProcessEngine(),
                                           extract=self._extract, mode=G.OBSERVE)
        out = pay.invoke({"counterparty": SANCTIONED, "amount": "0.09"},
                         config={"callbacks": [cb]})
        self.assertIn("PAID", out)


if __name__ == "__main__":
    unittest.main()
