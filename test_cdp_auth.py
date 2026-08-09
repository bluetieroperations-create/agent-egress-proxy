#!/usr/bin/env python3
"""
Tests for cdp_auth.py -- the pure-Python Ed25519 signer and the CDP Bearer JWT.

Run: python -m unittest test_cdp_auth.py -v

The Ed25519 signer is anchored on the RFC 8032 Section 7.1 test vectors
(library-independent: hard-coded public keys + signatures), and additionally
cross-checked against the `cryptography` library when it is importable (it is NOT
in the stdlib-only container, so those checks are skipped there). Each test notes
the mutation it kills.
"""
import base64
import json
import unittest

import cdp_auth as c

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey)
    _HAVE_CRYPTO = True
except BaseException:  # pragma: no cover - container path
    # NB: a broken `cryptography` rust binding raises pyo3_runtime.PanicException,
    # which subclasses BaseException (not Exception) -- catch it too so the
    # optional cross-check degrades to a skip instead of crashing the suite.
    _HAVE_CRYPTO = False


# RFC 8032 Section 7.1 -- Ed25519 test vectors (verbatim).
RFC8032 = [
    # (secret seed hex, public key hex, message hex, signature hex)
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
     "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da0"
     "85ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
]


class TestEd25519RFC8032(unittest.TestCase):
    def test_public_keys_match_rfc(self):
        # Kills any break in scalar-mult / point-encoding: a wrong curve constant
        # or encoding produces a different public key.
        for sk, pk, _msg, _sig in RFC8032:
            self.assertEqual(c.ed25519_publickey(bytes.fromhex(sk)).hex(), pk)

    def test_signatures_match_rfc(self):
        # Kills any break in the deterministic nonce (r), the challenge (k), or
        # the s computation -- Ed25519 is deterministic, so the signature is a
        # fixed known value for a given (seed, message).
        for sk, _pk, msg, sig in RFC8032:
            got = c.ed25519_sign(bytes.fromhex(sk), bytes.fromhex(msg)).hex()
            self.assertEqual(got, sig)

    def test_signature_is_deterministic(self):
        # Kills accidental randomness in signing (a real Ed25519 must be
        # deterministic; a nonce from os.urandom would break settlement replays).
        seed = bytes.fromhex(RFC8032[1][0])
        a = c.ed25519_sign(seed, b"blackwall")
        b = c.ed25519_sign(seed, b"blackwall")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_bad_seed_length_rejected(self):
        # Kills silent acceptance of a wrong-size seed (would sign with garbage).
        with self.assertRaises(ValueError):
            c.ed25519_sign(b"\x00" * 31, b"m")

    @unittest.skipUnless(_HAVE_CRYPTO, "cryptography not installed")
    def test_cross_check_against_cryptography(self):
        # Independent authority: our signer must agree with a battle-tested lib
        # across many random keys/messages (not just the two RFC vectors).
        import os
        for _ in range(25):
            seed = os.urandom(32)
            msg = os.urandom(50)
            k = Ed25519PrivateKey.from_private_bytes(seed)
            self.assertEqual(c.ed25519_sign(seed, msg), k.sign(msg))
            self.assertEqual(c.ed25519_publickey(seed),
                             k.public_key().public_bytes_raw())


class TestCdpSeed(unittest.TestCase):
    def test_accepts_64_byte_material(self):
        # CDP secret is base64 of seed||pubkey (64 bytes); we take the seed.
        seed = bytes(range(32))
        pub = bytes(range(32, 64))
        b64 = base64.b64encode(seed + pub).decode()
        self.assertEqual(c.cdp_seed_from_secret(b64), seed)

    def test_accepts_32_byte_material(self):
        seed = bytes(range(32))
        self.assertEqual(c.cdp_seed_from_secret(base64.b64encode(seed).decode()),
                         seed)

    def test_rejects_wrong_length(self):
        # Kills accepting a truncated/oversized secret (would sign with garbage).
        with self.assertRaises(ValueError):
            c.cdp_seed_from_secret(base64.b64encode(b"\x00" * 40).decode())

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            c.cdp_seed_from_secret("")


