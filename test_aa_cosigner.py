"""
Tests for aa_cosigner.py -- the ERC-4337 co-signer. Pure tests are stdlib; the
userOpHash and signature are ALSO cross-checked against eth_abi / eth-account when
present (skipUnless), since those are the ground truth an on-chain EntryPoint uses.
"""
import unittest

import aa_cosigner as AA
import eip712 as E
import secp256k1 as S
from keccak import keccak256

try:
    import eth_abi
    _HAVE_ETH_ABI = True
except Exception:  # pragma: no cover
    _HAVE_ETH_ABI = False
try:
    from eth_account import Account
    _HAVE_ETH_ACCOUNT = True
except Exception:  # pragma: no cover
    _HAVE_ETH_ACCOUNT = False

EP = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"   # EntryPoint v0.7
CID = 8453
KEY = 0xB1ACC1A55
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CP = "0x" + "c" * 40
EVIL = "0x" + "d" * 40


def _uo(**o):
    base = {"sender": "0x" + "a" * 40, "nonce": 7, "initCode": "0x",
            "callData": "0xdeadbeef", "verificationGasLimit": 100000,
            "callGasLimit": 200000, "preVerificationGas": 21000,
            "maxPriorityFeePerGas": 1, "maxFeePerGas": 2, "paymasterAndData": "0x"}
    base.update(o)
    return base


def _execute(dest, value, inner_hex):
    inner = bytes.fromhex(inner_hex[2:] if inner_hex.startswith("0x") else inner_hex)
    head = (bytes(12) + bytes.fromhex(dest[2:]) + value.to_bytes(32, "big")
            + (96).to_bytes(32, "big"))
    tail = len(inner).to_bytes(32, "big") + inner + bytes((-len(inner)) % 32)
    return "0x" + (AA.EXECUTE_SELECTOR + head + tail).hex()


def _transfer(to, amount):
    return "0xa9059cbb" + (bytes(12) + bytes.fromhex(to[2:])).hex() \
        + amount.to_bytes(32, "big").hex()


def GO(claim):
    return {"verdict": "GO", "hard_stop": False, "reasons": ["ok"]}


def STOP(claim):
    return {"verdict": "STOP", "hard_stop": True, "reasons": ["sanctioned"]}


def HOLD(claim):
    return {"verdict": "HOLD", "hard_stop": False, "reasons": ["thin"]}


def DOWN(claim):
    raise ConnectionError("engine down")


class TestUserOpHash(unittest.TestCase):
    """Mutation notes: any field dropped from the encoding -> a *-sensitive test
    FAILS; packing gas fields wrong -> test_packed_equals_unpacked FAILS."""

    def test_deterministic(self):
        self.assertEqual(AA.user_op_hash(_uo(), EP, CID),
                         AA.user_op_hash(_uo(), EP, CID))

    def test_field_sensitivity(self):
        base = AA.user_op_hash(_uo(), EP, CID)
        for chg in ({"nonce": 8}, {"sender": "0x" + "b" * 40},
                    {"callData": "0xdead"}, {"callGasLimit": 999},
                    {"maxFeePerGas": 9}, {"preVerificationGas": 1}):
            self.assertNotEqual(AA.user_op_hash(_uo(**chg), EP, CID), base, chg)
        self.assertNotEqual(AA.user_op_hash(_uo(), EP, 1), base)          # chainId
        self.assertNotEqual(AA.user_op_hash(_uo(), "0x" + "1" * 40, CID), base)  # EP

    def test_packed_equals_unpacked(self):
        acct = (100000 << 128 | 200000).to_bytes(32, "big")
        fees = (1 << 128 | 2).to_bytes(32, "big")
        packed = _uo(accountGasLimits="0x" + acct.hex(), gasFees="0x" + fees.hex())
        for k in ("verificationGasLimit", "callGasLimit", "maxPriorityFeePerGas",
                  "maxFeePerGas"):
            packed.pop(k)
        self.assertEqual(AA.user_op_hash(packed, EP, CID),
                         AA.user_op_hash(_uo(), EP, CID))

    @unittest.skipUnless(_HAVE_ETH_ABI, "eth-abi not installed")
    def test_matches_eth_abi_reference(self):
        u = _uo()
        acct = (100000 << 128 | 200000).to_bytes(32, "big")
        fees = (1 << 128 | 2).to_bytes(32, "big")
        empty = keccak256(b"")
        enc = eth_abi.encode(
            ["address", "uint256", "bytes32", "bytes32", "bytes32", "uint256",
             "bytes32", "bytes32"],
            [u["sender"], u["nonce"], empty, keccak256(bytes.fromhex("deadbeef")),
             acct, u["preVerificationGas"], fees, empty])
        outer = keccak256(eth_abi.encode(["bytes32", "address", "uint256"],
                                         [keccak256(enc), EP, CID]))
        self.assertEqual(AA.user_op_hash(u, EP, CID), int.from_bytes(outer, "big"))


