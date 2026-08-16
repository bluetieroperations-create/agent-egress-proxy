#!/usr/bin/env python3
"""
test_rwa_balance.py -- TDD for the settlement-held balance reader. Each test names the
MUTATION it kills.
"""
import unittest

from rwa_balance import BalanceReader
from solana_rwa import b58encode

EVM_TOKEN = "0x" + "22" * 20
EVM_PAYER = "0x" + "11" * 20
SOL_TOKEN = b58encode(bytes([1]) * 32)
SOL_PAYER = b58encode(bytes([2]) * 32)

WORD = lambda n: "0x" + format(n, "064x")


class TestEvm(unittest.TestCase):
    def test_positive_balance_true(self):
        r = BalanceReader(eth_call=lambda to, data: WORD(500))
        self.assertIs(r(EVM_TOKEN, "base", EVM_PAYER), True)

    def test_zero_balance_false(self):
        # MUTATION: a real 0 balance must read False (not None) -- the read succeeded.
        r = BalanceReader(eth_call=lambda to, data: WORD(0))
        self.assertIs(r(EVM_TOKEN, "base", EVM_PAYER), False)

    def test_absent_method_none(self):
        # "0x" (non-token / reverted) -> None (unknown), never False.
        r = BalanceReader(eth_call=lambda to, data: "0x")
        self.assertIsNone(r(EVM_TOKEN, "base", EVM_PAYER))

    def test_eth_call_raises_fail_open(self):
        def boom(to, data):
            raise RuntimeError("rpc down")
        self.assertIsNone(BalanceReader(eth_call=boom)(EVM_TOKEN, "base", EVM_PAYER))


class TestSolana(unittest.TestCase):
    def _accts(self, *amounts):
        return {"value": [{"account": {"data": {"parsed": {"info": {
            "tokenAmount": {"amount": str(a)}}}}}} for a in amounts]}

    def test_positive_holdings_true(self):
        r = BalanceReader(sol_rpc=lambda m, p: self._accts(0, 250))
        self.assertIs(r(SOL_TOKEN, "solana", SOL_PAYER), True)

    def test_no_accounts_false(self):
        # empty list -> owner holds none -> False (a successful read).
        r = BalanceReader(sol_rpc=lambda m, p: {"value": []})
        self.assertIs(r(SOL_TOKEN, "solana", SOL_PAYER), False)

    def test_unreadable_none(self):
        r = BalanceReader(sol_rpc=lambda m, p: {"value": None})
        self.assertIsNone(r(SOL_TOKEN, "solana", SOL_PAYER))

    def test_malformed_account_skipped(self):
        # MUTATION: an unguarded parse would crash on a malformed account entry.
        bad = {"value": [{"account": {"data": {}}},
                         {"account": {"data": {"parsed": {"info": {
                             "tokenAmount": {"amount": "5"}}}}}}]}
        r = BalanceReader(sol_rpc=lambda m, p: bad)
        self.assertIs(r(SOL_TOKEN, "solana", SOL_PAYER), True)

    def test_sol_rpc_raises_fail_open(self):
        def boom(m, p):
            raise RuntimeError("down")
        self.assertIsNone(BalanceReader(sol_rpc=boom)(SOL_TOKEN, "solana", SOL_PAYER))


class TestDispatch(unittest.TestCase):
    def test_evm_token_uses_evm_path(self):
        calls = {"evm": 0, "sol": 0}

        def ec(to, data):
            calls["evm"] += 1
            return WORD(1)

        def sr(m, p):
            calls["sol"] += 1
            return {"value": []}
        BalanceReader(eth_call=ec, sol_rpc=sr)(EVM_TOKEN, "base", EVM_PAYER)
        self.assertEqual((calls["evm"], calls["sol"]), (1, 0))

    def test_mismatched_token_payer_none(self):
        # EVM token but a Solana payer (or vice versa) -> can't read -> None.
        r = BalanceReader(eth_call=lambda to, data: WORD(1))
        self.assertIsNone(r(EVM_TOKEN, "base", SOL_PAYER))

    def test_no_rpc_configured_none(self):
        # No injected seam and no URL -> fail-open None, no crash.
        self.assertIsNone(BalanceReader()(EVM_TOKEN, "base", EVM_PAYER))


if __name__ == "__main__":
    unittest.main()