class TestCdpJwt(unittest.TestCase):
    KEY_ID = "11111111-2222-3333-4444-555555555555"
    # A deterministic 64-byte secret for reproducible JWTs.
    SECRET = base64.b64encode(bytes(range(64))).decode()

    def _decode(self, jwt):
        h, cl, sig = jwt.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)
        return (json.loads(base64.urlsafe_b64decode(pad(h))),
                json.loads(base64.urlsafe_b64decode(pad(cl))),
                base64.urlsafe_b64decode(pad(sig)))

    def test_three_segments(self):
        jwt = c.build_cdp_jwt(self.KEY_ID, self.SECRET, "POST",
                              "https://api.cdp.coinbase.com/platform/v2/x402/settle",
                              now=1_000_000, nonce="abcdef0123456789")
        self.assertEqual(jwt.count("."), 2)

    def test_header_fields(self):
        h, _cl, _sig = self._decode(c.build_cdp_jwt(
            self.KEY_ID, self.SECRET, "POST",
            "https://api.cdp.coinbase.com/platform/v2/x402/settle",
            now=1_000_000, nonce="abcdef0123456789"))
        self.assertEqual(h["alg"], "EdDSA")
        self.assertEqual(h["typ"], "JWT")
        self.assertEqual(h["kid"], self.KEY_ID)
        self.assertEqual(h["nonce"], "abcdef0123456789")

    def test_claims_and_expiry_window(self):
        # Kills a wrong TTL (CDP requires exp = nbf + 120) or wrong aud/iss.
        _h, cl, _sig = self._decode(c.build_cdp_jwt(
            self.KEY_ID, self.SECRET, "POST",
            "https://api.cdp.coinbase.com/platform/v2/x402/settle",
            now=1_000_000, nonce="abcdef0123456789"))
        self.assertEqual(cl["sub"], self.KEY_ID)
        self.assertEqual(cl["iss"], "cdp")
        self.assertEqual(cl["aud"], ["cdp_service"])
        self.assertEqual(cl["nbf"], 1_000_000)
        self.assertEqual(cl["exp"], 1_000_120)

    def test_uri_binds_method_host_and_path(self):
        # Kills a token that isn't bound to the exact request: CDP rejects a JWT
        # whose `uri` claim != "METHOD host/path" of the call. /verify and
        # /settle MUST get different tokens.
        _h, cl_v, _ = self._decode(c.build_cdp_jwt(
            self.KEY_ID, self.SECRET, "POST",
            "https://api.cdp.coinbase.com/platform/v2/x402/verify",
            now=1, nonce="0"))
        self.assertEqual(cl_v["uri"],
                         "POST api.cdp.coinbase.com/platform/v2/x402/verify")
        _h, cl_s, _ = self._decode(c.build_cdp_jwt(
            self.KEY_ID, self.SECRET, "POST",
            "https://api.cdp.coinbase.com/platform/v2/x402/settle",
            now=1, nonce="0"))
        self.assertEqual(cl_s["uri"],
                         "POST api.cdp.coinbase.com/platform/v2/x402/settle")
        self.assertNotEqual(cl_v["uri"], cl_s["uri"])

    def test_signature_matches_recomputed_sign(self):
        # The JWT signature must be Ed25519 over exactly "<header>.<claims>".
        jwt = c.build_cdp_jwt(self.KEY_ID, self.SECRET, "POST",
                              "https://api.cdp.coinbase.com/platform/v2/x402/settle",
                              now=1_000_000, nonce="abcdef0123456789")
        signing_input, _, sig_b64 = jwt.rpartition(".")
        seed = c.cdp_seed_from_secret(self.SECRET)
        expected = c.ed25519_sign(seed, signing_input.encode("ascii"))
        pad = sig_b64 + "=" * (-len(sig_b64) % 4)
        self.assertEqual(base64.urlsafe_b64decode(pad), expected)

    @unittest.skipUnless(_HAVE_CRYPTO, "cryptography not installed")
    def test_jwt_signature_verifies_under_cdp_public_key(self):
        # End-to-end: a CDP verifier holding the public key must accept our JWT.
        jwt = c.build_cdp_jwt(self.KEY_ID, self.SECRET, "POST",
                              "https://api.cdp.coinbase.com/platform/v2/x402/settle",
                              now=1_000_000, nonce="abcdef0123456789")
        signing_input, _, sig_b64 = jwt.rpartition(".")
        seed = c.cdp_seed_from_secret(self.SECRET)
        pub_raw = c.ed25519_publickey(seed)
        pad = sig_b64 + "=" * (-len(sig_b64) % 4)
        Ed25519PublicKey.from_public_bytes(pub_raw).verify(
            base64.urlsafe_b64decode(pad), signing_input.encode("ascii"))


if __name__ == "__main__":
    unittest.main()
