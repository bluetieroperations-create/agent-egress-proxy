"""
Tests for blackwall_guard.py -- the LangChain-free core. Stdlib only (no langchain
needed). Each test states the mutation it kills.

Run from this directory:  python -m unittest test_blackwall_guard.py
"""
import unittest

import blackwall_guard as G


class _FakeEngine:
    def __init__(self, verdict=None, exc=None):
        self.verdict, self.exc, self.seen = verdict, exc, None

    def forecast(self, request):
        self.seen = request
        if self.exc:
            raise self.exc
        return self.verdict


def _v(verdict, **o):
    return dict({"verdict": verdict, "hard_stop": verdict == "STOP",
                 "score": 1.0, "reasons": ["r"], "receipt_id": "bw_x"}, **o)


class TestDecide(unittest.TestCase):
    """
    Mutation notes:
      - GO not -> allow / STOP not -> block(enforce) / HOLD not -> confirm(enforce)
        each FAILS its line below.
      - observe ever blocking -> test_observe_never_blocks FAILS.
      - unknown verdict auto-allowed -> test_unknown_is_conservative FAILS.
    """
    def test_enforce_mapping(self):
        self.assertEqual(G.decide(_v("GO"), G.ENFORCE).action, G.ALLOW)
        self.assertEqual(G.decide(_v("HOLD"), G.ENFORCE).action, G.CONFIRM)
        self.assertEqual(G.decide(_v("STOP"), G.ENFORCE).action, G.BLOCK)

    def test_observe_never_blocks(self):
        for verdict in ("GO", "HOLD", "STOP"):
            self.assertEqual(G.decide(_v(verdict), G.OBSERVE).action, G.ALLOW)

    def test_unknown_is_conservative(self):
        # a missing/garbage verdict must NOT auto-allow under enforce
        self.assertEqual(G.decide({}, G.ENFORCE).action, G.CONFIRM)
        self.assertEqual(G.decide({"verdict": "???"}, G.ENFORCE).action, G.CONFIRM)

    def test_carries_fields(self):
        d = G.decide(_v("STOP"), G.ENFORCE)
        self.assertTrue(d.hard_stop)
        self.assertEqual(d.receipt_id, "bw_x")
        self.assertFalse(d.allowed)
        self.assertTrue(G.decide(_v("GO")).allowed)


class TestCheckPaymentFailSafe(unittest.TestCase):
    """Mutation note: fail OPEN on engine error under enforce -> these FAIL."""
    def test_engine_error_enforces_confirm(self):
        d = G.check_payment({"counterparty": "0x1"},
                            engine=_FakeEngine(exc=G.ForecastError("boom")),
                            mode=G.ENFORCE)
        self.assertEqual(d.action, G.CONFIRM)      # never a silent allow
        self.assertTrue(any("unavailable" in r for r in d.reasons))

    def test_engine_error_observe_allows(self):
        d = G.check_payment({}, engine=_FakeEngine(exc=ValueError("x")),
                            mode=G.OBSERVE)
        self.assertEqual(d.action, G.ALLOW)

    def test_fail_open_allows(self):
        d = G.check_payment({}, engine=_FakeEngine(exc=ValueError("x")),
                            mode=G.ENFORCE, fail_open=True)
        self.assertEqual(d.action, G.ALLOW)

    def test_happy_path_passes_request(self):
        eng = _FakeEngine(verdict=_v("GO"))
        d = G.check_payment({"counterparty": "0xabc", "amount": "1"}, engine=eng)
        self.assertEqual(d.action, G.ALLOW)
        self.assertEqual(eng.seen["counterparty"], "0xabc")


class TestGuardPayment(unittest.TestCase):
    """
    Mutation notes:
      - not raising on BLOCK -> test_block_raises FAILS.
      - allowing a CONFIRM without asking -> test_confirm_required FAILS.
    """
    def test_allow_returns_decision(self):
        d = G.guard_payment({}, engine=_FakeEngine(verdict=_v("GO")))
        self.assertEqual(d.action, G.ALLOW)

    def test_block_raises(self):
        with self.assertRaises(G.PaymentBlocked):
            G.guard_payment({}, engine=_FakeEngine(verdict=_v("STOP")))

    def test_confirm_required(self):
        eng = _FakeEngine(verdict=_v("HOLD"))
        # no confirm callback -> declined -> blocked
        with self.assertRaises(G.PaymentBlocked):
            G.guard_payment({}, engine=eng)
        # confirm approves -> proceeds
        d = G.guard_payment({}, engine=eng, confirm=lambda dec: True)
        self.assertEqual(d.action, G.CONFIRM)
        # confirm declines -> blocked
        with self.assertRaises(G.PaymentBlocked):
            G.guard_payment({}, engine=eng, confirm=lambda dec: False)

    def test_observe_never_raises(self):
        d = G.guard_payment({}, engine=_FakeEngine(verdict=_v("STOP")),
                            mode=G.OBSERVE)
        self.assertEqual(d.action, G.ALLOW)


class TestHttpEngine(unittest.TestCase):
    """Mutation notes: treat 402/non-200 as success -> these FAIL."""
    def test_success(self):
        eng = G.HttpEngine("https://bw.example",
                           _transport=lambda u, d, h, t: (200, _v("GO")))
        self.assertEqual(eng.forecast({"counterparty": "0x1"})["verdict"], "GO")

    def test_402_raises_payment_required(self):
        eng = G.HttpEngine("https://x", _transport=lambda *a: (402, {}))
        with self.assertRaises(G.PaymentRequired):
            eng.forecast({})

    def test_non_200_raises(self):
        eng = G.HttpEngine("https://x",
                           _transport=lambda *a: (400, {"error": "bad"}))
        with self.assertRaises(G.ForecastError):
            eng.forecast({})

    def test_sends_json_body_and_headers(self):
        seen = {}
        def cap(url, data, headers, timeout):
            seen["url"] = url; seen["headers"] = headers; seen["data"] = data
            return 200, _v("GO")
        G.HttpEngine("https://x/", headers={"X-API-KEY": "k"},
                     _transport=cap).forecast({"counterparty": "0xa"})
        self.assertTrue(seen["url"].endswith("/v1/forecast-payment"))
        self.assertEqual(seen["headers"]["X-API-KEY"], "k")
        self.assertIn(b"counterparty", seen["data"])


class TestInProcessEngine(unittest.TestCase):
    """The real engine end-to-end via blackwall.forecast (no network)."""
    def test_known_good_go_and_sanctioned_stop(self):
        eng = G.InProcessEngine()
        go = eng.forecast({"counterparty": "0xKNOWNGOOD000000000000000000000000000001",
                           "amount": "0.09", "asset": "USDC", "chain": "base"})
        self.assertEqual(go["verdict"], "GO")
        stop = eng.forecast({"counterparty": "0xSANCTIONED00000000000000000000000000003",
                             "amount": "0.09", "asset": "USDC", "chain": "base"})
        self.assertEqual(stop["verdict"], "STOP")

    def test_bad_request_raises_forecast_error(self):
        with self.assertRaises(G.ForecastError):
            G.InProcessEngine().forecast({"amount": "0.09"})   # missing counterparty


if __name__ == "__main__":
    unittest.main()
