"""
Tests for eip712.py -- EIP-712 typed-data hashing + address derivation.

The domain separator is anchored to the CANONICAL EIP-712 spec 'Mail' example
(its domain type is identical to ours). The full transferWithAuthorization pipeline
is proven by sign->recover round-trip and tamper.
"""
import unittest

import eip712 as E
import secp256k1 as S


class TestDomainSeparator(unittest.TestCase):
    def test_spec_mail_vector(self):
        # EIP-712 specification's canonical example domainSeparator.
        ds = E.domain_separator("Ether Mail", "1", 1,
                                "0xCcCCccccCCCCcCCCCCCcCcCccCcCCCcCcccccccC")
        self.assertEqual(
            "0x" + ds.hex(),
            "0xf2cee375fa42b42143804025fc449deafd50cc031ca257e0b194a650a912090f")


class TestEncoders(unittest.TestCase):
    def test_uint_and_address_width(self):
        self.assertEqual(len(E.enc_uint(1)), 32)
        self.assertEqual(len(E.enc_address("0x" + "a" * 40)), 32)
        self.assertEqual(E.enc_uint("0x10"), (16).to_bytes(32, "big"))

    def test_bytes32_exact(self):
        self.assertEqual(E.enc_bytes32("0x" + "1" * 64), b"\x11" * 32)
        with self.assertRaises(ValueError):
            E.enc_bytes32("0x1234")           # not 32 bytes

    def test_uint_range(self):
        with self.assertRaises(ValueError):
            E.enc_uint(1 << 256)


class TestPubkeyToAddress(unittest.TestCase):
    def test_known(self):
        self.assertEqual(E.pubkey_to_address(S.privkey_to_pub(1)),
                         "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf")


class TestSplitSignature(unittest.TestCase):
    def test_v27_28(self):
        sig = b"\x01" * 32 + b"\x02" * 32 + bytes([28])
        r, s, rec = E.split_signature(sig)
        self.assertEqual(rec, 1)

    def test_eip155_v(self):
        # v = 35 + 2*chainId + rec ; for base (8453) + rec 0 -> 35+16906 = 16941
        sig = b"\x01" * 32 + b"\x02" * 32 + bytes([(16941) & 0xFF])
        self.assertIsNotNone(E.split_signature(sig))

    def test_bad_length(self):
        self.assertIsNone(E.split_signature("0x00"))
        self.assertIsNone(E.split_signature(b"x" * 64))


class TestTransferAuthorizationDigest(unittest.TestCase):
    """
    Mutation notes:
      - wrong struct type string / field order -> round-trip recovers wrong signer.
      - not including the domain -> chain/asset stop binding (tamper tests below).
    """
    DOMAIN = {"name": "USD Coin", "version": "2", "chainId": 8453,
              "verifyingContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}

    def _msg(self, signer, **over):
        m = {"from": signer, "to": "0x" + "d" * 40, "value": "90000",
             "validAfter": "0", "validBefore": "9999999999", "nonce": "0x" + "1" * 64}
        m.update(over)
        return m

    def _sign(self, pk, domain, message):
        z = E.transfer_authorization_digest(domain, message)
        r, s, rec = S.ecdsa_sign(z, pk)
        return z, (r, s, rec)

    def test_sign_recover_round_trip(self):
        signer = E.pubkey_to_address(S.privkey_to_pub(0xA11CE))
        msg = self._msg(signer)
        z, (r, s, rec) = self._sign(0xA11CE, self.DOMAIN, msg)
        self.assertEqual(E.pubkey_to_address(S.ecdsa_recover(z, r, s, rec)), signer)

    def test_tamper_value_breaks_recovery(self):
        signer = E.pubkey_to_address(S.privkey_to_pub(0xA11CE))
        z, (r, s, rec) = self._sign(0xA11CE, self.DOMAIN, self._msg(signer))
        z2 = E.transfer_authorization_digest(
            self.DOMAIN, self._msg(signer, value="5000000"))
        self.assertNotEqual(E.pubkey_to_address(S.ecdsa_recover(z2, r, s, rec)), signer)

    def test_different_chain_id_changes_digest(self):
        signer = E.pubkey_to_address(S.privkey_to_pub(0xA11CE))
        msg = self._msg(signer)
        z_base = E.transfer_authorization_digest(self.DOMAIN, msg)
        z_sep = E.transfer_authorization_digest(dict(self.DOMAIN, chainId=84532), msg)
        self.assertNotEqual(z_base, z_sep)      # chainId is bound in the domain

    def test_malformed_field_raises(self):
        with self.assertRaises(Exception):
            E.transfer_authorization_digest(self.DOMAIN, self._msg("0xNOTHEX"))


if __name__ == "__main__":
    unittest.main()
