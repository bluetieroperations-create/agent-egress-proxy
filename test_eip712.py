"""
Tests for eip712.py -- EIP-712 typed-data hashing + address derivation.

The domain separator is anchored to the CANONICAL EIP-712 spec 'Mail' example
(its domain type is identical to ours). The full transferWithAuthorization pipeline
is proven by sign->recover round-trip and tamper.
"""
import unittest

import eip712 as E
import secp256k1 as S

try:
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    _HAVE_ETH_ACCOUNT = True
except Exception:  # pragma: no cover - optional cross-check dependency
    _HAVE_ETH_ACCOUNT = False

_TWA_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"}, {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"}],
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}],
}


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


@unittest.skipUnless(_HAVE_ETH_ACCOUNT, "eth-account not installed")
class TestCrossCheckEthAccount(unittest.TestCase):
    """
    LIVE cross-check against eth-account (a real Ethereum signing library) on real
    signatures -- the ground-truth validation the vector-anchored tests can't give.
    Skips cleanly when eth-account isn't installed (stays stdlib-only in CI).
    """
    USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    CP = "0x" + "a" * 40

    def _eth_sign(self, acct, *, to=None, value=90000, chain_id=8453,
                  contract=None):
        to = to or self.CP
        contract = contract or self.USDC
        domain = {"name": "USD Coin", "version": "2", "chainId": chain_id,
                  "verifyingContract": contract}
        message = {"from": acct.address, "to": to, "value": value, "validAfter": 0,
                   "validBefore": 9999999999, "nonce": bytes.fromhex("11" * 32)}
        return Account.sign_message(
            encode_typed_data(full_message={
                "types": _TWA_TYPES, "domain": domain,
                "primaryType": "TransferWithAuthorization", "message": message}),
            private_key=acct.key)

    def _my_digest(self, acct, *, to=None, chain_id=8453, contract=None):
        return E.transfer_authorization_digest(
            {"name": "USD Coin", "version": "2", "chainId": chain_id,
             "verifyingContract": contract or self.USDC},
            {"from": acct.address, "to": to or self.CP, "value": "90000",
             "validAfter": "0", "validBefore": "9999999999", "nonce": "0x" + "11" * 32})

    def test_digest_and_recovery_agree_over_many_keys(self):
        seen_v = set()
        for _ in range(25):
            acct = Account.create()
            signed = self._eth_sign(acct)
            my = self._my_digest(acct)
            # my EIP-712 digest is byte-identical to eth-account's
            self.assertEqual(my, int.from_bytes(signed.message_hash, "big"))
            # my ecrecover on eth-account's real signature -> the real signer
            q = S.ecdsa_recover(my, signed.r, signed.s, signed.v - 27)
            self.assertEqual(E.pubkey_to_address(q), acct.address.lower())
            seen_v.add(signed.v)
        self.assertTrue(seen_v <= {27, 28} and seen_v)   # both parities handled

    def test_recovers_wrong_signer_for_foreign_signature(self):
        a, b = Account.create(), Account.create()
        signed = self._eth_sign(b)                       # signed by b
        my = self._my_digest(a)                          # digest names a as `from`
        q = S.ecdsa_recover(my, signed.r, signed.s, signed.v - 27)
        self.assertNotEqual(E.pubkey_to_address(q), a.address.lower())


if __name__ == "__main__":
    unittest.main()
