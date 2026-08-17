#!/usr/bin/env python3
"""
test_solana_rwa.py -- TDD for the Solana (SPL Token-2022) leg of the RWA readiness
gate. Each test names the MUTATION it kills.
"""
import base64
import unittest

import solana_rwa as sol
from rwa_readiness import apply_rwa_readiness
from solana_rwa import (SolanaRwaReadinessSource, assess_solana_readiness, b58decode,
                        b58encode, is_solana_address, parse_mint_extensions,
                        parse_token_account)

MINT = b58encode(bytes([1]) * 32)          # valid 32-byte base58 pubkeys
ATA = b58encode(bytes([2]) * 32)


def mint_buf(*exts):
    """Build a Token-2022 mint buffer: 82-byte base, pad to 165, type byte, TLV exts."""
    buf = bytearray(166)
    buf[165] = 1                            # account type = Mint
    for etype, val in exts:
        buf += etype.to_bytes(2, "little") + len(val).to_bytes(2, "little") + val
    return bytes(buf)


def acct_buf(frozen=False):
    buf = bytearray(165)
    buf[108] = 2 if frozen else 1           # state: 2 = Frozen, 1 = Initialized
    return bytes(buf)


DEFAULT_FROZEN = (6, b"\x02")
NON_TRANSFERABLE = (9, b"")
TRANSFER_HOOK = (14, b"\x00" * 64)
PERMANENT_DELEGATE = (12, b"\x00" * 32)


class TestBase58(unittest.TestCase):
    def test_roundtrip(self):
        raw = bytes(range(32))
        self.assertEqual(b58decode(b58encode(raw)), raw)

    def test_leading_zero_preserved(self):
        # MUTATION: dropping the leading-'1' padding loses leading zero bytes.
        raw = b"\x00\x00" + bytes(range(30))
        self.assertEqual(b58decode(b58encode(raw)), raw)

    def test_is_solana_address(self):
        self.assertTrue(is_solana_address(MINT))
        self.assertFalse(is_solana_address("0x" + "ab" * 20))    # EVM
        self.assertFalse(is_solana_address("not base58 !!!"))
        self.assertFalse(is_solana_address(b58encode(bytes(31))))  # 31 bytes != pubkey


class TestParseMint(unittest.TestCase):
    def test_plain_mint_not_token2022(self):
        # MUTATION: treating a plain 82-byte mint as extended would misparse garbage.
        self.assertFalse(parse_mint_extensions(bytes(82))["is_token2022"])

    def test_default_frozen(self):
        ext = parse_mint_extensions(mint_buf(DEFAULT_FROZEN))
        self.assertTrue(ext["is_token2022"])
        self.assertEqual(ext["default_account_state"], 2)

    def test_non_transferable_and_hook_and_delegate(self):
        ext = parse_mint_extensions(mint_buf(NON_TRANSFERABLE, TRANSFER_HOOK,
                                             PERMANENT_DELEGATE))
        self.assertTrue(ext["non_transferable"])
        self.assertTrue(ext["transfer_hook"])
        self.assertTrue(ext["permanent_delegate"])

    def test_garbage_never_raises(self):
        self.assertFalse(parse_mint_extensions(None)["is_token2022"])
        self.assertFalse(parse_mint_extensions(b"\x01\x02")["is_token2022"])


class TestParseAccount(unittest.TestCase):
    def test_frozen(self):
        self.assertTrue(parse_token_account(acct_buf(frozen=True))["frozen"])
        self.assertFalse(parse_token_account(acct_buf(frozen=False))["frozen"])

    def test_too_short_none(self):
        self.assertIsNone(parse_token_account(bytes(50)))


