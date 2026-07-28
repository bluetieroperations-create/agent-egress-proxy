"""
Tests for traceipt_verify.py -- pure-Python Ed25519 verify + Traceipt envelope
authentication. Each test states the mutation it kills.

Genuine signatures come from two sources: RFC 8032 §7.1 vectors (interop proof --
if our verifier disagrees with the standard it is wrong), and envelopes signed here
with cdp_auth's pure-Python signer (so tests need no `cryptography` binding, which
is broken in some containers -- see test_cdp_auth.py).
"""
import base64
import binascii
import copy
import hashlib
import json
import unittest

import cdp_auth as C
import traceipt_verify as V

# RFC 8032 §7.1 -- (secret seed, public key, message, signature), all hex.
RFC8032 = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
     "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da0"
     "85ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
     "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def make_signed_envelope(payload, seed=b"\x11" * 32):
    """Sign `payload` exactly as traceipt/signing.py does, using cdp_auth's
    pure-Python Ed25519. Returns (envelope, jwks) where jwks publishes the key."""
    pub = C.ed25519_publickey(seed)
    kid = hashlib.sha256(pub).hexdigest()[:16]      # Traceipt Signer.kid
    protected = {"alg": "EdDSA", "kid": kid, "typ": "x402-receipt+json"}
    signing_input = json.dumps(
        {"payload": payload, "protected": protected},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sig = C.ed25519_sign(seed, signing_input)
    env = {"protected": protected, "payload": payload, "signature": _b64url(sig)}
    jwk = {"kty": "OKP", "crv": "Ed25519", "kid": kid,
           "x": _b64url(pub), "use": "sig", "alg": "EdDSA"}
    return env, {"keys": [jwk]}


class TestEd25519Verify(unittest.TestCase):
    """
    Mutation notes:
      - accept a tampered sig -> test_rfc_vectors FAILS (flipped byte must reject).
      - drop the S<L malleability check -> test_high_s_rejected FAILS.
      - skip on-curve check / bad decode -> test_garbage_rejected FAILS.
    """
    def test_rfc_vectors_accept_and_reject(self):
        for seed, pub, msg, sig in RFC8032:
            P, M, S = (binascii.unhexlify(x) for x in (pub, msg, sig))
            self.assertTrue(V.ed25519_verify(P, M, S), "genuine RFC sig must verify")
            bad = bytearray(S); bad[10] ^= 0x40
            self.assertFalse(V.ed25519_verify(P, M, bytes(bad)), "flipped sig rejects")
            self.assertFalse(V.ed25519_verify(P, M + b"\x00", S), "wrong msg rejects")
            badk = bytearray(P); badk[0] ^= 1
            self.assertFalse(V.ed25519_verify(bytes(badk), M, S), "wrong key rejects")

    def test_high_s_rejected(self):
        # S >= L is non-canonical (malleable); must be rejected even if the point
        # math would otherwise pass.
        seed, pub, msg, sig = RFC8032[1]
        P, M, S = (binascii.unhexlify(x) for x in (pub, msg, sig))
        s_int = int.from_bytes(S[32:], "little")
        mall = S[:32] + (s_int + V._L).to_bytes(32, "little")
        self.assertEqual(len(mall), 64)
        self.assertFalse(V.ed25519_verify(P, M, mall))

    def test_garbage_rejected_without_raising(self):
        self.assertFalse(V.ed25519_verify(b"", b"m", b"\x00" * 64))
        self.assertFalse(V.ed25519_verify(b"\xff" * 32, b"m", b"\x00" * 63))  # short sig
        self.assertFalse(V.ed25519_verify(b"\xff" * 32, b"m", b"\xff" * 64))  # off-curve key


class TestVerifyEnvelope(unittest.TestCase):
    """
    Mutation notes:
      - use ensure_ascii=True in canonicalization -> test_non_ascii FAILS (the bytes
        would differ from Traceipt's signer for any non-ASCII payload).
      - don't re-verify the signature over the payload -> test_tampered FAILS.
      - ignore the kid/alg in `protected` -> test_wrong_kid / test_alg_sub FAIL.
    """
    PAYLOAD = {"receipt_id": "rcpt_" + "a" * 20, "kind": "payment",
               "settlement": {"verified": True, "payee": "0x" + "a" * 40,
                              "payer": "0x" + "b" * 40, "tx_hash": "0x" + "1" * 64,
                              "amount_base_units": "90000"}}

    def test_genuine_returns_payload(self):
        env, jwks = make_signed_envelope(self.PAYLOAD)
        self.assertEqual(V.verify_envelope(env, jwks), self.PAYLOAD)

    def test_non_ascii_payload_verifies(self):
        # café/€ exercise ensure_ascii=False -- the crux of matching Traceipt's bytes.
        payload = dict(self.PAYLOAD, memo="café — €5 façade")
        env, jwks = make_signed_envelope(payload)
        self.assertEqual(V.verify_envelope(env, jwks), payload)

    def test_tampered_payload_rejected(self):
        env, jwks = make_signed_envelope(self.PAYLOAD)
        env2 = copy.deepcopy(env)
        env2["payload"]["settlement"]["amount_base_units"] = "999999999"
        self.assertIsNone(V.verify_envelope(env2, jwks))

    def test_wrong_kid_rejected(self):
        env, jwks = make_signed_envelope(self.PAYLOAD)
        env["protected"]["kid"] = "deadbeefdeadbeef"     # not in jwks (and unsigned)
        self.assertIsNone(V.verify_envelope(env, jwks))

    def test_key_not_in_jwks_rejected(self):
        env, _ = make_signed_envelope(self.PAYLOAD)
        self.assertIsNone(V.verify_envelope(env, {"keys": []}))

    def test_alg_substitution_rejected(self):
        env, jwks = make_signed_envelope(self.PAYLOAD)
        env["protected"]["alg"] = "none"
        self.assertIsNone(V.verify_envelope(env, jwks))

    def test_malformed_never_raises(self):
        _, jwks = make_signed_envelope(self.PAYLOAD)
        for bad in (None, {}, {"payload": {}}, {"protected": 1, "payload": {},
                    "signature": "x"}, [], "str", 7):
            self.assertIsNone(V.verify_envelope(bad, jwks))
        # a foreign key signed the same payload -> different kid, not in jwks
        env2, _ = make_signed_envelope(self.PAYLOAD, seed=b"\x22" * 32)
        self.assertIsNone(V.verify_envelope(env2, jwks))

    def test_poison_key_cannot_shadow_genuine(self):
        # A JWKS carrying a WRONG key first under the same kid must not shadow the
        # genuine key: the verifier tries every kid-match and accepts on any hit.
        env, jwks = make_signed_envelope(self.PAYLOAD)
        kid = env["protected"]["kid"]
        wrong = C.ed25519_publickey(b"\x44" * 32)
        poison = {"kty": "OKP", "crv": "Ed25519", "kid": kid,
                  "x": _b64url(wrong), "use": "sig", "alg": "EdDSA"}
        self.assertEqual(
            V.verify_envelope(env, {"keys": [poison] + jwks["keys"]}), self.PAYLOAD)

    def test_make_verifier_binds_jwks(self):
        env, jwks = make_signed_envelope(self.PAYLOAD)
        f = V.make_verifier(jwks)
        self.assertEqual(f(env), self.PAYLOAD)
        env["payload"]["kind"] = "credit"                 # tamper after signing
        self.assertIsNone(f(env))


class TestFetchJwks(unittest.TestCase):
    """Mutation note: return a non-JWKS body as valid -> test_rejects_non_jwks FAILS."""
    def test_fetches_and_shapes(self):
        doc = {"keys": [{"kid": "k1"}]}
        got = V.fetch_jwks("https://api.traceipt.xyz",
                           _transport=lambda url, timeout: doc)
        self.assertEqual(got, doc)

    def test_rejects_non_jwks_and_errors(self):
        self.assertIsNone(V.fetch_jwks("https://x", _transport=lambda u, t: {"no": 1}))
        def boom(u, t): raise ConnectionError("down")
        self.assertIsNone(V.fetch_jwks("https://x", _transport=boom))


if __name__ == "__main__":
    unittest.main()
