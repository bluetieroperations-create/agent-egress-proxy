#!/usr/bin/env python3
"""
test_auth_sim.py -- TDD for EIP-3009 authorization simulation. Each test names the
MUTATION it kills. The three failure modes here are exactly the ones a plain-transfer
simulation cannot see: replay, validity window, and real-call execution.
"""
import unittest

from auth_sim import (AUTH_UNKNOWN, EXPIRED, NOT_YET_VALID, REPLAYED, VALID, WOULD_REVERT,
                      AuthorizationSimSource, apply_auth_sim, assess_authorization,
                      check_window, encode_address, encode_authorization_state,
                      encode_bytes32, encode_transfer_with_authorization, encode_uint,
                      split_signature)

PAYER = "0x" + "a1" * 20
PAYEE = "0x" + "b2" * 20
NONCE = "0x" + "7f" * 32
SIG = "0x" + ("11" * 32) + ("22" * 32) + "1b"          # r, s, v=27


def _auth(**kw):
    a = {"from": PAYER, "to": PAYEE, "value": "1000000",
         "validAfter": 0, "validBefore": 9999999999, "nonce": NONCE}
    a.update(kw)
    return a


def _payment(auth=None, signature=SIG):
    return {"payload": {"authorization": auth or _auth(), "signature": signature}}


class TestEncoding(unittest.TestCase):
    def test_bytes32_is_right_padded_not_numeric(self):
        # THE BUG THIS MODULE EXISTS TO AVOID: eth_call_data would send a bytes32 nonce
        # through its numeric branch and encode ZEROS -- simulating a DIFFERENT
        # authorization than the one being checked. bytes32 is right-padded bytes.
        self.assertEqual(encode_bytes32("0xab"), "ab" + "0" * 62)
        self.assertEqual(encode_bytes32(NONCE), "7f" * 32)
        self.assertNotEqual(encode_bytes32(NONCE), encode_uint(0))

    def test_bytes32_junk_is_zeros_not_crash(self):
        for junk in (None, 5, [], "0xzz", ""):
            self.assertEqual(len(encode_bytes32(junk)), 64)

    def test_encoders_never_raise_on_any_garbage(self):
        # SWEEP REGRESSION: encode_uint(inf) raised OverflowError -- outside its old
        # (TypeError, ValueError) tuple. Same class as the 3.12 dict-slice KeyError CI
        # caught in dex_price. Includes the 3.12 dict shape explicitly.
        class D312(dict):
            def __getitem__(self, k):
                raise KeyError(k)
        for bad in (None, "", {}, D312(), [], (), set(), 0, -1, object(), b"x",
                    float("inf"), float("nan"), "0xzz", "notahex"):
            self.assertEqual(len(encode_uint(bad)), 64, repr(bad))
            self.assertEqual(len(encode_bytes32(bad)), 64, repr(bad))
            self.assertEqual(len(encode_address(bad)), 64, repr(bad))
            split_signature(bad)                       # must not raise

    def test_uint_and_address_words(self):
        self.assertEqual(encode_uint(1), "0" * 63 + "1")
        self.assertEqual(encode_address(PAYER), "0" * 24 + "a1" * 20)
        self.assertEqual(encode_uint(-1), "0" * 64)          # out of range -> zeros
        self.assertEqual(encode_uint(1 << 256), "0" * 64)
        self.assertEqual(encode_address("nope"), "0" * 64)

    def test_split_signature(self):
        v, r, s = split_signature(SIG)
        self.assertEqual(v, 27)
        self.assertEqual(r, "11" * 32)
        self.assertEqual(s, "22" * 32)

    def test_split_signature_normalizes_v_and_rejects_bad(self):
        # MUTATION: a v of 0/1 (EIP-2098-style) must normalize to 27/28, not be dropped.
        v, _, _ = split_signature("0x" + "11" * 32 + "22" * 32 + "00")
        self.assertEqual(v, 27)
        for bad in (None, 5, "0x1234", "0x" + "11" * 64, "notahex"):
            self.assertIsNone(split_signature(bad))

    def test_calldata_length_and_selector(self):
        data = encode_transfer_with_authorization(_auth(), SIG)
        # 4-byte selector + 9 static words
        self.assertEqual(len(data), 2 + 8 + 9 * 64)
        self.assertTrue(data.startswith("0x"))

    def test_calldata_none_on_bad_signature(self):
        self.assertIsNone(encode_transfer_with_authorization(_auth(), "0xdead"))
        self.assertIsNone(encode_transfer_with_authorization(None, SIG))

    def test_authorization_state_calldata(self):
        d = encode_authorization_state(PAYER, NONCE)
        self.assertEqual(len(d), 2 + 8 + 2 * 64)
        self.assertIn("7f" * 32, d)              # the real nonce, not zeros


