#!/usr/bin/env python3
"""
test_rwa_readiness.py -- TDD for the tokenized-RWA transfer-restriction readiness
gate (rwa_readiness.py). Each test names the MUTATION it kills.

Coverage:
  * assess_transfer_readiness -- blocked/ready/unknown boundaries + blocking DOMINATES
    positive + fail-open (None never counts as failure).
  * apply_rwa_readiness -- conservative-only fold (GO->HOLD, never upgrade), no-op on
    None/unknown, non-mutating.
  * ABI helpers -- selector correctness, address-arg encoding, bool/address decode
    with the critical empty-'0x' -> None (absence != rejection).
  * RwaReadinessSource -- ERC-3643 / allowlist / frozen / paused / plain-ERC-20 /
    no-payer / transport-raises, all via an injected eth_call transport.
  * forecast integration -- a GO buy of a permissioned RWA whose receiver is not
    verified escalates to HOLD; no `acquires` leaves the verdict untouched.
  * validate_request -- accepts a well-formed `acquires`, rejects malformed shapes.
"""
import unittest

import rwa_readiness as rwa
from rwa_readiness import (RwaReadinessSource, apply_rwa_readiness,
                           assess_transfer_readiness, decode_address, decode_bool,
                           decode_code_and_reason, decode_string, decode_uint,
                           eth_call_data, is_rwa_descriptor, selector)


