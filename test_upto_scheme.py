"""
Tests for upto_scheme.py -- the x402 `upto` (metered) scheme and the Permit2
allowance it requires. Each test names the mutation it kills.
"""
import unittest

import upto_scheme as U

MAX_UINT256 = (1 << 256) - 1
MAX_UINT160 = (1 << 160) - 1


class TestIsUpto(unittest.TestCase):
    def test_recognises_the_scheme(self):
        self.assertTrue(U.is_upto("upto"))
        self.assertTrue(U.is_upto("UPTO"))       # case-insensitive like the rest of x402
        self.assertTrue(U.is_upto("  upto  "))

    def test_everything_else_is_not_upto(self):
        # kills: treating `exact` as metered, which would relax the amount check
        for s in ("exact", "", None, "batch-settlement", 12, [], "uptoo"):
            self.assertFalse(U.is_upto(s), s)


class TestParseAllowance(unittest.TestCase):
    """Never raises; a value we cannot read is None (unknown), not 0 (safe)."""
    def test_decimal_and_hex(self):
        self.assertEqual(U.parse_allowance("1000000"), 1000000)
        self.assertEqual(U.parse_allowance(1000000), 1000000)
        self.assertEqual(U.parse_allowance("0x0f4240"), 1000000)

    def test_junk_is_unknown_not_zero(self):
        # kills: returning 0 for junk, which would read as "no allowance granted"
        # and silently PASS the exposure check
        for v in (None, "", "abc", "0x", [], {}, True, False, -1):
            self.assertIsNone(U.parse_allowance(v), v)