class TestSignatureRecovery(unittest.TestCase):
    def test_round_trip(self):
        uoh = AA.user_op_hash(_uo(), EP, CID)
        sig, signer = AA._sign_hash(uoh, KEY)
        self.assertEqual(signer, E.pubkey_to_address(S.privkey_to_pub(KEY)))
        self.assertEqual(AA.recover_cosigner(uoh, sig), signer)

    def test_recover_rejects_garbage(self):
        self.assertIsNone(AA.recover_cosigner(123, "0x00"))
        self.assertIsNone(AA.recover_cosigner(123, "0x" + "00" * 65))

    @unittest.skipUnless(_HAVE_ETH_ACCOUNT, "eth-account not installed")
    def test_eth_account_recovers_cosigner(self):
        uoh = AA.user_op_hash(_uo(), EP, CID)
        sig, signer = AA._sign_hash(uoh, KEY)
        rec = Account._recover_hash(uoh.to_bytes(32, "big"), signature=sig)
        self.assertEqual(rec.lower(), signer)


class TestDecodeExecute(unittest.TestCase):
    def test_decode(self):
        cd = _execute(USDC, 5, _transfer(CP, 90000))
        d = AA.decode_execute(cd)
        self.assertTrue(AA.recover_cosigner and d["to"].lower() == USDC.lower())
        self.assertEqual(d["value"], 5)
        self.assertTrue(d["data"].startswith("0xa9059cbb"))

    def test_non_execute_is_none(self):
        self.assertIsNone(AA.decode_execute("0xdeadbeef"))
        self.assertIsNone(AA.decode_execute("0x"))
        self.assertIsNone(AA.decode_execute(None))


class TestCosignDecision(unittest.TestCase):
    """
    Mutation notes:
      - sign on STOP -> test_stop_refused FAILS.
      - sign a HOLD without approval -> test_hold_requires_confirm FAILS.
      - fail-open/closed swapped -> test_engine_down_policy FAILS.
      - ignore calldata mismatch -> test_drainer_refused_despite_go FAILS.
    """
    CLAIM = {"counterparty": CP, "amount": "0.09", "asset": USDC, "chain": "base"}

    def _cosign(self, verdict_fn, **kw):
        return AA.cosign_user_op(_uo(), self.CLAIM, entry_point=EP, chain_id=CID,
                                 verdict_fn=verdict_fn, signer_key=KEY, screen=False, **kw)

    def test_go_signed_and_verifiable(self):
        r = self._cosign(GO)
        self.assertTrue(r.approved)
        self.assertEqual(AA.recover_cosigner(r.user_op_hash, r.signature), r.signer)

    def test_stop_refused(self):
        r = self._cosign(STOP)
        self.assertFalse(r.approved)
        self.assertIsNone(r.signature)

    def test_hold_requires_confirm(self):
        self.assertFalse(self._cosign(HOLD).approved)                     # no confirm
        self.assertFalse(self._cosign(HOLD, confirm=lambda x: False).approved)
        approved = self._cosign(HOLD, confirm=lambda x: True)
        self.assertTrue(approved.approved)
        self.assertEqual(approved.action, "confirmed")

    def test_engine_down_policy(self):
        self.assertFalse(self._cosign(DOWN, policy=AA.FAIL_CLOSED).approved)  # halt
        self.assertTrue(self._cosign(DOWN, policy=AA.FAIL_OPEN).approved)     # advisory

    def test_drainer_refused_despite_go(self):
        # verdict says GO, but the inner call is an UNLIMITED approval -> REFUSE
        inner = "0x095ea7b3" + (bytes(12) + bytes.fromhex(EVIL[2:])).hex() \
            + ((1 << 256) - 1).to_bytes(32, "big").hex()
        uo = _uo(callData=_execute(USDC, 0, inner))
        r = AA.cosign_user_op(uo, self.CLAIM, entry_point=EP, chain_id=CID,
                              verdict_fn=GO, signer_key=KEY)
        self.assertFalse(r.approved)
        self.assertTrue(any("UNLIMITED" in x for x in r.reasons))

    def test_wrong_recipient_refused_despite_go(self):
        uo = _uo(callData=_execute(USDC, 0, _transfer(EVIL, 90000)))
        r = AA.cosign_user_op(uo, self.CLAIM, entry_point=EP, chain_id=CID,
                              verdict_fn=GO, signer_key=KEY)
        self.assertFalse(r.approved)

    def test_clean_execute_signed(self):
        uo = _uo(callData=_execute(USDC, 0, _transfer(CP, 90000)))
        r = AA.cosign_user_op(uo, self.CLAIM, entry_point=EP, chain_id=CID,
                              verdict_fn=GO, signer_key=KEY)
        self.assertTrue(r.approved)


class TestMakeVerdictFn(unittest.TestCase):
    def test_backed_by_forecast(self):
        import blackwall
        fn = AA.make_verdict_fn(blackwall.MockReputationSource())
        good = fn({"counterparty": "0xKNOWNGOOD000000000000000000000000000001",
                   "amount": "0.09", "asset": "USDC", "chain": "base"})
        self.assertEqual(good["verdict"], "GO")
        with self.assertRaises(ValueError):
            fn({"amount": "0.09"})            # bad request -> raises (engine "down")


if __name__ == "__main__":
    unittest.main()