class TestWindow(unittest.TestCase):
    def test_expired_and_not_yet_valid(self):
        self.assertEqual(check_window(0, 1000, now=2000), EXPIRED)
        self.assertEqual(check_window(5000, 9999, now=2000), NOT_YET_VALID)
        self.assertIsNone(check_window(0, 9999, now=2000))

    def test_zero_valid_before_means_no_expiry(self):
        # MUTATION: treating validBefore==0 as "expired at epoch" would reject every
        # no-expiry authorization.
        self.assertIsNone(check_window(0, 0, now=10 ** 9))

    def test_unparseable_bounds_fail_open(self):
        for bad in (None, "abc", [], {}):
            self.assertIsNone(check_window(bad, bad, now=2000))


class TestAssess(unittest.TestCase):
    def test_replay_wins_over_everything(self):
        # ORDERING: replay is definitive and EXPLAINS a revert, so it must be reported
        # rather than the downstream "it reverts".
        s = assess_authorization({"used": True, "execution_ok": False,
                                  "revert_reason": "x"})
        self.assertEqual(s["grade"], REPLAYED)

    def test_window_beats_execution(self):
        s = assess_authorization({"used": False, "window": EXPIRED,
                                  "execution_ok": False})
        self.assertEqual(s["grade"], EXPIRED)

    def test_execution_revert(self):
        s = assess_authorization({"used": False, "execution_ok": False,
                                  "revert_reason": "FiatTokenV2: invalid signature"})
        self.assertEqual(s["grade"], WOULD_REVERT)
        self.assertIn("invalid signature", s["reason"])

    def test_valid_and_unknown(self):
        self.assertEqual(assess_authorization({"execution_ok": True})["grade"], VALID)
        self.assertEqual(assess_authorization({"used": False})["grade"], VALID)
        self.assertEqual(assess_authorization({})["grade"], AUTH_UNKNOWN)
        self.assertEqual(assess_authorization(None)["grade"], AUTH_UNKNOWN)

    def test_never_raises(self):
        for j in (None, "x", 5, []):
            assess_authorization(j)


class TestApplyFold(unittest.TestCase):
    def test_replay_escalates_go_to_hold(self):
        v = apply_auth_sim({"verdict": "GO"}, {"grade": REPLAYED, "reason": "used"})
        self.assertEqual(v["verdict"], "HOLD")
        self.assertTrue(v["signals"]["auth_sim"]["gated"])

    def test_expired_and_would_revert_gate(self):
        for g in (EXPIRED, NOT_YET_VALID, WOULD_REVERT):
            v = apply_auth_sim({"verdict": "GO"}, {"grade": g})
            self.assertEqual(v["verdict"], "HOLD", g)

    def test_valid_and_unknown_do_not_gate(self):
        for g in (VALID, AUTH_UNKNOWN):
            v = apply_auth_sim({"verdict": "GO"}, {"grade": g})
            self.assertEqual(v["verdict"], "GO", g)
            self.assertFalse(v["signals"]["auth_sim"]["gated"])

    def test_never_stops_and_never_upgrades(self):
        # HARD BOUNDARY: payload_sim keeps the STOP authority for a forged/mismatched
        # authorization; this module only ever says "it cannot land right now".
        for g in (REPLAYED, EXPIRED, WOULD_REVERT, VALID, AUTH_UNKNOWN):
            self.assertNotEqual(apply_auth_sim({"verdict": "GO"}, {"grade": g})["verdict"],
                                "STOP")
            for start in ("HOLD", "STOP"):
                self.assertEqual(
                    apply_auth_sim({"verdict": start}, {"grade": VALID})["verdict"], start)

    def test_noop_and_non_mutating(self):
        base = {"verdict": "GO", "signals": {"x": 1}}
        self.assertEqual(apply_auth_sim(base, None), base)
        self.assertEqual(apply_auth_sim(base, {"grade": "bogus"}), base)
        apply_auth_sim(base, {"grade": REPLAYED})
        self.assertNotIn("auth_sim", base["signals"])
        apply_auth_sim(None, {"grade": VALID})            # must not raise


class _Sim:
    """Minimal stand-in exposing the `_call` seam the source uses."""

    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def _call(self, params):
        self.calls.append(params)
        return self.responder(params)