class TestAssessUpto(unittest.TestCase):
    """
    The security core. AWS documents granting an UNLIMITED Permit2 allowance as a
    normal option for `upto`
    (115792089237316195423570985008687907853269984665640564039457584007913129639935),
    and `approve` SETS rather than adds. A spending cap cannot see this: an
    allowance is not a spend, so a $1 session budget coexists with an unlimited
    approval over the wallet's whole USDC balance.

    Mutation notes:
      - return ok for an unlimited allowance -> test_unlimited_is_a_hard_stop FAILS.
      - use > instead of >= at the uint160 boundary -> test_permit2_uint160_max FAILS.
      - drop the ratio gate -> test_allowance_far_above_the_ceiling_holds FAILS.
      - screen a payment that states no allowance ->
        test_no_allowance_is_not_assessed FAILS.
      - key the screen off the scheme name again ->
        test_permit2_on_exact_is_screened FAILS.
    """
    def test_no_allowance_is_not_assessed(self):
        # An `exact` payment that states no allowance creates no Permit2 exposure,
        # so there is nothing to screen. kills: screening on absent input, which
        # would attach a finding to every ordinary EIP-3009 payment.
        r = U.assess_upto("exact", max_amount="1000", allowance=None)
        self.assertEqual(r["status"], "not_applicable")
        self.assertFalse(r["mismatches"])
        self.assertFalse(r["warnings"])

    def test_permit2_on_exact_is_screened(self):
        """REGRESSION. The screen used to key off `is_upto(scheme)`.

        Permit2 is used with `exact` too -- sellers advertise it as
        `extra.assetTransferMethod: "permit2-exact"` -- so an UNLIMITED allowance
        on an `exact` payment was not screened at all: not gated, not warned, not
        recorded. End to end on the shipped code that request returned a clean GO,
        which is the drainer pattern calldata.py hard-STOPs as calldata walking
        through as a payment intent.

        The exposure is created by the ALLOWANCE, not the scheme name.

        The previous test here asserted the buggy behaviour -- `assess_upto("exact",
        allowance=MAX_UINT256)` -> not_applicable with no mismatches -- and is
        replaced rather than kept, because it encoded the defect.

        kills: restoring the `if not is_upto(scheme): return` early exit.
        """
        for scheme in ("exact", "permit2-exact", "batch-settlement", None):
            r = U.assess_upto(scheme, max_amount="1000", allowance=MAX_UINT256)
            self.assertEqual(r["status"], "unlimited", "scheme=%r" % (scheme,))
            self.assertTrue(r["mismatches"], "scheme=%r" % (scheme,))

    def test_upto_with_no_allowance_stays_unknown(self):
        # `upto` meters THROUGH Permit2, so a missing allowance is a gap in what we
        # know, not an absence of exposure. kills: collapsing upto's absent-allowance
        # case into not_applicable when the scheme check moved.
        self.assertEqual(
            U.assess_upto("upto", max_amount="1000", allowance=None)["status"],
            "unknown")

    def test_unlimited_is_a_hard_stop(self):
        r = U.assess_upto("upto", max_amount="1000", allowance=MAX_UINT256)
        self.assertEqual(r["status"], "unlimited")
        self.assertTrue(r["mismatches"])
        self.assertIn("unlimited", r["mismatches"][0].lower())

    def test_permit2_uint160_max(self):
        # Permit2's own "unlimited" convention is uint160-max, not uint256-max.
        r = U.assess_upto("upto", max_amount="1000", allowance=MAX_UINT160)
        self.assertEqual(r["status"], "unlimited")

    def test_allowance_equal_to_the_ceiling_is_fine(self):
        r = U.assess_upto("upto", max_amount="1000", allowance=1000)
        self.assertEqual(r["status"], "ok")
        self.assertFalse(r["mismatches"])
        self.assertFalse(r["warnings"])

    def test_allowance_far_above_the_ceiling_holds(self):
        # 1000x the quoted ceiling: legal, but the exposure is not what was quoted
        r = U.assess_upto("upto", max_amount="1000", allowance=1000 * 1000)
        self.assertEqual(r["status"], "excessive")
        self.assertTrue(r["warnings"])
        self.assertFalse(r["mismatches"], "excessive must HOLD, never STOP")

    def test_modest_headroom_is_allowed(self):
        # kills: gating any allowance above the exact ceiling -- a wallet
        # legitimately approves once and meters many calls under it
        r = U.assess_upto("upto", max_amount="1000", allowance=1000 * 4)
        self.assertEqual(r["status"], "ok")

    def test_no_allowance_supplied_is_unknown_and_fails_open(self):
        # kills: treating an absent allowance as unlimited (false STOP) or as 0
        r = U.assess_upto("upto", max_amount="1000", allowance=None)
        self.assertEqual(r["status"], "unknown")
        self.assertFalse(r["mismatches"])

    def test_an_unreadable_ceiling_says_the_ratio_check_did_not_run(self):
        """`unknown` must not be silent.

        Found by the cold-start session auditing this module against their corpus
        work: a ceiling quoted in HUMAN units ("0.001" rather than "1000") is not
        an integer, so it parses to None and the 100x ratio check is skipped --
        measured, not hypothetical, since 1 of 363 live quotes is advertised that
        way. Skipping is CORRECT (a human ceiling against an atomic allowance is
        not a comparison), but doing it silently is the same defect shape as
        amount_status="verified" on an unverified scale: a check that did not run,
        reported as though nothing was wrong. The unlimited hard-STOP is
        unaffected either way -- it fires before the ceiling is consulted.

        Mutation: drop the note -> this FAILS.
        """
        r = U.assess_upto("upto", max_amount="0.001", allowance=10 ** 9)
        self.assertEqual(r["status"], "unknown")
        self.assertFalse(r["mismatches"], "must not gate on an unknown ceiling")
        self.assertTrue(r["warnings"], "the skipped check must be visible")
        self.assertIn("ceiling", r["warnings"][0].lower())

    def test_unlimited_still_stops_with_an_unreadable_ceiling(self):
        # kills: moving the unlimited check below the ceiling parse, which would
        # let a human-unit quote disable the hard STOP too
        r = U.assess_upto("upto", max_amount="0.001", allowance=(1 << 256) - 1)
        self.assertEqual(r["status"], "unlimited")
        self.assertTrue(r["mismatches"])

    def test_no_allowance_stays_quiet(self):
        # kills: warning when the caller simply said nothing -- that is not a
        # skipped check, it is an absent input, and noise there is not free
        r = U.assess_upto("upto", max_amount="1000", allowance=None)
        self.assertEqual(r["status"], "unknown")
        self.assertFalse(r["warnings"])

    def test_unreadable_ceiling_does_not_crash_or_gate(self):
        r = U.assess_upto("upto", max_amount=None, allowance=1000)
        self.assertEqual(r["status"], "unknown")
        self.assertFalse(r["mismatches"])

    def test_never_raises(self):
        for scheme in ("upto", "exact", None, 5):
            for amt in (None, "", "abc", 0, -1, 10**40, [], {}):
                for allow in (None, "", "abc", 0, -1, MAX_UINT256, [], {}):
                    U.assess_upto(scheme, max_amount=amt, allowance=allow)