class TestAssess(unittest.TestCase):
    def test_non_transferable_blocked(self):
        r = assess_solana_readiness(parse_mint_extensions(mint_buf(NON_TRANSFERABLE)))
        self.assertEqual(r["grade"], "blocked")

    def test_default_frozen_no_account_blocked(self):
        # MUTATION: treating default-frozen as fine would clear a wallet that can't
        # actually use the tokens.
        r = assess_solana_readiness(parse_mint_extensions(mint_buf(DEFAULT_FROZEN)))
        self.assertEqual(r["grade"], "blocked")

    def test_default_frozen_thawed_account_ready(self):
        r = assess_solana_readiness(parse_mint_extensions(mint_buf(DEFAULT_FROZEN)),
                                    account={"frozen": False})
        self.assertEqual(r["grade"], "ready")

    def test_frozen_account_blocked(self):
        r = assess_solana_readiness(parse_mint_extensions(mint_buf(DEFAULT_FROZEN)),
                                    account={"frozen": True})
        self.assertEqual(r["grade"], "blocked")

    def test_transfer_hook_is_caution_not_ready(self):
        # MUTATION: grading a hooked token 'ready' when the receiver is thawed would
        # over-clear -- the hook can still deny. Must be unknown+caution.
        r = assess_solana_readiness(
            parse_mint_extensions(mint_buf(DEFAULT_FROZEN, TRANSFER_HOOK)),
            account={"frozen": False})
        self.assertEqual(r["grade"], "unknown")
        self.assertTrue(any("transfer-hook" in x for x in r["reasons"]))

    def test_plain_token2022_no_restriction_unknown(self):
        # An extended mint with only a non-restriction extension -> unknown no-op.
        r = assess_solana_readiness({"is_token2022": True})
        self.assertEqual(r["grade"], "unknown")
        self.assertEqual(r["reasons"], [])


class TestLiveSource(unittest.TestCase):
    def _rpc(self, table):
        def rpc(method, params):
            pk = params[0]
            raw = table.get(pk)
            if raw is None:
                return {"value": None}
            return {"value": {"data": [base64.b64encode(raw).decode(), "base64"]}}
        return rpc

    def test_default_frozen_mint_blocks(self):
        src = SolanaRwaReadinessSource(rpc=self._rpc({MINT: mint_buf(DEFAULT_FROZEN)}))
        sig = src.check({"token": MINT}, payer=None)
        self.assertEqual(sig["grade"], "blocked")
        self.assertEqual(sig["signals"]["standard"], "spl-token-2022")

    def test_receiver_account_thawed_ready(self):
        src = SolanaRwaReadinessSource(rpc=self._rpc(
            {MINT: mint_buf(DEFAULT_FROZEN), ATA: acct_buf(frozen=False)}))
        sig = src.check({"token": MINT, "receiver_token_account": ATA})
        self.assertEqual(sig["grade"], "ready")

    def test_receiver_account_frozen_blocks(self):
        src = SolanaRwaReadinessSource(rpc=self._rpc(
            {MINT: mint_buf(DEFAULT_FROZEN), ATA: acct_buf(frozen=True)}))
        sig = src.check({"token": MINT, "receiver_token_account": ATA})
        self.assertEqual(sig["grade"], "blocked")

    def test_plain_mint_noop_none(self):
        # MUTATION: a plain SPL mint (no extensions) must be a no-op None, not blocked.
        src = SolanaRwaReadinessSource(rpc=self._rpc({MINT: bytes(82)}))
        self.assertIsNone(src.check({"token": MINT}))

    def test_non_solana_token_none(self):
        src = SolanaRwaReadinessSource(rpc=self._rpc({}))
        self.assertIsNone(src.check({"token": "0x" + "ab" * 20}))

    def test_rpc_raises_fail_open(self):
        def boom(method, params):
            raise RuntimeError("rpc down")
        self.assertIsNone(SolanaRwaReadinessSource(rpc=boom).check({"token": MINT}))

    def test_signal_folds_through_apply(self):
        # End-to-end: a blocked Solana signal escalates a GO verdict to HOLD via the
        # SHARED apply_rwa_readiness fold (proving shape-compatibility with the EVM leg).
        src = SolanaRwaReadinessSource(rpc=self._rpc({MINT: mint_buf(NON_TRANSFERABLE)}))
        sig = src.check({"token": MINT})
        verdict = apply_rwa_readiness(
            {"verdict": "GO", "reasons": [], "signals": {}}, sig)
        self.assertEqual(verdict["verdict"], "HOLD")
        self.assertEqual(verdict["signals"]["rwa_transfer_readiness"]["standard"],
                         "spl-token-2022")


if __name__ == "__main__":
    unittest.main()