def _abi_string(text):
    """Encode a plain ABI `string` return: offset(0x20) + length + padded bytes."""
    raw = text.encode("utf-8")
    body = raw.ljust((len(raw) + 31) // 32 * 32, b"\x00")
    return ("0x" + format(32, "064x") + format(len(raw), "064x") + body.hex())


def _abi_code_reason(code, text):
    """Encode a `(uint256 code, string reason)` return (Securitize preTransferCheck)."""
    raw = text.encode("utf-8")
    body = raw.ljust((len(raw) + 31) // 32 * 32, b"\x00")
    return ("0x" + format(code, "064x") + format(64, "064x")
            + format(len(raw), "064x") + body.hex())

VERIFIED = "0x1111111111111111111111111111111111111111"
TOKEN = "0x2222222222222222222222222222222222222222"
REGISTRY = "0x3333333333333333333333333333333333333333"
PAYEE = "0x4444444444444444444444444444444444444444"

WORD_TRUE = "0x" + "00" * 31 + "01"
WORD_FALSE = "0x" + "00" * 32


def _word_addr(addr):
    return "0x" + "0" * 24 + addr[2:].lower()


class TestAssess(unittest.TestCase):
    def test_not_verified_is_blocked(self):
        # MUTATION: `receiver_verified is False` -> `is True` would flip a rejected
        # receiver to blocked-not-fired. A non-whitelisted wallet MUST block.
        r = assess_transfer_readiness({"standard": "erc3643",
                                       "receiver_verified": False})
        self.assertEqual(r["grade"], "blocked")
        self.assertTrue(any("KYC" in x or "whitelist" in x for x in r["reasons"]))

    def test_verified_is_ready(self):
        # MUTATION: dropping the `positive` branch would leave a verified receiver
        # as unknown instead of ready.
        r = assess_transfer_readiness({"standard": "erc3643",
                                       "receiver_verified": True})
        self.assertEqual(r["grade"], "ready")

    def test_frozen_is_blocked(self):
        # MUTATION: removing the frozen check lets a frozen wallet through.
        r = assess_transfer_readiness({"receiver_frozen": True})
        self.assertEqual(r["grade"], "blocked")

    def test_paused_is_blocked(self):
        # MUTATION: removing the paused check lets a paused token through.
        r = assess_transfer_readiness({"paused": True})
        self.assertEqual(r["grade"], "blocked")

    def test_can_receive_false_with_code(self):
        # MUTATION: dropping can_receive handling / reason_code interpolation.
        r = assess_transfer_readiness({"can_receive": False, "reason_code": "0x50"})
        self.assertEqual(r["grade"], "blocked")
        self.assertTrue(any("0x50" in x for x in r["reasons"]))

    def test_blocking_dominates_positive(self):
        # MUTATION: an `elif` that reported ready when verified is True would MISS
        # that the same wallet is frozen. Blocking must dominate.
        r = assess_transfer_readiness({"receiver_verified": True,
                                       "receiver_frozen": True})
        self.assertEqual(r["grade"], "blocked")

    def test_no_signal_is_unknown_not_blocked(self):
        # MUTATION: a default of "blocked" instead of "unknown" would false-block
        # every plain ERC-20. Absence of a restriction interface is NOT a rejection.
        self.assertEqual(assess_transfer_readiness({})["grade"], "unknown")
        self.assertEqual(assess_transfer_readiness({"standard": None})["grade"],
                         "unknown")

    def test_none_fields_never_count_as_failure(self):
        # MUTATION: using truthiness (`if probe.get("paused")`) instead of `is True`
        # would still pass None, but using `is not None`/`== False` wrongly could
        # block. Explicitly assert None -> unknown.
        r = assess_transfer_readiness({"receiver_verified": None,
                                       "receiver_frozen": None, "paused": None})
        self.assertEqual(r["grade"], "unknown")

    def test_non_dict_probe_is_unknown(self):
        self.assertEqual(assess_transfer_readiness(None)["grade"], "unknown")
        self.assertEqual(assess_transfer_readiness("nope")["grade"], "unknown")


class TestApply(unittest.TestCase):
    def _go(self):
        return {"verdict": "GO", "reasons": ["ok"], "signals": {"x": 1}}

    def test_blocked_escalates_go_to_hold(self):
        # MUTATION: not setting verdict=HOLD on blocked would let a doomed buy GO.
        sig = assess_transfer_readiness({"receiver_verified": False})
        out = apply_rwa_readiness(self._go(), sig)
        self.assertEqual(out["verdict"], "HOLD")
        self.assertTrue(any("GO->HOLD" in r for r in out["reasons"]))

    def test_ready_leaves_go(self):
        # MUTATION: escalating on ready would make the gate useless (always HOLD).
        sig = assess_transfer_readiness({"receiver_verified": True})
        self.assertEqual(apply_rwa_readiness(self._go(), sig)["verdict"], "GO")

    def test_never_upgrades_hold(self):
        # MUTATION: an unconditional verdict write could upgrade HOLD->GO on ready.
        hold = {"verdict": "HOLD", "reasons": [], "signals": {}}
        sig = assess_transfer_readiness({"receiver_verified": True})
        self.assertEqual(apply_rwa_readiness(hold, sig)["verdict"], "HOLD")

    def test_never_touches_stop(self):
        # MUTATION: blocked-escalation must be guarded by `verdict == "GO"`; a STOP
        # must stay STOP (and never become HOLD).
        stop = {"verdict": "STOP", "reasons": [], "signals": {}, "hard_stop": True}
        sig = assess_transfer_readiness({"receiver_verified": False})
        out = apply_rwa_readiness(stop, sig)
        self.assertEqual(out["verdict"], "STOP")
        self.assertTrue(out["hard_stop"])

    def test_none_signal_is_noop(self):
        base = self._go()
        self.assertEqual(apply_rwa_readiness(base, None), base)
        self.assertEqual(apply_rwa_readiness(base, {}), base)

    def test_unknown_with_reason_surfaces_note_without_escalating(self):
        sig = {"grade": "unknown", "standard": "erc3643", "signals": {},
               "reasons": ["no receiving wallet supplied"]}
        out = apply_rwa_readiness(self._go(), sig)
        self.assertEqual(out["verdict"], "GO")
        self.assertTrue(any("no receiving wallet" in r for r in out["reasons"]))

    def test_does_not_mutate_input(self):
        # MUTATION: mutating verdict["reasons"]/["signals"] in place would corrupt
        # the caller's dict.
        base = self._go()
        sig = assess_transfer_readiness({"receiver_verified": False})
        apply_rwa_readiness(base, sig)
        self.assertEqual(base["verdict"], "GO")
        self.assertEqual(base["reasons"], ["ok"])
        self.assertNotIn("rwa_transfer_readiness", base["signals"])

    def test_grade_recorded_in_signals(self):
        sig = assess_transfer_readiness({"receiver_verified": False})
        out = apply_rwa_readiness(self._go(), sig)
        self.assertEqual(out["signals"]["rwa_transfer_readiness"]["grade"], "blocked")


class TestAbiHelpers(unittest.TestCase):
    def test_selector_known_values(self):
        # MUTATION: a broken keccak/selector would drift these universally-known
        # 4-byte selectors.
        self.assertEqual(selector("transfer(address,uint256)"), "0xa9059cbb")
        self.assertEqual(selector("paused()"), "0x5c975abb")

    def test_eth_call_data_encodes_address_arg(self):
        data = eth_call_data("isVerified(address)", (VERIFIED,))
        self.assertTrue(data.startswith(selector("isVerified(address)")))
        # address left-padded into a 32-byte word (24 zero-nibbles + 40 hex).
        self.assertTrue(data.endswith("0" * 24 + VERIFIED[2:].lower()))
        self.assertEqual(len(data), 10 + 64)   # 0x+selector + one word

    def test_decode_bool_true_false_and_empty(self):
        self.assertIs(decode_bool(WORD_TRUE), True)
        self.assertIs(decode_bool(WORD_FALSE), False)
        # MUTATION: the crux -- an empty result (absent/reverted method) MUST decode
        # to None, never False, or every plain token would look "not whitelisted".
        self.assertIsNone(decode_bool("0x"))
        self.assertIsNone(decode_bool(""))
        self.assertIsNone(decode_bool(None))

    def test_decode_bool_strict_rejects_noncanonical(self):
        # AUDIT REGRESSION (finding A): a non-canonical bool word (e.g. a token whose
        # paused()/isFrozen() returns a uint timestamp, not 0/1) must decode to None
        # (unknown), NEVER True -- else a quirky return manufactures a false 'blocked'.
        # MUTATION: `int(word) != 0` (old, loose) would return True here.
        self.assertIsNone(decode_bool("0x" + format(1723000000, "064x")))
        self.assertIsNone(decode_bool("0x" + "ff" * 32))
        self.assertIs(decode_bool("0x" + "00" * 31 + "01"), True)   # canonical still ok
        self.assertIs(decode_bool("0x" + "00" * 32), False)

    def test_decode_address_and_zero(self):
        self.assertEqual(decode_address(_word_addr(REGISTRY)), REGISTRY)
        # zero address -> None (unset registry pointer), not "0x000...0".
        self.assertIsNone(decode_address(WORD_FALSE))
        self.assertIsNone(decode_address("0x"))

    def test_decode_uint(self):
        # ERC-1404 restriction code: 0 = allowed, nonzero = coded rejection.
        self.assertEqual(decode_uint("0x" + format(0, "064x")), 0)
        self.assertEqual(decode_uint("0x" + format(3, "064x")), 3)
        self.assertIsNone(decode_uint("0x"))
        self.assertIsNone(decode_uint(None))

    def test_decode_string(self):
        # ERC-1404 messageForTransferRestriction plain-string return.
        self.assertEqual(decode_string(_abi_string("Wallet not whitelisted")),
                         "Wallet not whitelisted")
        self.assertIsNone(decode_string("0x"))

    def test_decode_code_and_reason(self):
        # Securitize preTransferCheck (uint256 code, string reason).
        self.assertEqual(decode_code_and_reason(_abi_code_reason(0, "")), (0, None))
        code, reason = decode_code_and_reason(_abi_code_reason(10, "Not in registry"))
        self.assertEqual(code, 10)
        self.assertEqual(reason, "Not in registry")
        # A malformed dynamic tail must keep the (authoritative) code, drop the reason.
        self.assertEqual(decode_code_and_reason("0x" + format(7, "064x")), (7, None))


class FakeChain:
    """Injected eth_call transport: routes (to, selector[, arg]) -> canned result.

    `table` maps (to.lower(), signature) -> a fixed result hex, OR a callable(arg_addr)
    -> result hex for methods that take an address. Unregistered calls return '0x'
    (as a real node does for an absent method), which decodes to None (unknown)."""

    def __init__(self, table):
        self.table = {(k[0].lower(), rwa.selector(k[1])): v for k, v in table.items()}

    def __call__(self, to, data):
        sel = data[:10]
        entry = self.table.get((to.lower(), sel))
        if entry is None:
            return "0x"
        if callable(entry):
            arg = "0x" + data[10 + 24:10 + 64] if len(data) >= 10 + 64 else None
            return entry(arg)
        return entry


class TestLiveSource(unittest.TestCase):
    def _verified_map(self, verified_addr):
        def reg(arg):
            return WORD_TRUE if arg and arg.lower() == verified_addr.lower() else WORD_FALSE
        return reg

    def test_erc3643_verified_receiver_ready(self):
        chain = FakeChain({
            (TOKEN, "identityRegistry()"): _word_addr(REGISTRY),
            (REGISTRY, "isVerified(address)"): self._verified_map(VERIFIED),
        })
        src = RwaReadinessSource(transport=chain)
        sig = src.check({"token": TOKEN}, VERIFIED)
        self.assertEqual(sig["grade"], "ready")
        self.assertEqual(sig["signals"]["standard"], "erc3643")

    def test_erc3643_unverified_receiver_blocked(self):
        # MUTATION: routing the registry to the wrong selector, or reading the token
        # instead of the registry, would miss the rejection.
        chain = FakeChain({
            (TOKEN, "identityRegistry()"): _word_addr(REGISTRY),
            (REGISTRY, "isVerified(address)"): self._verified_map(VERIFIED),
        })
        src = RwaReadinessSource(transport=chain)
        sig = src.check({"token": TOKEN}, PAYEE)   # PAYEE is not the verified addr
        self.assertEqual(sig["grade"], "blocked")

    def test_frozen_receiver_blocked_even_if_verified(self):
        chain = FakeChain({
            (TOKEN, "identityRegistry()"): _word_addr(REGISTRY),
            (REGISTRY, "isVerified(address)"): self._verified_map(VERIFIED),
            (TOKEN, "isFrozen(address)"): WORD_TRUE,
        })
        src = RwaReadinessSource(transport=chain)
        sig = src.check({"token": TOKEN}, VERIFIED)
        self.assertEqual(sig["grade"], "blocked")

    def test_paused_token_blocked(self):
        chain = FakeChain({
            (TOKEN, "paused()"): WORD_TRUE,
            (TOKEN, "identityRegistry()"): _word_addr(REGISTRY),
            (REGISTRY, "isVerified(address)"): self._verified_map(VERIFIED),
        })
        src = RwaReadinessSource(transport=chain)
        sig = src.check({"token": TOKEN}, VERIFIED)
        self.assertEqual(sig["grade"], "blocked")

    def test_allowlist_fallback_blocked(self):
        # No identityRegistry -> falls back to isWhitelisted on the token itself.
        chain = FakeChain({
            (TOKEN, "isWhitelisted(address)"):
                (lambda arg: WORD_TRUE if arg and arg.lower() == VERIFIED.lower()
                 else WORD_FALSE),
        })
        src = RwaReadinessSource(transport=chain)
        self.assertEqual(src.check({"token": TOKEN}, VERIFIED)["grade"], "ready")
        self.assertEqual(src.check({"token": TOKEN}, PAYEE)["grade"], "blocked")

    def test_erc1404_restriction_code_blocks(self):
        # ERC-1404 detectTransferRestriction -> nonzero code = rejected. Needs a
        # sender (counterparty), and identityRegistry must be absent so it falls
        # through to the 1404 branch.
        chain = FakeChain({
            (TOKEN, "detectTransferRestriction(address,address,uint256)"):
                "0x" + format(3, "064x"),
            (TOKEN, "messageForTransferRestriction(uint8)"):
                _abi_string("Receiver not whitelisted"),
        })
        src = RwaReadinessSource(transport=chain)
        sig = src.check({"token": TOKEN}, VERIFIED, counterparty=PAYEE)
        self.assertEqual(sig["grade"], "blocked")
        self.assertEqual(sig["signals"]["standard"], "erc1404")
        self.assertTrue(any("Receiver not whitelisted" in r for r in sig["reasons"]))

    def test_erc1404_code_zero_is_ready(self):
        chain = FakeChain({
            (TOKEN, "detectTransferRestriction(address,address,uint256)"):
                "0x" + format(0, "064x"),
        })
        sig = RwaReadinessSource(transport=chain).check(
            {"token": TOKEN}, VERIFIED, counterparty=PAYEE)
        self.assertEqual(sig["grade"], "ready")

    def test_securitize_pretransfercheck_blocks_with_reason(self):
        chain = FakeChain({
            (TOKEN, "preTransferCheck(address,address,uint256)"):
                _abi_code_reason(10, "Wallet not in registry"),
        })
        sig = RwaReadinessSource(transport=chain).check(
            {"token": TOKEN, "seller": PAYEE}, VERIFIED)
        self.assertEqual(sig["grade"], "blocked")
        self.assertEqual(sig["signals"]["standard"], "securitize")
        self.assertTrue(any("Wallet not in registry" in r for r in sig["reasons"]))

    def test_from_requiring_standards_skipped_without_sender(self):
        # MUTATION: firing detectTransferRestriction/preTransferCheck without a sender
        # would either error or guess a bad `from`. With no seller AND no counterparty,
        # a token that ONLY implements the from-requiring methods -> no-op None.
        chain = FakeChain({
            (TOKEN, "detectTransferRestriction(address,address,uint256)"):
                "0x" + format(3, "064x"),
        })
        self.assertIsNone(RwaReadinessSource(transport=chain).check({"token": TOKEN}, VERIFIED))

    def test_erc3643_wins_over_from_requiring(self):
        # Detection order: an ERC-3643 token is labeled erc3643 (receiver-only),
        # not probed as securitize/erc1404 even when a sender is present.
        chain = FakeChain({
            (TOKEN, "identityRegistry()"): _word_addr(REGISTRY),
            (REGISTRY, "isVerified(address)"): WORD_TRUE,
        })
        sig = RwaReadinessSource(transport=chain).check(
            {"token": TOKEN}, VERIFIED, counterparty=PAYEE)
        self.assertEqual(sig["signals"]["standard"], "erc3643")
        self.assertEqual(sig["grade"], "ready")

    def test_plain_erc20_is_noop_none(self):
        # MUTATION: a plain ERC-20 (none of the restriction methods) must return
        # None (no-op), NOT a false "blocked". FakeChain returns '0x' for all.
        src = RwaReadinessSource(transport=FakeChain({}))
        self.assertIsNone(src.check({"token": TOKEN}, VERIFIED, counterparty=PAYEE))

    def test_plain_token_short_circuits_without_probing_pause_freeze(self):
        # AUDIT REGRESSION (finding B): a plain token (no permissioned interface) must
        # NOT keep calling paused()/isFrozen() -- it short-circuits after detecting no
        # standard, capping RPC amplification. MUTATION: probing paused/frozen before
        # the standard check would (a) cost extra calls and (b) let a paused-ONLY token
        # false-block. Assert both: <=2 calls AND never queries paused/isFrozen.
        calls = []

        def spy(to, data):
            calls.append(data[:10])
            return "0x"
        self.assertIsNone(RwaReadinessSource(transport=spy).check({"token": TOKEN}, VERIFIED))
        self.assertLessEqual(len(calls), 2)
        self.assertNotIn(selector("paused()"), calls)
        self.assertNotIn(selector("isFrozen(address)"), calls)

    def test_paused_only_token_is_not_false_blocked(self):
        # AUDIT REGRESSION (findings A+B combined): a token that is PAUSED but exposes
        # no permissioned receiver gate is not a recognized permissioned security --
        # it must be unknown (no-op), never a false 'blocked'.
        chain = FakeChain({(TOKEN, "paused()"): WORD_TRUE})
        self.assertIsNone(RwaReadinessSource(transport=chain).check({"token": TOKEN}, VERIFIED))

    def test_bad_token_address_is_none(self):
        src = RwaReadinessSource(transport=FakeChain({}))
        self.assertIsNone(src.check({"token": "not-an-address"}, VERIFIED))
        self.assertIsNone(src.check({}, VERIFIED))

    def test_declared_permissioned_without_payer_surfaces_unknown(self):
        # MUTATION: silently returning None here would hide that the wedge could not
        # run; a declared permissioned asset with no receiver must surface a note.
        src = RwaReadinessSource(transport=FakeChain({}))
        sig = src.check({"token": TOKEN, "standard": "ERC-3643"}, None)
        self.assertEqual(sig["grade"], "unknown")
        self.assertTrue(any("payer" in r for r in sig["reasons"]))

    def test_undeclared_without_payer_is_silent_none(self):
        src = RwaReadinessSource(transport=FakeChain({}))
        self.assertIsNone(src.check({"token": TOKEN}, None))

    def test_transport_raises_fail_open(self):
        # MUTATION: an unguarded transport call would crash the verdict; it must
        # fail open to None.
        def boom(to, data):
            raise RuntimeError("rpc down")
        src = RwaReadinessSource(transport=boom)
        self.assertIsNone(src.check({"token": TOKEN}, VERIFIED))

    def test_is_rwa_descriptor(self):
        self.assertTrue(is_rwa_descriptor({"token": TOKEN}))
        self.assertFalse(is_rwa_descriptor({"token": "x"}))
        self.assertFalse(is_rwa_descriptor(None))


class TestCombinedSource(unittest.TestCase):
    def test_dispatches_to_first_non_none(self):
        from rwa_readiness import CombinedRwaReadinessSource

        class NoneSrc:
            def check(self, acquires, payer=None, counterparty=None):
                return None

        class HitSrc:
            def check(self, acquires, payer=None, counterparty=None):
                return {"grade": "blocked", "standard": "x", "reasons": [], "signals": {}}

        c = CombinedRwaReadinessSource([NoneSrc(), HitSrc()])
        self.assertEqual(c.check({"token": TOKEN}, PAYEE)["grade"], "blocked")

    def test_skips_raising_source(self):
        from rwa_readiness import CombinedRwaReadinessSource

        class BoomSrc:
            def check(self, acquires, payer=None, counterparty=None):
                raise RuntimeError("boom")

        class HitSrc:
            def check(self, acquires, payer=None, counterparty=None):
                return {"grade": "ready", "standard": "x", "reasons": [], "signals": {}}

        c = CombinedRwaReadinessSource([BoomSrc(), HitSrc()])
        self.assertEqual(c.check({"token": TOKEN}, PAYEE)["grade"], "ready")

    def test_all_none_returns_none(self):
        from rwa_readiness import CombinedRwaReadinessSource

        class NoneSrc:
            def check(self, acquires, payer=None, counterparty=None):
                return None

        self.assertIsNone(CombinedRwaReadinessSource([NoneSrc()]).check({"token": TOKEN}, PAYEE))


class TestForecastIntegration(unittest.TestCase):
    """The wedge, end-to-end through blackwall.forecast."""

    def _known_good_payload(self, **extra):
        # MockReputationSource's KNOWNGOOD address graduates to GO.
        p = {"counterparty": "0xKNOWNGOOD000000000000000000000000000001",
             "amount": "0.09", "asset": "USDC", "chain": "base",
             "resource": "https://api.example.com/buy"}
        p.update(extra)
        return p

    def test_blocked_rwa_escalates_forecast_to_hold(self):
        import blackwall
        base = blackwall.forecast(self._known_good_payload(),
                                  blackwall.MockReputationSource())[0]
        self.assertEqual(base["verdict"], "GO")   # baseline is GO without the gate

        class BlockedSource:
            def check(self, acquires, payer, counterparty=None):
                return assess_transfer_readiness({"receiver_verified": False})

        payload = self._known_good_payload(
            payer=PAYEE, acquires={"token": TOKEN, "standard": "erc3643"})
        out = blackwall.forecast(payload, blackwall.MockReputationSource(),
                                 rwa_source=BlockedSource())[0]
        self.assertEqual(out["verdict"], "HOLD")
        self.assertEqual(
            out["signals"]["rwa_transfer_readiness"]["grade"], "blocked")

    def test_no_acquires_leaves_verdict_untouched(self):
        import blackwall

        class BoomSource:
            def check(self, acquires, payer, counterparty=None):
                raise AssertionError("must not be called without acquires")

        out = blackwall.forecast(self._known_good_payload(),
                                 blackwall.MockReputationSource(),
                                 rwa_source=BoomSource())[0]
        self.assertEqual(out["verdict"], "GO")
        self.assertNotIn("rwa_transfer_readiness", out["signals"])

    def test_source_raising_fails_open(self):
        import blackwall

        class BoomSource:
            def check(self, acquires, payer, counterparty=None):
                raise RuntimeError("boom")

        payload = self._known_good_payload(payer=PAYEE,
                                           acquires={"token": TOKEN})
        out = blackwall.forecast(payload, blackwall.MockReputationSource(),
                                 rwa_source=BoomSource())[0]
        self.assertEqual(out["verdict"], "GO")   # fail-open: unaffected


class TestValidateRequest(unittest.TestCase):
    def test_accepts_well_formed_acquires(self):
        import blackwall
        clean, err = blackwall.validate_request({
            "counterparty": PAYEE, "amount": "1.00", "asset": "USDC",
            "chain": "base", "acquires": {"token": TOKEN, "standard": "erc3643"}})
        self.assertIsNone(err)
        self.assertEqual(clean["acquires"]["token"], TOKEN)

    def test_rejects_non_object_acquires(self):
        import blackwall
        _, err = blackwall.validate_request({
            "counterparty": PAYEE, "amount": "1.00", "asset": "USDC",
            "chain": "base", "acquires": "AAPLx"})
        self.assertIsNotNone(err)

    def test_rejects_acquires_without_token(self):
        import blackwall
        _, err = blackwall.validate_request({
            "counterparty": PAYEE, "amount": "1.00", "asset": "USDC",
            "chain": "base", "acquires": {"standard": "erc3643"}})
        self.assertIsNotNone(err)

    def test_absent_acquires_is_none(self):
        import blackwall
        clean, err = blackwall.validate_request({
            "counterparty": PAYEE, "amount": "1.00", "asset": "USDC",
            "chain": "base"})
        self.assertIsNone(err)
        self.assertIsNone(clean["acquires"])


if __name__ == "__main__":
    unittest.main()