class TestExcessiveGate(unittest.TestCase):
    """`excessive` may escalate GO -> HOLD, behind a reversibility lock.

    AUDIT (2026-08-29). CLAUDE.md and the shipping PR both described ">100x the
    ceiling -> HOLD only". It did not HOLD. `assess_upto` put it in `warnings`,
    and blackwall.forecast only extends `reasons` with warnings -- it never moves
    the verdict. So a 10^6x disproportionate allowance returned GO with a note
    most callers never read. Confirmed live in production before the fix.

    A control documented as gating that does not gate is the defect class this
    codebase keeps finding, and this one was mine.

    Graduated the way sybil_ring and issuer_trust were: behind a lock, DEFAULT
    OFF, so nothing changes on live traffic until the false-HOLD rate is measured
    on the shipped corpus. Flip EXCESSIVE_GATES to enable.

    Mutation notes:
      - gate while the lock is off -> test_lock_off_is_advisory FAILS.
      - ignore the lock when on -> test_lock_on_escalates FAILS.
      - escalate a STOP or a HOLD -> test_only_go_is_escalated FAILS.
    """
    def _signal(self, allowance=10 ** 9, ceiling="1000"):
        return U.assess_upto("upto", max_amount=ceiling, allowance=allowance)

    def test_lock_off_is_advisory(self):
        self.assertFalse(U.EXCESSIVE_GATES, "the lock must ship OFF")
        v = U.apply_excessive(
            {"verdict": "GO", "reasons": []}, self._signal(), gates=False)
        self.assertEqual(v["verdict"], "GO")
        self.assertTrue(any("allowance" in r.lower() for r in v["reasons"]),
                        "still reported, just not gating")

    def test_lock_on_escalates(self):
        v = U.apply_excessive(
            {"verdict": "GO", "reasons": []}, self._signal(), gates=True)
        self.assertEqual(v["verdict"], "HOLD")
        self.assertTrue(any("escalated GO->HOLD" in r for r in v["reasons"]))

    def test_only_go_is_escalated(self):
        # kills: downgrading a STOP to HOLD, or re-escalating a HOLD
        for start in ("STOP", "HOLD"):
            v = U.apply_excessive({"verdict": start, "reasons": []},
                                  self._signal(), gates=True)
            self.assertEqual(v["verdict"], start)

    def test_ok_and_unknown_never_escalate(self):
        # NB the third case used to be `exact` with a 10**9 allowance against a 1000
        # ceiling -- 10^6x. It sat here as a "never escalates" example only because
        # `exact` was not screened at all; now that the screen keys off the
        # allowance it is correctly `excessive`, so it belongs in the gating test,
        # not this one. Replaced with an `exact` payment carrying a PROPORTIONATE
        # allowance, which is what this test was always meant to assert.
        for status_signal in (U.assess_upto("upto", max_amount="1000", allowance=1000),
                              U.assess_upto("upto", max_amount="1000", allowance=None),
                              U.assess_upto("exact", max_amount="1000", allowance=None),
                              U.assess_upto("exact", max_amount="1000", allowance=3000)):
            v = U.apply_excessive({"verdict": "GO", "reasons": []},
                                  status_signal, gates=True)
            self.assertEqual(v["verdict"], "GO", status_signal["status"])

    def test_unlimited_is_untouched_by_the_lock(self):
        # kills: routing the hard STOP through the lock -- an unlimited approval
        # is a mismatch and STOPs whether or not `excessive` gates
        sig = U.assess_upto("upto", max_amount="1000", allowance=(1 << 256) - 1)
        self.assertTrue(sig["mismatches"])
        for gates in (True, False):
            v = U.apply_excessive({"verdict": "GO", "reasons": []}, sig, gates=gates)
            self.assertEqual(v["verdict"], "GO",
                             "apply_excessive handles `excessive` only; the hard "
                             "STOP travels as a mismatch")

    def test_does_not_duplicate_a_warning_the_caller_already_added(self):
        """`forecast` already extends reasons with upto_check["warnings"] via
        sim_warnings, so re-adding them here printed the same line twice.

        Found by auditing this change: the operator sees one exposure reported as
        two, which is how a reason list stops being read.

        Mutation: extend reasons with signal["warnings"] here -> this FAILS.
        """
        sig = self._signal()
        already = list(sig["warnings"])          # the caller added these
        v = U.apply_excessive({"verdict": "GO", "reasons": already},
                              sig, gates=False)
        self.assertEqual(len(v["reasons"]), len(already),
                         "apply_excessive must not restate the caller's warnings")

    def test_still_reports_when_the_caller_added_nothing(self):
        # kills: relying on the caller to have added the warning -- a direct
        # caller of apply_excessive must still see why it gated
        v = U.apply_excessive({"verdict": "GO", "reasons": []},
                              self._signal(), gates=True)
        self.assertTrue(any("escalated GO->HOLD" in r for r in v["reasons"]))

    def test_never_raises_on_a_malformed_verdict(self):
        """A fold must not turn a recoverable verdict into a 503.

        Found by auditing this change: `dict(v.get("signals") or {})` raised on a
        non-dict `signals`, and `dict(verdict)` raised on a non-dict verdict --
        6 of 64 malformed combinations. Every other fold here is documented as
        never raising, and `forecast` has no try/except around this one, so the
        exception would surface as a 503 from a request the engine could have
        answered.

        Mutation: drop the isinstance guards -> this FAILS.
        """
        sig = self._signal()
        for verdict in (None, {}, {"verdict": None}, {"reasons": "notalist"},
                        {"signals": "nope"}, 5, [], "x", 1.5, True):
            for signal in (None, {}, sig, {"status": "excessive"},
                           {"status": "excessive", "warnings": "str"},
                           {"status": 123}, [], "x", 7):
                U.apply_excessive(verdict, signal, gates=True)
                U.apply_excessive(verdict, signal, gates=False)

    def test_a_malformed_verdict_is_returned_untouched(self):
        # kills: manufacturing a verdict out of junk -- if we cannot read it, the
        # caller's object is what it was
        for verdict in (None, 5, "x", []):
            self.assertIs(U.apply_excessive(verdict, self._signal(), gates=True),
                          verdict)

    def test_pure_does_not_mutate_the_input(self):
        v0 = {"verdict": "GO", "reasons": []}
        U.apply_excessive(v0, self._signal(), gates=True)
        self.assertEqual(v0["verdict"], "GO")
        self.assertEqual(v0["reasons"], [])


