"""
Unit tests for addresses (EVM address validation / normalization).

Run: python -m unittest test_addresses.py -v

Mutation notes:
  - Drop the length check in the regex -> short/long address tests FAIL.
  - Make addresses_equal case-sensitive -> test_equal_case_insensitive FAILS
    (the bug this module exists to prevent in the recipient mismatch check).
"""
import unittest

import addresses as A

CHECKSUMMED = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # Base USDC, EIP-55
LOWER = CHECKSUMMED.lower()


class TestIsEvmAddress(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(A.is_evm_address(CHECKSUMMED))
        self.assertTrue(A.is_evm_address(LOWER))

    def test_too_short(self):
        self.assertFalse(A.is_evm_address("0x1234"))

    def test_too_long(self):
        self.assertFalse(A.is_evm_address(LOWER + "ab"))

    def test_missing_prefix(self):
        self.assertFalse(A.is_evm_address(LOWER[2:]))

    def test_non_hex(self):
        self.assertFalse(A.is_evm_address("0x" + "z" * 40))

    def test_non_string(self):
        self.assertFalse(A.is_evm_address(None))
        self.assertFalse(A.is_evm_address(12345))


class TestNormalize(unittest.TestCase):
    def test_lowercases_valid(self):
        self.assertEqual(A.normalize_address(CHECKSUMMED), LOWER)

    def test_invalid_is_none(self):
        self.assertIsNone(A.normalize_address("0xNOPE"))
        self.assertIsNone(A.normalize_address("not-an-address"))


class TestAddressesEqual(unittest.TestCase):
    def test_equal_case_insensitive(self):
        # The bug this guards: checksummed vs lowercase are the SAME address.
        self.assertTrue(A.addresses_equal(CHECKSUMMED, LOWER))

    def test_distinct_addresses(self):
        self.assertFalse(A.addresses_equal(LOWER, "0x" + "0" * 40))

    def test_none_never_equal(self):
        self.assertFalse(A.addresses_equal(None, None))
        self.assertFalse(A.addresses_equal(LOWER, None))


if __name__ == "__main__":
    unittest.main()
