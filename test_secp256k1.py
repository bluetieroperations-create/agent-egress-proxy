"""
Tests for secp256k1.py -- pure-Python ECDSA public-key recovery.

Anchored by PUBLISHED Ethereum vectors: the addresses derived from private keys
1/2/3 are well-known constants, so a bug in the curve math or keccak address
derivation shows up here. Recovery is then proven by round-trip + tamper.
"""
import hashlib
import unittest

import secp256k1 as S
from keccak import keccak256


def _addr(point):
    return "0x" + keccak256(point[0].to_bytes(32, "big")
                            + point[1].to_bytes(32, "big"))[-20:].hex()


# Published address(privkey) vectors.
KNOWN = {
    1: "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf",
    2: "0x2b5ad5c4795c026514f8317c7a215e218dccd6cf",
    3: "0x6813eb9362372eef6200f3b1dbc3f819671cba69",
}


class TestCurveAndAddress(unittest.TestCase):
    """Mutation note: any error in point_add/scalar_mult/inv changes these
    published addresses -> the asserts FAIL."""

    def test_privkey_one_is_generator(self):
        self.assertEqual(S.privkey_to_pub(1), S.G)

    def test_published_addresses(self):
        for k, a in KNOWN.items():
            self.assertEqual(_addr(S.privkey_to_pub(k)), a)

    def test_points_on_curve(self):
        for k in (1, 2, 3, 0xDEADBEEF):
            self.assertTrue(S.is_on_curve(S.privkey_to_pub(k)))


class TestRecovery(unittest.TestCase):
    """
    Mutation notes:
      - wrong recovery-id parity handling -> test_round_trip FAILS.
      - recovery ignoring z -> test_wrong_digest FAILS.
      - not range-checking r/s/rec_id -> test_invalid_inputs FAILS.
    """
    def _z(self, msg):
        return int.from_bytes(hashlib.sha256(msg).digest(), "big")

    def test_round_trip(self):
        for d in (1, 2, 3, 0xA11CE, 0x1234567890ABCDEF, S.N - 1):
            z = self._z(b"m-%x" % d)
            r, s, rec = S.ecdsa_sign(z, d)
            self.assertEqual(S.ecdsa_recover(z, r, s, rec), S.privkey_to_pub(d))

    def test_wrong_digest_recovers_different_key(self):
        z = self._z(b"a")
        r, s, rec = S.ecdsa_sign(z, 42)
        self.assertEqual(S.ecdsa_recover(z, r, s, rec), S.privkey_to_pub(42))
        self.assertNotEqual(S.ecdsa_recover(self._z(b"b"), r, s, rec),
                            S.privkey_to_pub(42))

    def test_low_s_enforced(self):
        # Ethereum requires s <= N/2; our signer must produce it.
        _, s, _ = S.ecdsa_sign(self._z(b"x"), 7)
        self.assertLessEqual(s * 2, S.N)

    def test_invalid_inputs_return_none(self):
        z = self._z(b"a")
        r, s, rec = S.ecdsa_sign(z, 5)
        self.assertIsNone(S.ecdsa_recover(z, 0, s, rec))       # r out of range
        self.assertIsNone(S.ecdsa_recover(z, r, 0, rec))       # s out of range
        self.assertIsNone(S.ecdsa_recover(z, r, s, 9))         # bad rec_id
        self.assertIsNone(S.ecdsa_recover(z, S.N, s, rec))     # r == N


if __name__ == "__main__":
    unittest.main()