class TestAllowanceFromRequest(unittest.TestCase):
    """Read the allowance wherever a caller plausibly puts it -- our own field, or
    the AWS AgentCore spelling, which is the shape agents will actually hold."""
    def test_our_snake_case_field(self):
        self.assertEqual(U.allowance_from_request({"permit2_allowance": "500"}), 500)

    def test_aws_agentcore_spelling(self):
        # paymentInput.cryptoX402.permit2AllowanceLimit
        self.assertEqual(U.allowance_from_request(
            {"permit2AllowanceLimit": "500"}), 500)

    def test_nested_aws_shape(self):
        self.assertEqual(U.allowance_from_request(
            {"cryptoX402": {"permit2AllowanceLimit": "500"}}), 500)

    def test_absent_is_none(self):
        # kills: defaulting to 0, which reads as "no approval" and passes silently
        for body in ({}, None, {"other": 1}, [], "x"):
            self.assertIsNone(U.allowance_from_request(body), body)


class TestSchemeFromRequest(unittest.TestCase):
    def test_top_level_scheme(self):
        self.assertEqual(U.scheme_from_request({"scheme": "upto"}), "upto")

    def test_from_the_402_accepts_entry(self):
        # a challenge carries the scheme in accepts[]; the first entry is the one
        # a client pays, matching how the rest of the engine reads accepts
        self.assertEqual(U.scheme_from_request(
            {"accepts": [{"scheme": "upto", "maxAmountRequired": "3495"}]}), "upto")

    def test_absent_is_none(self):
        for body in ({}, None, {"accepts": []}, {"accepts": "x"}):
            self.assertIsNone(U.scheme_from_request(body), body)


