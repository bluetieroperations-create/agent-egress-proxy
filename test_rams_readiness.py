#!/usr/bin/env python3
"""
test_rams_readiness.py -- TDD for the dormant-but-ready ERC-8226 (RAMS) source. Each
test names the MUTATION it kills.
"""
import unittest

import rams_readiness as rams
from rams_readiness import (RamsReadinessSource, assess_mandate, decode_allowed_reason,
                            encode_can_execute)
from rwa_readiness import apply_rwa_readiness

REG = "0x" + "11" * 20
AGENT = "0x" + "22" * 20
PRINCIPAL = "0x" + "33" * 20
TOKEN = "0x" + "44" * 20

WORD_T = "0x" + "00" * 31 + "01"
WORD_F = "0x" + "00" * 32


def _two_words(allowed, reason):
    return ("0x" + ("00" * 31 + "01" if allowed else "00" * 32)
            + format(reason, "064x"))


class TestEncode(unittest.TestCase):
    def test_selector_and_length(self):
        data = encode_can_execute(AGENT, PRINCIPAL, TOKEN, "0xa9059cbb", 0)
        self.assertTrue(data.startswith(rams.selector(
            "canExecute(address,address,address,bytes32,uint256)")))
        self.assertEqual(len(data), 10 + 64 * 5)      # 5 static words

    def test_bytes32_left_aligned(self):
        # MUTATION: right-aligning the action selector would encode the wrong bytes32.
        data = encode_can_execute(AGENT, PRINCIPAL, TOKEN, "0xa9059cbb", 0)
        action_word = data[10 + 64 * 3:10 + 64 * 4]
        self.assertTrue(action_word.startswith("a9059cbb"))
        self.assertTrue(action_word.endswith("0000"))


class TestDecode(unittest.TestCase):
    def test_allowed_true_false(self):
        self.assertEqual(decode_allowed_reason(_two_words(True, 0)), (True, 0))
        self.assertEqual(decode_allowed_reason(_two_words(False, 3)), (False, 3))

    def test_single_word_bool_reason_none(self):
        self.assertEqual(decode_allowed_reason(WORD_T), (True, None))

    def test_empty_none(self):
        self.assertEqual(decode_allowed_reason("0x"), (None, None))


class TestAssess(unittest.TestCase):
    def test_not_allowed_blocked(self):
        r = assess_mandate(False, 3)
        self.assertEqual(r["grade"], "blocked")
        self.assertTrue(any("reason code 3" in x for x in r["reasons"]))

    def test_allowed_ready(self):
        self.assertEqual(assess_mandate(True)["grade"], "ready")

    def test_none_unknown(self):
        self.assertEqual(assess_mandate(None)["grade"], "unknown")


class TestDormancy(unittest.TestCase):
    def _src(self, result):
        return RamsReadinessSource(transport=lambda to, data: result)

    def test_no_registry_is_dormant_none(self):
        # MUTATION: firing without an advertised registry would call eth_call on garbage.
        src = self._src(_two_words(False, 1))
        self.assertIsNone(src.check({"token": TOKEN, "principal": PRINCIPAL}, payer=AGENT))

    def test_missing_principal_dormant(self):
        src = self._src(_two_words(False, 1))
        self.assertIsNone(src.check({"token": TOKEN, "mandate_registry": REG}, payer=AGENT))

    def test_activates_when_registry_advertised_and_blocks(self):
        src = self._src(_two_words(False, 4))
        sig = src.check({"token": TOKEN, "mandate_registry": REG, "principal": PRINCIPAL},
                        payer=AGENT)
        self.assertEqual(sig["grade"], "blocked")
        self.assertEqual(sig["signals"]["standard"], "erc8226-rams")

    def test_activates_and_allows(self):
        src = self._src(_two_words(True, 0))
        sig = src.check({"token": TOKEN, "mandate_registry": REG, "principal": PRINCIPAL},
                        payer=AGENT)
        self.assertEqual(sig["grade"], "ready")

    def test_operator_default_registry_activates(self):
        src = RamsReadinessSource(transport=lambda to, data: _two_words(False, 2),
                                  default_registry=REG)
        sig = src.check({"token": TOKEN, "principal": PRINCIPAL}, payer=AGENT)
        self.assertEqual(sig["grade"], "blocked")

    def test_unreadable_registry_dormant(self):
        # registry advertised but eth_call returns nothing -> stay no-op (None).
        src = self._src("0x")
        self.assertIsNone(src.check(
            {"token": TOKEN, "mandate_registry": REG, "principal": PRINCIPAL}, payer=AGENT))

    def test_transport_raises_fail_open(self):
        def boom(to, data):
            raise RuntimeError("rpc down")
        src = RamsReadinessSource(transport=boom)
        self.assertIsNone(src.check(
            {"token": TOKEN, "mandate_registry": REG, "principal": PRINCIPAL}, payer=AGENT))

    def test_folds_through_apply_rwa_readiness(self):
        # A blocked mandate escalates a GO to HOLD via the SHARED fold.
        src = self._src(_two_words(False, 5))
        sig = src.check({"token": TOKEN, "mandate_registry": REG, "principal": PRINCIPAL},
                        payer=AGENT)
        verdict = apply_rwa_readiness({"verdict": "GO", "reasons": [], "signals": {}}, sig)
        self.assertEqual(verdict["verdict"], "HOLD")


class TestCombinedIntegration(unittest.TestCase):
    def test_slots_into_combined_source(self):
        from rwa_readiness import CombinedRwaReadinessSource

        class NoneEvm:
            def check(self, acquires, payer=None, counterparty=None):
                return None                          # EVM restriction source: nothing
        rams_src = RamsReadinessSource(transport=lambda to, data: _two_words(False, 1))
        combined = CombinedRwaReadinessSource([NoneEvm(), rams_src])
        sig = combined.check({"token": TOKEN, "mandate_registry": REG,
                              "principal": PRINCIPAL}, AGENT)
        self.assertEqual(sig["grade"], "blocked")

    def test_dormant_in_combined_when_no_registry(self):
        from rwa_readiness import CombinedRwaReadinessSource
        rams_src = RamsReadinessSource(transport=lambda to, data: _two_words(False, 1))
        combined = CombinedRwaReadinessSource([rams_src])
        self.assertIsNone(combined.check({"token": TOKEN}, AGENT))


if __name__ == "__main__":
    unittest.main()
