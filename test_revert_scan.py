#!/usr/bin/env python3
"""
test_revert_scan.py -- TDD for the restriction-revert data tap. Each test names the
MUTATION it kills. The fixtures are REAL shapes captured from the live Blockscout spike
(the decoded `revert_reason` structure + the "exceeds balance" noise reason).
"""
import unittest

import revert_scan as rs
from revert_scan import (RevertScanner, classify_revert, extract_reason,
                         restriction_axis, summarize_reverts)

# The exact decoded shape the live spike returned for a failed transferShares.
BALANCE_REASON = {"method_call": "Error(string reason)", "method_id": "08c379a0",
                  "parameters": [{"name": "reason", "type": "string",
                                  "value": "ERC20: transfer amount exceeds balance"}]}


def _restr(msg):
    return {"parameters": [{"name": "reason", "type": "string", "value": msg}]}


class TestExtract(unittest.TestCase):
    def test_decoded_parameters(self):
        self.assertEqual(extract_reason(BALANCE_REASON),
                         "ERC20: transfer amount exceeds balance")

    def test_bare_string_and_none(self):
        self.assertEqual(extract_reason("Pausable: paused"), "Pausable: paused")
        self.assertIsNone(extract_reason(None))
        self.assertIsNone(extract_reason({"raw": None}))     # undecoded empty
        self.assertIsNone(extract_reason({"raw": "0x"}))

    def test_raw_hex_kept_as_unknown(self):
        # MUTATION: dropping an undecoded raw would lose a real (if unclassifiable) revert.
        self.assertEqual(extract_reason({"raw": "0xdeadbeef"}), "0xdeadbeef")

    def test_never_raises(self):
        for junk in (123, [], {"parameters": "nope"}, {"parameters": [1, 2]}):
            extract_reason(junk)      # must not raise


class TestClassify(unittest.TestCase):
    def test_balance_is_not_restriction(self):
        # THE CORE MUTATION: misclassifying a fat-finger "exceeds balance" as a restriction
        # would grade an issuer LOW for a caller's typo. This is the whole point.
        self.assertEqual(classify_revert("ERC20: transfer amount exceeds balance"),
                         "balance")

    def test_restriction_reasons(self):
        for msg in ("TREX: Transfer not possible", "recipient not whitelisted",
                    "identity is not verified", "Pausable: paused", "wallet is frozen",
                    "address is blacklisted", "sender not allowed", "KYC required",
                    "not compliant with transfer rules"):
            self.assertEqual(classify_revert(msg), "restriction", msg)

    def test_allowance_and_gas_and_other(self):
        self.assertEqual(classify_revert("ERC20: insufficient allowance"), "allowance")
        self.assertEqual(classify_revert("out of gas"), "gas")
        self.assertEqual(classify_revert("some unrelated failure"), "other")

    def test_empty_is_unknown(self):
        self.assertEqual(classify_revert(None), "unknown")
        self.assertEqual(classify_revert(""), "unknown")


class TestRealWorldCalibration(unittest.TestCase):
    """CALIBRATION against REAL contract revert strings, harvested live by simulating
    transfers from publicly OFAC/Circle-blacklisted addresses (eth_call with `from`) --
    not hand-written fixtures. These are the ground-truth labels for the classifier."""

    def test_real_usdc_blacklist_string_is_restriction(self):
        # Harvested live from USDC (0xa0b8...eb48) simulating a transfer FROM a
        # confirmed-blacklisted address (isBlacklisted() == true).
        self.assertEqual(classify_revert("Blacklistable: account is blacklisted"),
                         "restriction")

    def test_real_balance_string_is_not_restriction(self):
        # Harvested live from the same contract for a NON-blacklisted sender. The
        # TRUE-NEGATIVE control: the classifier must not confuse the two.
        self.assertEqual(classify_revert("ERC20: transfer amount exceeds balance"),
                         "balance")

    def test_real_reasonless_reverts_are_opaque_not_restriction(self):
        # MEASURED GAP: USDT's pre-0.8 blacklist throws INVALID with NO reason string.
        # It must NOT be silently bucketed as `other` (invisible) NOR as `restriction`
        # (fabricated evidence) -- it is `opaque`, an ACKNOWLEDGED under-count.
        for msg in ("invalid opcode: INVALID", "execution reverted"):
            self.assertEqual(classify_revert(msg), "opaque", msg)

    def test_opaque_never_inflates_restriction_evidence(self):
        # The safety property: a pile of opaque reverts must leave the axis DORMANT.
        ax = restriction_axis(100, summarize_reverts(["invalid opcode: INVALID"] * 40))
        self.assertEqual(ax["restriction_reverts"], 0)
        self.assertTrue(ax["dormant"])

    def test_restriction_wins_over_opaque_wording(self):
        # MUTATION: ordering opaque before restriction would swallow a real reason that
        # happens to contain "reverted".
        self.assertEqual(
            classify_revert("execution reverted: recipient not whitelisted"),
            "restriction")