class TestFoldsIntoTheVerdict(unittest.TestCase):
    """End-to-end through `forecast`: an unlimited Permit2 allowance on an `upto`
    payment must reach a hard STOP, and a sane one must not gate.

    Mutation: drop the upto fold from blackwall.forecast -> these FAIL.
    """
    def _src(self):
        class Src:
            def lookup(self, cp):
                return {"settlements": 500, "distinct_payers": 80,
                        "dispute_rate": 0.0, "median_amount": "0.001",
                        "first_seen_days": 400}
        return Src()

    def _body(self, **extra):
        b = {"counterparty": "0x" + "11" * 20, "amount": "0.001",
             "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
             "chain": "base", "scheme": "upto"}
        b.update(extra)
        return b

    def test_unlimited_allowance_hard_stops(self):
        from blackwall import forecast
        v, err = forecast(self._body(permit2AllowanceLimit=str(MAX_UINT256)),
                          self._src(), verify_signer=False)
        self.assertIsNone(err)
        self.assertEqual(v["verdict"], "STOP")
        self.assertTrue(v["hard_stop"])

    def test_sane_allowance_does_not_gate(self):
        from blackwall import forecast
        v, err = forecast(self._body(permit2AllowanceLimit="1000",
                                     accepts=[{"scheme": "upto",
                                               "maxAmountRequired": "1000"}]),
                          self._src(), verify_signer=False)
        self.assertIsNone(err)
        self.assertNotEqual(v["verdict"], "STOP")

    def test_unlimited_allowance_on_exact_is_a_hard_stop(self):
        """REGRESSION, end to end. This test previously asserted the OPPOSITE, on
        the stated rationale that "the field is meaningless for `exact`". That
        rationale was wrong: Permit2 is used with `exact` too, advertised as
        `extra.assetTransferMethod: "permit2-exact"`. An unlimited approval over
        the whole balance is the same exposure whatever the scheme is called, and
        under the old behaviour this exact request returned a clean GO.

        kills: restoring the scheme-keyed early exit in assess_upto.
        """
        from blackwall import forecast
        v, err = forecast(self._body(scheme="exact",
                                     permit2AllowanceLimit=str(MAX_UINT256)),
                          self._src(), verify_signer=False)
        self.assertIsNone(err)
        self.assertEqual(v["verdict"], "STOP")

    def test_proportionate_allowance_on_exact_does_not_gate(self):
        # The restraint half: widening the screen must not start blocking ordinary
        # `exact` payments that grant a sane allowance.
        # kills: gating on the mere PRESENCE of an allowance rather than its size
        from blackwall import forecast
        v, err = forecast(self._body(scheme="exact", permit2AllowanceLimit="3000"),
                          self._src(), verify_signer=False)
        self.assertIsNone(err)
        self.assertNotEqual(v["verdict"], "STOP")


