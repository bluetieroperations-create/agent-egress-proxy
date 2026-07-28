"""
Tests for calldata.py -- Phase 3 contract-call screening. Selectors are anchored
to the published 4byte database; each test states the mutation it kills.
"""
import unittest

import calldata as C

CP = "0x" + "a" * 40      # the counterparty being paid
SP = "0x" + "d" * 40      # some other address (spender / wrong recipient)
MAX_UINT = (1 << 256) - 1


def _word_addr(a):
    return bytes(12) + bytes.fromhex(a[2:])


def _word_uint(n):
    return n.to_bytes(32, "big")


def _call(sel, *words):
    return sel + b"".join(words).hex()


class TestSelectors(unittest.TestCase):
    """Mutation note: a wrong keccak/selector -> these published values FAIL."""
    def test_published_selectors(self):
        self.assertEqual(C.selector("approve(address,uint256)"), "0x095ea7b3")
        self.assertEqual(C.selector("transfer(address,uint256)"), "0xa9059cbb")
        self.assertEqual(C.selector("transferFrom(address,address,uint256)"),
                         "0x23b872dd")
        self.assertEqual(C.selector("setApprovalForAll(address,bool)"), "0xa22cb465")
        self.assertEqual(
            C.selector("permit(address,address,uint256,uint256,uint8,bytes32,bytes32)"),
            "0xd505accf")


class TestDecode(unittest.TestCase):
    def test_decode_approve(self):
        d = C.decode_call(_call("0x095ea7b3", _word_addr(SP), _word_uint(500)))
        self.assertEqual(d["name"], "approve")
        self.assertTrue(C.addresses_equal(d["args"]["spender"], SP))
        self.assertEqual(d["args"]["amount"], 500)

    def test_decode_transferFrom(self):
        d = C.decode_call(_call("0x23b872dd", _word_addr(SP), _word_addr(CP),
                                _word_uint(7)))
        self.assertEqual(d["name"], "transferFrom")
        self.assertTrue(C.addresses_equal(d["args"]["to"], CP))
        self.assertEqual(d["args"]["amount"], 7)

    def test_unknown_selector(self):
        d = C.decode_call("0xdeadbeef" + "00" * 32)
        self.assertIsNone(d["name"])
        self.assertEqual(d["selector"], "0xdeadbeef")

    def test_too_short_is_none(self):
        self.assertIsNone(C.decode_call("0x0102"))
        self.assertIsNone(C.decode_call(None))
        self.assertIsNone(C.decode_call("0xnothex"))

    def test_truncated_args(self):
        d = C.decode_call("0x095ea7b3")           # approve, no args
        self.assertTrue(d.get("truncated"))


class TestScreenCritical(unittest.TestCase):
    """
    Mutation notes (each drainer must hard-STOP):
      - don't flag unlimited approve -> test_unlimited_approve FAILS.
      - don't flag setApprovalForAll -> test_set_approval_for_all FAILS.
      - drop the transfer recipient check -> test_transfer_wrong_recipient FAILS.
    """
    def _screen(self, data):
        return C.screen_transaction({"counterparty": CP},
                                    {"to": "0x" + "f" * 40, "data": data})

    def test_unlimited_approve(self):
        r = self._screen(_call("0x095ea7b3", _word_addr(SP), _word_uint(MAX_UINT)))
        self.assertFalse(r["matches"])
        self.assertTrue(any("UNLIMITED" in m for m in r["mismatches"]))

    def test_unlimited_permit(self):
        data = _call("0xd505accf", _word_addr(CP), _word_addr(SP),
                     _word_uint(MAX_UINT), _word_uint(0), _word_uint(27),
                     _word_uint(0), _word_uint(0))
        r = self._screen(data)
        self.assertFalse(r["matches"])
        self.assertTrue(any("permit" in m for m in r["mismatches"]))

    def test_set_approval_for_all_true(self):
        r = self._screen(_call("0xa22cb465", _word_addr(SP), _word_uint(1)))
        self.assertFalse(r["matches"])
        self.assertTrue(any("ALL your NFTs" in m for m in r["mismatches"]))

    def test_set_approval_for_all_false_is_benign(self):
        r = self._screen(_call("0xa22cb465", _word_addr(SP), _word_uint(0)))
        self.assertTrue(r["matches"])           # revoke is not a drain

    def test_transfer_wrong_recipient(self):
        r = self._screen(_call("0xa9059cbb", _word_addr(SP), _word_uint(90000)))
        self.assertFalse(r["matches"])
        self.assertTrue(any("recipient mismatch" in m for m in r["mismatches"]))

    def test_transferFrom_wrong_recipient(self):
        r = self._screen(_call("0x23b872dd", _word_addr(SP), _word_addr(SP),
                               _word_uint(1)))
        self.assertFalse(r["matches"])

    def test_transfer_amount_mismatch_when_token_is_asset(self):
        # right recipient but INFLATED amount: score 0.09 (90000 atomic) vs 5.00
        usdc = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        data = _call("0xa9059cbb", _word_addr(CP), _word_uint(5000000))
        r = C.screen_transaction(
            {"counterparty": CP, "amount": "0.09", "asset": usdc},
            {"to": usdc, "data": data})       # tx.to == the claimed asset
        self.assertFalse(r["matches"])
        self.assertTrue(any("amount mismatch" in m for m in r["mismatches"]))

    def test_transfer_amount_match_when_token_is_asset(self):
        usdc = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        data = _call("0xa9059cbb", _word_addr(CP), _word_uint(90000))
        r = C.screen_transaction(
            {"counterparty": CP, "amount": "0.09", "asset": usdc},
            {"to": usdc, "data": data})
        self.assertTrue(r["matches"])

    def test_truncated_calldata_stops(self):
        r = self._screen("0x095ea7b3")          # approve selector, no args
        self.assertFalse(r["matches"])
        self.assertTrue(any("truncated" in m or "garbled" in m for m in r["mismatches"]))


class TestScreenBenignAndAdvisory(unittest.TestCase):
    def _screen(self, data):
        return C.screen_transaction({"counterparty": CP},
                                    {"to": "0x" + "f" * 40, "data": data})

    def test_transfer_right_recipient_ok(self):
        r = self._screen(_call("0xa9059cbb", _word_addr(CP), _word_uint(90000)))
        self.assertTrue(r["matches"])
        self.assertEqual(r["mismatches"], [])

    def test_bounded_approve_is_warning(self):
        r = self._screen(_call("0x095ea7b3", _word_addr(SP), _word_uint(1000000)))
        self.assertTrue(r["matches"])           # not a hard stop
        self.assertTrue(any("not itself a payment" in w for w in r["warnings"]))

    def test_unknown_selector_is_warning(self):
        r = self._screen("0xdeadbeef" + "00" * 32)
        self.assertTrue(r["matches"])
        self.assertTrue(any("unrecognized" in w for w in r["warnings"]))

    def test_no_transaction_is_opt_in(self):
        r = C.screen_transaction({"counterparty": CP}, None)
        self.assertFalse(r["checked"])
        self.assertTrue(r["matches"])

    def test_plain_value_transfer_no_data(self):
        # a tx with no calldata (native value transfer) -> nothing to decode, benign
        r = C.screen_transaction({"counterparty": CP}, {"to": CP, "value": "1000"})
        self.assertTrue(r["matches"])

    def test_garbage_never_raises(self):
        for tx in ({}, {"data": 123}, {"data": []}, {"data": "0xzz"}):
            r = C.screen_transaction({"counterparty": CP}, tx)
            self.assertIn("matches", r)


if __name__ == "__main__":
    unittest.main()