class TestAuditRegressions(unittest.TestCase):
    """Regressions for bugs found by the adversarial audit sweep. Each names the bug."""

    def test_negation_words_are_not_restrictions(self):
        # AUDIT BUG (HIGH): naive substring matching classified messages that say the
        # OPPOSITE as restrictions -- "unrestricted" contains "restricted", "unpaused"
        # contains "paused", etc. -- fabricating restriction evidence against an issuer.
        for msg in ("transfer is unrestricted", "contract is unpaused",
                    "account is unfrozen", "address unblocked",
                    "sender is not blacklisted", "token is non-restricted"):
            self.assertNotEqual(classify_revert(msg), "restriction", msg)

    def test_real_restrictions_still_match_after_boundary_fix(self):
        # The fix must not break true positives (word boundaries + negator lookbehind).
        for msg in ("account is blacklisted", "wallet is frozen", "Pausable: paused",
                    "recipient not whitelisted", "trading is halted"):
            self.assertEqual(classify_revert(msg), "restriction", msg)

    def test_allowance_beats_balance_prefix(self):
        # AUDIT BUG (LOW): "transfer amount exceeds allowance" matched the broader
        # balance prefix "transfer amount exceeds" first and mislabeled as `balance`.
        self.assertEqual(classify_revert("ERC20: transfer amount exceeds allowance"),
                         "allowance")

    def test_axis_survives_non_numeric_counts(self):
        # AUDIT BUG (MEDIUM): revert summaries can come from an operator-edited JSON
        # file, so a string/list count must coerce -- not TypeError at server startup.
        for bad in ({"restriction": "5"}, {"restriction": [1]}, {"restriction": None},
                    {"restriction": 3.7}, "notadict", None, 42):
            ax = restriction_axis(100, bad)
            self.assertIsInstance(ax, dict)
        self.assertEqual(restriction_axis(100, {"restriction": "5"})["restriction_reverts"], 5)

    def test_axis_survives_bad_landed(self):
        for bad in ("100", None, -5, float("inf"), 3.5):
            self.assertIsInstance(restriction_axis(bad, {"restriction": 5}), dict)

    def test_scan_reports_truncation(self):
        # AUDIT (NO SILENT CAPS): when more failures exist than details fetched, the
        # under-count must be VISIBLE, else "few restrictions" is indistinguishable
        # from "we only looked at the first N".
        def fetch_txns(token, params):
            return ([{"hash": "0x%d" % i, "status": "error"} for i in range(500)], None)

        def fetch_tx(h):
            return {"revert_reason": _restr("recipient not whitelisted")}

        sc = RevertScanner("ethereum", fetch_txns=fetch_txns, fetch_tx=fetch_tx)
        out = sc.scan_token("0x" + "22" * 20, max_details=50)
        self.assertEqual(out["failed"], 500)
        self.assertEqual(out["sampled"], 50)
        self.assertTrue(out["truncated"])


class TestSummarize(unittest.TestCase):
    def test_counts_by_class(self):
        s = summarize_reverts(["Pausable: paused", "not whitelisted",
                               "ERC20: transfer amount exceeds balance", None])
        self.assertEqual(s["restriction"], 2)
        self.assertEqual(s["balance"], 1)
        self.assertEqual(s["unknown"], 1)
        self.assertEqual(s["total"], 4)


class TestAxis(unittest.TestCase):
    def test_dormant_below_evidence(self):
        # MUTATION: contributing on 1-2 reverts (noise) would false-flag. Must be dormant.
        ax = restriction_axis(100, {"restriction": 2})
        self.assertFalse(ax["evidence_sufficient"])
        self.assertTrue(ax["dormant"])

    def test_active_at_threshold_with_rate(self):
        ax = restriction_axis(97, {"restriction": 3})
        self.assertTrue(ax["evidence_sufficient"])
        self.assertFalse(ax["dormant"])
        self.assertEqual(ax["attempts"], 100)
        self.assertAlmostEqual(ax["restriction_revert_rate"], 0.03)

    def test_rate_none_when_no_data(self):
        ax = restriction_axis(0, {})
        self.assertIsNone(ax["restriction_revert_rate"])
        self.assertTrue(ax["dormant"])

    def test_balance_reverts_do_not_count(self):
        # A token with 50 balance reverts but 0 restriction reverts -> axis stays dormant.
        ax = restriction_axis(100, summarize_reverts(
            ["ERC20: transfer amount exceeds balance"] * 50))
        self.assertEqual(ax["restriction_reverts"], 0)
        self.assertTrue(ax["dormant"])


class TestScanner(unittest.TestCase):
    def test_scan_two_step_and_classify(self):
        # Injected transport mirrors the live two-step (list has null reason; detail has it).
        txns = [{"hash": "0x1", "status": "error"},
                {"hash": "0x2", "status": "ok"},
                {"hash": "0x3", "status": "error"}]
        details = {"0x1": {"revert_reason": _restr("recipient not whitelisted")},
                   "0x3": {"revert_reason": BALANCE_REASON}}

        def fetch_txns(token, params):
            return (txns, None)

        def fetch_tx(h):
            return details.get(h, {})

        sc = RevertScanner("ethereum", fetch_txns=fetch_txns, fetch_tx=fetch_tx)
        out = sc.scan_token("0x" + "22" * 20)
        self.assertEqual(out["failed"], 2)                 # only the 2 errors
        self.assertEqual(out["summary"]["restriction"], 1)  # whitelist
        self.assertEqual(out["summary"]["balance"], 1)      # exceeds balance

    def test_scan_failopen_on_transport_error(self):
        # MUTATION: a transport raise must not crash the scan (fail-open -> empties).
        def boom_txns(token, params):
            raise IOError("network")
        sc = RevertScanner("ethereum", fetch_txns=boom_txns, fetch_tx=lambda h: {})
        out = sc.scan_token("0x" + "22" * 20)
        self.assertEqual(out["failed"], 0)
        self.assertEqual(out["summary"]["total"], 0)

    def test_unknown_chain_no_base(self):
        sc = RevertScanner("nope")
        self.assertIsNone(sc.base)


if __name__ == "__main__":
    unittest.main()
