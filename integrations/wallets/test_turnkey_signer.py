"""Tests for turnkey_signer.py -- the Turnkey adapter maps a signing request into
the shared gate. Run:  python -m unittest test_turnkey_signer.py"""
import unittest

import wallet_guard as W
from turnkey_signer import TurnkeyGuard, turnkey_extract

GOOD = "0x" + "a" * 40
BAD = "0x" + "b" * 40
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _transfer(to, amt):
    return "0xa9059cbb" + (bytes(12) + bytes.fromhex(to[2:])).hex() \
        + amt.to_bytes(32, "big").hex()


class _Src:
    def lookup(self, cp):
        cp = cp.lower()
        if cp == BAD:
            return {"sanctioned": True, "settlement_count": 5}
        if cp == GOOD:
            return {"settlement_count": 1240, "confirmed_settlement_count": 1240,
                    "distinct_payers": 400, "dispute_rate": 0.0,
                    "price_history": ["0.09"] * 20}
        return {"settlement_count": 0}


class TestTurnkeyExtract(unittest.TestCase):
    def test_reads_transaction(self):
        req = {"parameters": {"signWith": GOOD},
               "transaction": {"to": USDC, "value": 0, "data": _transfer(GOOD, 90000)}}
        p = turnkey_extract(req)
        self.assertEqual(p["counterparty"], GOOD)

    def test_reads_payment_authorization(self):
        p = turnkey_extract({"payment_authorization": "b64", "counterparty": GOOD,
                             "amount": "0.09", "asset": "USDC", "chain": "base"})
        self.assertEqual(p["payment_authorization"], "b64")

    def test_unparseable_is_empty(self):
        self.assertEqual(turnkey_extract({"foo": "bar"}), {})
        self.assertEqual(turnkey_extract(None), {})


class TestTurnkeyGuard(unittest.TestCase):
    def _tk(self, **kw):
        guard = W.WalletGuard(W.in_process_decider(_Src()), **kw)
        self.calls = []
        return TurnkeyGuard(guard, sign_fn=lambda req: self.calls.append(req) or "0xTKSIG")

    def _req(self, to):
        return {"transaction": {"to": USDC, "value": 0, "data": _transfer(to, 90000)}}

    def test_good_signs(self):
        r = self._tk().sign(self._req(GOOD))
        self.assertTrue(r.signed)
        self.assertEqual(r.signature, "0xTKSIG")
        self.assertEqual(len(self.calls), 1)             # Turnkey signer invoked once

    def test_sanctioned_withheld(self):
        r = self._tk().sign(self._req(BAD))
        self.assertFalse(r.signed)
        self.assertEqual(self.calls, [])                 # signer NOT invoked
        self.assertEqual(r.decision.verdict, "STOP")

    def test_unparseable_request_uses_toggle(self):
        # a request the extractor can't map -> verdict unavailable -> toggle governs
        closed = self._tk(on_unreachable=W.FAIL_CLOSED)
        self.assertFalse(closed.sign({"weird": 1}).signed)
        opened = self._tk(on_unreachable=W.FAIL_OPEN)
        self.assertTrue(opened.sign({"weird": 1}).signed)


if __name__ == "__main__":
    unittest.main()
