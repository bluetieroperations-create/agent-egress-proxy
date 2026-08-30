"""
Tests for payee_syntax.py -- is the address the agent is about to pay possible?

Each test states the mutation it kills. Pure and network-free.
"""
import json
import unittest

import payee_syntax as PSX

# The string a live seller actually advertises as its Solana payTo. An address
# with an environment variable concatenated onto it.
GLUED = ("2DgEL95L8DtaRb4ubYqrrnMbX7Zxgjxq7k8Ed9XAWYcp"
         "FACILITATOR_URL=https://x402.org/facilitator")
CLEAN_SOLANA = "2DgEL95L8DtaRb4ubYqrrnMbX7Zxgjxq7k8Ed9XAWYcp"
TRUNCATED_EVM = "0x8AC76a51cc950d9822D68b83fE43AD4843bA77E"      # 39, not 40
GOOD_EVM = "0x480CD46E6faDe651a0437DeaddA53D5c8e7D846A"


class TestTheCaseThatPromptedIt(unittest.TestCase):
    def test_the_real_glued_payee_is_malformed(self):
        # Kills: the whole module. This exact string is live in the corpus.
        self.assertEqual(PSX.assess_payee(GLUED)["grade"], PSX.MALFORMED)

    def test_the_clean_form_of_the_same_address_is_not(self):
        # Kills: matching on a prefix or length. The first 44 characters are
        # identical -- only the appended junk distinguishes them.
        self.assertNotEqual(PSX.assess_payee(CLEAN_SOLANA)["grade"], PSX.MALFORMED)

    def test_the_truncated_evm_shape_is_recorded_but_not_gated(self):
        # Kills: gating on it. 0 of 558 real payees are 0x-but-invalid, its only
        # real instance was an ASSET field, and gating it failed 15 tests across
        # 8 modules -- every one a synthetic placeholder. A rule whose only hits
        # are fixtures is not ready to refuse a payment.
        self.assertEqual(PSX.assess_payee(TRUNCATED_EVM)["grade"], PSX.INVALID_HEX)

    def test_invalid_hex_does_not_escalate(self):
        # Kills: folding invalid_hex into the gating branch, which would HOLD
        # every request in this repo's own test corpus.
        out = PSX.apply_payee_syntax(
            {"verdict": "GO", "reasons": [], "signals": {}},
            PSX.assess_payee("0xKNOWNGOOD000000000000000000000000000001"))
        self.assertEqual(out["verdict"], "GO")
        self.assertEqual(out["signals"]["payee_syntax"]["grade"], PSX.INVALID_HEX)

    def test_invalid_hex_is_still_reported(self):
        # Kills: silently swallowing it. Not gating is not the same as not saying.
        out = PSX.apply_payee_syntax(
            {"verdict": "GO", "reasons": [], "signals": {}},
            PSX.assess_payee(TRUNCATED_EVM))
        self.assertTrue(any("not a valid EVM address" in r for r in out["reasons"]))

    def test_the_reason_names_what_is_wrong_not_just_that_it_is_wrong(self):
        # Kills: a bare "invalid payee". The operator has to be able to act on
        # it, and the actionable part is WHICH characters cannot be there.
        reason = PSX.assess_payee(GLUED)["reasons"][0]
        self.assertIn("'://'", reason)
        self.assertIn("cannot arrive", reason)


class TestWhatItRefusesToJudge(unittest.TestCase):
    """The rule is chain-agnostic on purpose. Declining to judge a format we
    cannot check is the difference between a gate and a nuisance."""

    def test_non_evm_identifiers_are_unknown_not_malformed(self):
        # Kills: applying is_evm_address to everything, which would condemn
        # every Solana, Stellar and Algorand payee in the corpus.
        for payee in (CLEAN_SOLANA, "31566704",
                      "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN",
                      "BENrLoUbndxoNMUS5JXApGMtNykLjFXXixMtpDwDR9SP"):
            self.assertEqual(PSX.assess_payee(payee)["grade"], PSX.UNKNOWN, payee)

    def test_a_stellar_colon_form_is_not_condemned(self):
        # Kills: adding ':' to the impossible set. Stellar asset identifiers
        # legitimately carry one, and a colon is not evidence of anything.
        self.assertNotEqual(
            PSX.assess_payee("USDC:GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN")["grade"],
            PSX.MALFORMED)

    def test_a_checksummed_evm_address_is_ok(self):
        # Kills: a case-sensitive hex check. Live 402s return EIP-55.
        self.assertEqual(PSX.assess_payee(GOOD_EVM)["grade"], PSX.OK)

    def test_absent_or_unreadable_is_unknown_never_malformed(self):
        # Kills: treating a missing payee as a broken one. Absence is the
        # validator's problem, not this gate's, and fail-open is the posture.
        for payee in (None, "", "   ", 123, [], {}):
            self.assertEqual(PSX.assess_payee(payee)["grade"], PSX.UNKNOWN, payee)


