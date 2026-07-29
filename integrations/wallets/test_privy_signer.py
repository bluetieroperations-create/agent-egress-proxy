"""Tests for privy_signer.py -- the Privy RPC adapter. Run:
python -m unittest test_privy_signer.py"""
import unittest

import wallet_guard as W
from privy_signer import PrivyGuard, privy_extract

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


class TestPrivyExtract(unittest.TestCase):
    def test_eth_sign_transaction(self):
        p = privy_extract({"method": "eth_signTransaction",
                           "params": [{"to": USDC, "value": 0,
                                       "data": _transfer(GOOD, 90000)}]})
        self.assertEqual(p["counterparty"], GOOD)

    def test_eth_send_transaction_input_alias(self):
        # some RPC shapes use `input` instead of `data`
        p = privy_extract({"method": "eth_sendTransaction",
                           "params": [{"to": USDC, "input": _transfer(GOOD, 90000)}]})
        self.assertEqual(p["counterparty"], GOOD)

    def test_typed_data_payment_authorization(self):
        p = privy_extract({"method": "eth_signTypedData_v4", "counterparty": GOOD,
                           "amount": "0.09", "payment_authorization": "b64"})
        self.assertEqual(p["payment_authorization"], "b64")
        self.assertEqual(p["counterparty"], GOOD)

    def test_unknown_method_empty(self):
        self.assertEqual(privy_extract({"method": "personal_sign", "params": ["x"]}), {})
        self.assertEqual(privy_extract(None), {})


class TestPrivyGuard(unittest.TestCase):
    def _pv(self, **kw):
        guard = W.WalletGuard(W.in_process_decider(_Src()), **kw)
        self.calls = []
        return PrivyGuard(guard, sign_fn=lambda req: self.calls.append(req) or "0xPVSIG")

    def _rpc(self, to, method="eth_signTransaction"):
        return {"method": method,
                "params": [{"to": USDC, "value": 0, "data": _transfer(to, 90000)}]}

    def test_good_signs(self):
        r = self._pv().sign(self._rpc(GOOD))
        self.assertTrue(r.signed)
        self.assertEqual(r.signature, "0xPVSIG")

    def test_sanctioned_withheld(self):
        r = self._pv().sign(self._rpc(BAD))
        self.assertFalse(r.signed)
        self.assertEqual(self.calls, [])

    def test_toggle_on_unparseable(self):
        self.assertFalse(self._pv(on_unreachable=W.FAIL_CLOSED)
                         .sign({"method": "personal_sign"}).signed)
        self.assertTrue(self._pv(on_unreachable=W.FAIL_OPEN)
                        .sign({"method": "personal_sign"}).signed)


if __name__ == "__main__":
    unittest.main()
