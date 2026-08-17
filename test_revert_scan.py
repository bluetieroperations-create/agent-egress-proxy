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
