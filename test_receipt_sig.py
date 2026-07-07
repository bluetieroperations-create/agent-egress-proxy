"""
Tests for receipt_sig.py -- Ed25519 public-key receipt signatures.

Each test names the mutation it kills. The Ed25519 primitive itself is pinned to
the RFC 8032 Section 7.1 TEST 1 known-answer vector, so we prove our seed->key
and sign wiring is standard Ed25519, not just internally self-consistent.
"""
import unittest

import receipt_sig as rs
from nacl.signing import SigningKey

# RFC 8032 Section 7.1, TEST 1 (message is empty).
RFC_SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
RFC_PUB = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
RFC_SIG = ("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
           "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")


class TestRfc8032Kat(unittest.TestCase):
    def test_public_key_matches_rfc_vector(self):
        # Kills: wrong hex->seed decoding or wrong verify-key encoding would
        # produce a public key that does not match the standard.
        self.assertEqual(rs.public_key_hex(RFC_SEED), RFC_PUB)

    def test_raw_signature_matches_rfc_vector(self):
        # Kills: a load_seed that mangles the seed -- the standard signature of
        # the empty message under this seed would then differ.
        sk = SigningKey(rs.load_seed(RFC_SEED))
        self.assertEqual(sk.sign(b"").signature.hex(), RFC_SIG)


class TestSignVerify(unittest.TestCase):
    def setUp(self):
        self.obj = {"verdict": "GO", "score": 0.8, "counterparty": "0xabc"}
        self.pub = rs.public_key_hex(RFC_SEED)

    def test_roundtrip_true(self):
        sig = rs.sign_obj(self.obj, RFC_SEED)
        self.assertTrue(rs.verify_obj(self.obj, sig, self.pub))

    def test_tampered_object_fails(self):
        # Kills: a verify that does not actually bind the signature to content.
        sig = rs.sign_obj(self.obj, RFC_SEED)
        tampered = dict(self.obj, verdict="STOP")
        self.assertFalse(rs.verify_obj(tampered, sig, self.pub))

    def test_wrong_public_key_fails(self):
        # Kills: a verify that ignores the public key.
        sig = rs.sign_obj(self.obj, RFC_SEED)
        other_pub = rs.public_key_hex("11" * 32)
        self.assertFalse(rs.verify_obj(self.obj, sig, other_pub))

    def test_canonicalization_is_key_order_independent(self):
        # Kills: signing over a non-canonical (insertion-order) encoding -- the
        # same content in a different order must verify.
        sig = rs.sign_obj(self.obj, RFC_SEED)
        reordered = {"counterparty": "0xabc", "score": 0.8, "verdict": "GO"}
        self.assertTrue(rs.verify_obj(reordered, sig, self.pub))

    def test_bad_sig_hex_returns_false_not_raise(self):
        # Kills: letting malformed input raise instead of returning False.
        self.assertFalse(rs.verify_obj(self.obj, "zzzz", self.pub))
        self.assertFalse(rs.verify_obj(self.obj, "", self.pub))

    def test_bad_pubkey_hex_returns_false_not_raise(self):
        sig = rs.sign_obj(self.obj, RFC_SEED)
        self.assertFalse(rs.verify_obj(self.obj, sig, "not-hex"))


class TestKeyId(unittest.TestCase):
    def test_stable_and_16_hex(self):
        kid = rs.key_id(RFC_PUB)
        self.assertEqual(kid, rs.key_id(RFC_PUB))
        self.assertEqual(len(kid), 16)
        int(kid, 16)  # is hex

    def test_distinct_keys_distinct_ids(self):
        # Kills: a key_id that collapses different keys to one id.
        self.assertNotEqual(rs.key_id(RFC_PUB), rs.key_id(rs.public_key_hex("22" * 32)))


class TestIsDevKey(unittest.TestCase):
    def test_dev_seed_is_flagged(self):
        # Kills: failing to flag the built-in dev seed (forgeable receipts).
        self.assertTrue(rs.is_dev_key("00" * 32))

    def test_real_seed_not_flagged(self):
        self.assertFalse(rs.is_dev_key(RFC_SEED))

    def test_malformed_seed_is_not_dev(self):
        # A bad seed is not the dev key; must not crash or claim dev.
        self.assertFalse(rs.is_dev_key("nothex"))


class TestLoadSeed(unittest.TestCase):
    def test_rejects_wrong_length(self):
        # Kills: accepting a short/long seed (would silently weaken the key).
        with self.assertRaises(ValueError):
            rs.load_seed("00" * 31)
        with self.assertRaises(ValueError):
            rs.load_seed("00" * 33)

    def test_rejects_non_hex(self):
        with self.assertRaises(ValueError):
            rs.load_seed("nothex" * 8)


class TestSignedReceipt(unittest.TestCase):
    def _receipt(self, **over):
        kw = dict(receipt_id="bw_deadbeef", counterparty="0xabc",
                  amount="4.50", asset="USDC", chain="base",
                  verdict="GO", score=0.8, ts="2026-07-07T00:00:00Z",
                  seed_hex=RFC_SEED)
        kw.update(over)
        return rs.build_signed_receipt(**kw)

    def test_build_and_verify_roundtrip(self):
        sr = self._receipt()
        pub = rs.public_key_hex(RFC_SEED)
        self.assertTrue(rs.verify_signed_receipt(sr, pub))

    def test_signature_covers_verdict(self):
        # Kills: signing a subset that omits the verdict -- flipping GO->STOP
        # after signing must break verification.
        sr = self._receipt()
        sr["verdict"] = "STOP"
        self.assertFalse(rs.verify_signed_receipt(sr, rs.public_key_hex(RFC_SEED)))

    def test_signature_covers_amount(self):
        sr = self._receipt()
        sr["amount"] = "999999.00"
        self.assertFalse(rs.verify_signed_receipt(sr, rs.public_key_hex(RFC_SEED)))

    def test_key_id_present_and_matches_signer(self):
        sr = self._receipt()
        self.assertEqual(sr["key_id"], rs.key_id(rs.public_key_hex(RFC_SEED)))

    def test_missing_sig_returns_false(self):
        sr = self._receipt()
        del sr["sig"]
        self.assertFalse(rs.verify_signed_receipt(sr, rs.public_key_hex(RFC_SEED)))

    def test_non_dict_returns_false(self):
        self.assertFalse(rs.verify_signed_receipt("nope", rs.public_key_hex(RFC_SEED)))

    def test_amount_coerced_to_string(self):
        # Kills: leaving amount as a float (cross-language float ambiguity in the
        # signed message). build should stringify.
        sr = self._receipt(amount=4.5)
        self.assertEqual(sr["amount"], "4.5")
        self.assertTrue(rs.verify_signed_receipt(sr, rs.public_key_hex(RFC_SEED)))


if __name__ == "__main__":
    unittest.main()