class TestUptoBillingSemantics(unittest.TestCase):
    """`upto` means pay AT MOST the quote. x402.payment_satisfies had a dormant
    non-exact branch demanding AT LEAST -- inverted. Unreachable today because we
    only ever issue `exact` requirements ourselves, but wrong if ever enabled.

    Mutation: restore `elif value < required` -> these FAIL.
    """
    def _req(self, scheme, amount):
        return {"scheme": scheme, "network": "base", "maxAmountRequired": amount,
                "payTo": "0x" + "11" * 20,
                "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"}

    def _payment(self, value, scheme="upto"):
        return {"x402Version": 2, "scheme": scheme, "network": "base",
                "payload": {"authorization": {
                    "from": "0x" + "22" * 20, "to": "0x" + "11" * 20,
                    "value": str(value), "validAfter": "0",
                    "validBefore": "99999999999", "nonce": "0x" + "33" * 32}}}

    def test_under_the_ceiling_is_valid_for_upto(self):
        import x402
        ok, why = x402.payment_satisfies(self._payment(500), self._req("upto", "1000"))
        self.assertTrue(ok, why)

    def test_over_the_ceiling_is_rejected_for_upto(self):
        import x402
        ok, why = x402.payment_satisfies(self._payment(1500), self._req("upto", "1000"))
        self.assertFalse(ok)
        self.assertIn("exceeds", why.lower())

    def test_exact_still_requires_equality(self):
        # kills: relaxing `exact` into `upto` semantics, which would accept an
        # underpay the real facilitator rejects
        import x402
        for v in (500, 1500):
            ok, _ = x402.payment_satisfies(self._payment(v, "exact"),
                                           self._req("exact", "1000"))
            self.assertFalse(ok, v)
        ok, why = x402.payment_satisfies(self._payment(1000, "exact"),
                                         self._req("exact", "1000"))
        self.assertTrue(ok, why)


if __name__ == "__main__":
    unittest.main()


class TestCeilingIsActuallyReadable(unittest.TestCase):
    """AUDIT 2026-08-29: the ratio gate could not read the ceiling on real
    challenges, so it never ran. Two causes, both fixed here.
    """

    def test_human_unit_ceiling_is_scaled_when_decimals_are_known(self):
        # Kills: dropping parse_ceiling's scaling branch. 1 of 363 live quotes is
        # written in human units, and it switched the ratio check off entirely.
        self.assertEqual(U.parse_ceiling("0.00335", 6), 3350)

    def test_human_unit_ceiling_without_decimals_stays_unknown(self):
        # Kills: guessing a scale. Unknown decimals must fail OPEN, as before.
        self.assertIsNone(U.parse_ceiling("0.00335", None))

    def test_an_integer_is_never_reinterpreted_as_human_units(self):
        # Kills: scaling every value. Treating an atomic 1000000 as human would
        # multiply the ceiling by 10^decimals and SUPPRESS the warning -- the
        # dangerous direction.
        self.assertEqual(U.parse_ceiling("1000000", 6), 1000000)
        self.assertEqual(U.parse_ceiling(1000000, 18), 1000000)

    def test_the_ratio_gate_now_fires_on_a_human_unit_quote(self):
        # Kills: fixing parse_ceiling but not threading it into assess_upto.
        r = U.assess_upto("upto", max_amount="0.00335", allowance=str(3350 * 1000),
                          decimals=6)
        self.assertEqual(r["status"], "excessive")

    def test_same_quote_without_decimals_is_unknown_not_excessive(self):
        # Kills: defaulting decimals to 6. Fail-open is the posture; a guessed
        # scale would make the warning depend on a number nobody verified.
        r = U.assess_upto("upto", max_amount="0.00335", allowance=str(3350 * 1000))
        self.assertEqual(r["status"], "unknown")

    def test_unlimited_still_stops_regardless_of_the_ceiling(self):
        # Kills: routing the unlimited hard-STOP through the ceiling. It needs
        # only the allowance and must fire even when the quote is unreadable.
        r = U.assess_upto("upto", max_amount="not-a-number", allowance=str(1 << 129))
        self.assertEqual(r["status"], "unlimited")
        self.assertTrue(r["mismatches"])

    def test_parse_ceiling_never_raises(self):
        # Kills: letting a crafted quote crash the gate.
        for junk in (None, "", [], {}, True, "0x" + "f" * 64, "1e999", "-5",
                     "." , "..", "0." + "9" * 100):
            U.parse_ceiling(junk, 6)
            U.parse_ceiling(junk, None)