class TestNoFalseFlagsOnRealPayees(unittest.TestCase):
    """Measured before shipping this on rather than behind a lock, the way
    sybil_ring graduated. If this ever fails, the rule got greedy."""

    def test_every_committed_directory_payee_passes(self):
        # Kills: any rule that flags a real payee. 266 of them, all harvested
        # from public on-chain history.
        with open("data/directory.json") as handle:
            payees = [row["payee"] for row in json.load(handle)]
        flagged = [p for p in payees
                   if PSX.assess_payee(p)["grade"] == PSX.MALFORMED]
        self.assertEqual(flagged, [], "false flags on real payees")
        self.assertGreater(len(payees), 200, "corpus shrank; re-check the claim")


class TestTheFold(unittest.TestCase):
    def _go(self):
        return {"verdict": "GO", "reasons": ["fine"], "signals": {}}

    def test_a_malformed_payee_escalates_go_to_hold(self):
        # Kills: recording the signal without gating -- the state this module
        # was written to change.
        out = PSX.apply_payee_syntax(self._go(), PSX.assess_payee(GLUED))
        self.assertEqual(out["verdict"], "HOLD")

    def test_it_never_produces_a_stop(self):
        # Kills: escalating to STOP. Defensible, deliberately declined: every
        # gate here except sanctions and payload mismatch is HOLD-only, and the
        # format rules could be surprised by a new chain.
        out = PSX.apply_payee_syntax(self._go(), PSX.assess_payee(GLUED))
        self.assertNotEqual(out["verdict"], "STOP")

    def test_it_never_upgrades_a_verdict(self):
        # Kills: rebuilding the verdict rather than only ever adding caution.
        for start in ("HOLD", "STOP"):
            out = PSX.apply_payee_syntax(
                {"verdict": start, "reasons": [], "signals": {}},
                PSX.assess_payee(GOOD_EVM))
            self.assertEqual(out["verdict"], start)

    def test_unknown_does_not_escalate(self):
        # Kills: gating on anything we could not actually check, which would
        # HOLD every non-EVM payment in the corpus.
        out = PSX.apply_payee_syntax(self._go(), PSX.assess_payee(CLEAN_SOLANA))
        self.assertEqual(out["verdict"], "GO")

    def test_the_signal_is_recorded_even_when_it_does_not_gate(self):
        # Kills: reporting only failures. A caller should be able to tell
        # "checked and fine" from "not checked".
        out = PSX.apply_payee_syntax(self._go(), PSX.assess_payee(GOOD_EVM))
        self.assertEqual(out["signals"]["payee_syntax"]["grade"], PSX.OK)

    def test_the_recorded_payee_is_a_redacted_hint_not_the_raw_string(self):
        # Kills: echoing an arbitrary merchant-supplied string into the verdict
        # and the logs at full length. Matches how secret_scan reports.
        out = PSX.apply_payee_syntax(self._go(), PSX.assess_payee(GLUED))
        self.assertLess(len(out["signals"]["payee_syntax"]["payee"]), len(GLUED))

    def test_gate_false_records_without_escalating(self):
        # Kills: ignoring the advisory mode the other folds all support.
        out = PSX.apply_payee_syntax(self._go(), PSX.assess_payee(GLUED), gate=False)
        self.assertEqual(out["verdict"], "GO")
        self.assertEqual(out["signals"]["payee_syntax"]["grade"], PSX.MALFORMED)

    def test_the_fold_does_not_mutate_its_input(self):
        # Kills: editing the caller's verdict in place, which every other fold
        # in this codebase is careful not to do.
        original = self._go()
        PSX.apply_payee_syntax(original, PSX.assess_payee(GLUED))
        self.assertEqual(original["verdict"], "GO")

    def test_junk_signals_and_verdicts_are_returned_untouched(self):
        # Kills: raising on the hot path. `forecast` has no try/except here, so
        # a raise would surface as a 503 on a request it could otherwise answer.
        for bad in (None, {}, [], "x", {"grade": "weird"}):
            self.assertEqual(PSX.apply_payee_syntax(self._go(), bad)["verdict"], "GO")
        for verdict in (None, "x", 7, []):
            PSX.apply_payee_syntax(verdict, PSX.assess_payee(GLUED))


class TestNeverRaises(unittest.TestCase):
    def test_hostile_payees(self):
        # Kills: assuming anything about a field the merchant controls.
        for payee in ("\x00" * 50, "a" * 100000, "0x", "0X" + "f" * 40,
                      "://", "=", "\n\n\n", "0x" + "g" * 40, "٣" * 44):
            PSX.assess_payee(payee)


if __name__ == "__main__":
    unittest.main()