class TestSource(unittest.TestCase):
    CLAIM = {"payer": PAYER, "counterparty": PAYEE, "amount": "1.0",
             "asset": "USDC", "chain": "ethereum"}

    def test_detects_replayed_nonce_without_executing(self):
        # THE HEADLINE CAPABILITY: a used nonce is caught by a DIRECT state read, and the
        # expensive execution simulation is skipped entirely.
        sim = _Sim(lambda p: {"result": "0x" + "0" * 63 + "1"})     # authorizationState -> true
        src = AuthorizationSimSource(simulator=sim)
        sig = src.check(self.CLAIM, _payment())
        self.assertEqual(sig["grade"], REPLAYED)
        self.assertEqual(len(sim.calls), 1)                          # no second call

    def test_expired_window_needs_no_execution_call(self):
        sim = _Sim(lambda p: {"result": "0x" + "0" * 64})
        src = AuthorizationSimSource(simulator=sim, now=10 ** 10)
        sig = src.check(self.CLAIM, _payment(_auth(validBefore=1000)))
        self.assertEqual(sig["grade"], EXPIRED)

    def test_clean_authorization_simulates_and_is_valid(self):
        sim = _Sim(lambda p: {"result": "0x" + "0" * 64})
        src = AuthorizationSimSource(simulator=sim, now=1000)
        sig = src.check(self.CLAIM, _payment())
        self.assertEqual(sig["grade"], VALID)
        self.assertEqual(len(sim.calls), 2)             # state read + execution sim

    def test_execution_revert_is_reported(self):
        def responder(p):
            if len(p.get("data", "")) < 200:            # the short authorizationState call
                return {"result": "0x" + "0" * 64}
            body = b"FiatTokenV2: invalid signature"
            return {"error": {"message": "execution reverted",
                              "data": "0x08c379a0" + "%064x" % 32 + "%064x" % len(body)
                                      + body.hex().ljust(64, "0")}}
        src = AuthorizationSimSource(simulator=_Sim(responder), now=1000)
        sig = src.check(self.CLAIM, _payment())
        self.assertEqual(sig["grade"], WOULD_REVERT)
        self.assertIn("invalid signature", sig["reason"])

    def test_noop_without_authorization_or_chain(self):
        sim = _Sim(lambda p: {"result": "0x" + "0" * 64})
        src = AuthorizationSimSource(simulator=sim)
        self.assertIsNone(src.check(self.CLAIM, None))
        self.assertIsNone(src.check(self.CLAIM, {"payload": {}}))
        self.assertIsNone(src.check(dict(self.CLAIM, chain="dogechain"), _payment()))
        self.assertIsNone(src.check(None, _payment()))

    def test_noop_without_rpc(self):
        self.assertIsNone(AuthorizationSimSource().check(self.CLAIM, _payment()))

    def test_source_never_raises(self):
        def boom(p):
            raise IOError("net")
        src = AuthorizationSimSource(simulator=_Sim(boom), now=1000)
        sig = src.check(self.CLAIM, _payment())
        self.assertEqual(sig["grade"], AUTH_UNKNOWN)     # fail-open, no exception

    def test_real_usdc_revert_string(self):
        """LIVE-harvested from mainnet USDC: submitting the real 9-arg
        transferWithAuthorization with a bogus signature reverts with this exact string.
        Its presence proves our ABI encoding reaches the genuine function."""
        def responder(p):
            if len(p.get("data", "")) < 200:
                return {"result": "0x" + "0" * 64}
            body = b"ECRecover: invalid signature"
            return {"error": {"message": "execution reverted",
                              "data": "0x08c379a0" + "%064x" % 32 + "%064x" % len(body)
                                      + body.hex().ljust(64, "0")}}
        sig = AuthorizationSimSource(simulator=_Sim(responder), now=1000).check(
            self.CLAIM, _payment())
        self.assertEqual(sig["grade"], WOULD_REVERT)
        self.assertEqual(sig["reason"], "ECRecover: invalid signature")

    def test_calldata_length_stable_under_junk_fields(self):
        # AUDIT: a junk field must not SHORTEN the calldata -- truncated calldata would
        # call a different function shape entirely.
        good = encode_transfer_with_authorization(_auth(), SIG)
        for bad in ({"from": None}, {"value": "abc"}, {"nonce": 123}, {"validBefore": []}):
            self.assertEqual(len(encode_transfer_with_authorization(_auth(**bad), SIG)),
                             len(good), bad)

    def test_window_boundaries(self):
        # AUDIT: exactly AT validBefore is expired; exactly AT validAfter is in-window.
        self.assertEqual(check_window(0, 1000, now=1000), EXPIRED)
        self.assertIsNone(check_window(1000, 2000, now=1000))

    def test_nonce_reaches_the_chain_intact(self):
        # REGRESSION for the encoding bug: the state read must carry the REAL nonce.
        sim = _Sim(lambda p: {"result": "0x" + "0" * 64})
        AuthorizationSimSource(simulator=sim, now=1000).check(self.CLAIM, _payment())
        self.assertIn("7f" * 32, sim.calls[0]["data"])


if __name__ == "__main__":
    unittest.main()