class TestUptoCeilingReadsTheV2FieldName(unittest.TestCase):
    """AUDIT 2026-08-29 (MEDIUM): `_upto_ceiling` read only `maxAmountRequired`,
    the v1 field name. x402 v2 carries the quote in `amount`, and live sellers
    use the v2 spelling 69 to 4 -- so on nearly every real challenge the ceiling
    was None, the status was `unknown`, and the ratio gate never ran.
    """

    def test_v2_amount_is_read(self):
        # Kills: reverting to maxAmountRequired-only, which makes the gate inert
        # on the overwhelming majority of live challenges.
        import blackwall
        self.assertEqual(
            blackwall._upto_ceiling({"accepts": [{"amount": "1000"}]}, {}), "1000")

    def test_v1_max_amount_required_still_works(self):
        # Kills: swapping one field name for the other instead of trying both.
        import blackwall
        self.assertEqual(
            blackwall._upto_ceiling({"accepts": [{"maxAmountRequired": "1000"}]}, {}),
            "1000")

    def test_v2_wins_when_a_challenge_carries_both(self):
        # Kills: preferring v1. Mirrors x402._req_amount, so the two agree on
        # which field names a quote.
        import blackwall
        self.assertEqual(
            blackwall._upto_ceiling(
                {"accepts": [{"amount": "7", "maxAmountRequired": "9"}]}, {}), "7")


class TestCeilingCannotBeInflatedToSilenceTheWarning(unittest.TestCase):
    """AUDIT 2026-08-29 (MEDIUM, in parse_ceiling's first version): the scaling
    branch accepted anything `Decimal` accepted. The ceiling comes from the 402
    challenge -- the screened party's own input -- and INFLATING it is exactly
    how the excessive-allowance warning gets switched off.
    """

    ALLOW = str(10 ** 9)          # far beyond any honest ceiling here

    def _status(self, ceiling):
        return U.assess_upto("upto", max_amount=ceiling,
                             allowance=self.ALLOW, decimals=6)["status"]

    def test_exponent_notation_cannot_manufacture_a_huge_ceiling(self):
        # Kills: passing the raw value to Decimal. "1e999" scaled to a
        # 1000-digit ceiling and the warning went quiet (status "ok").
        self.assertIsNone(U.parse_ceiling("1e999", 6))
        self.assertEqual(self._status("1e999"), "unknown")

    def test_plausible_exponent_is_also_refused(self):
        # Kills: blocking only absurd exponents. "1e6" is the dangerous one --
        # it reads as a real price while inflating an atomic quote by 10^6.
        self.assertIsNone(U.parse_ceiling("1e6", 6))

    def test_a_real_human_quote_still_scales(self):
        # Kills: tightening the pattern until the case this exists for stops
        # working. This is the shape one live seller actually sends.
        self.assertEqual(U.parse_ceiling("0.00335", 6), 3350)
        self.assertEqual(self._status("0.00335"), "excessive")

    def test_only_ascii_digits_qualify(self):
        # Kills: using `\d`, which matches other scripts' digits -- int("٣")
        # is 3, so a quote's value would depend on the script it was written in.
        self.assertIsNone(U.parse_ceiling("٣.٤", 6))

    def test_non_price_forms_stay_unknown(self):
        # Kills: widening the pattern. None of these is a price, and each one
        # that parsed would be an attacker-chosen ceiling.
        for junk in ("inf", "nan", "NaN", "-1.5", ".5", "1.", "0x1.8",
                     "1_0.5", "+1.5", "1.5e3", ""):
            self.assertIsNone(U.parse_ceiling(junk, 6), junk)

    def test_a_bool_is_never_a_ceiling(self):
        # Kills: an isinstance(x, int) check that lets bool through -- True
        # would become 10^decimals, a ceiling out of nothing.
        self.assertIsNone(U.parse_ceiling(True, 6))
        self.assertIsNone(U.parse_ceiling(False, 6))
