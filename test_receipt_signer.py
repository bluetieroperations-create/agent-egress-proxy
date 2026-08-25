"""Tests for receipt_signer.py. Each states the MUTATION it kills.

The claim under test is "independently verifiable", so the load-bearing test is
CrossVerification: a signature made by our pure-Python RFC 8032 signer must
validate under a completely separate implementation. Everything else guards the
key handling, where the dangerous failure is a receipt that LOOKS verifiable.
"""

import json
import os
import subprocess
import tempfile
import unittest

import receipt_signer as rs

SEED = bytes(range(32))
OTHER_SEED = bytes(range(1, 33))

try:
    # BaseException, not Exception: a broken/mismatched native build raises
    # pyo3_runtime.PanicException, which does NOT derive from Exception and would
    # otherwise take the whole suite down instead of skipping this class.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    HAVE_CRYPTO = True
except BaseException:                                 # pragma: no cover
    HAVE_CRYPTO = False
    InvalidSignature = Exception


class Base64Url(unittest.TestCase):
    def test_unpadded_and_roundtrips(self):
        # MUTATION: emitting padded base64 -> JOSE consumers reject the signature.
        for n in range(1, 40):
            data = bytes(range(n))
            text = rs.b64url(data)
            self.assertNotIn("=", text)
            self.assertEqual(rs.b64url_decode(text), data)

    def test_decode_rejects_non_string(self):
        with self.assertRaises(ValueError):
            rs.b64url_decode(b"bytes-not-str")


class CanonicalJson(unittest.TestCase):
    def test_key_order_is_irrelevant(self):
        # MUTATION: dropping sort_keys -> two servers sign different bytes for the
        # same verdict and receipts stop verifying across instances.
        self.assertEqual(rs.canonical_json({"b": 1, "a": 2}),
                         rs.canonical_json({"a": 2, "b": 1}))

    def test_no_incidental_whitespace(self):
        # MUTATION: default separators -> ", " spacing changes the signed bytes.
        self.assertEqual(rs.canonical_json({"a": 1, "b": [1, 2]}), b'{"a":1,"b":[1,2]}')

    def test_floats_are_refused(self):
        # MUTATION: allowing floats -> a value can serialize differently between
        # runs/platforms and the signature becomes unverifiable. Amounts are
        # decimal STRINGS throughout this codebase for the same reason.
        for bad in ({"amount": 0.1}, {"a": [1, 2.0]}, {"a": {"b": 1e10}}):
            with self.assertRaises(ValueError):
                rs.canonical_json(bad)

    def test_ints_and_bools_still_allowed(self):
        # MUTATION: checking isinstance(x, float) AFTER treating bool as numeric,
        # or rejecting ints -> ordinary verdict payloads stop signing.
        self.assertEqual(rs.canonical_json({"n": 3, "t": True, "f": False, "z": None}),
                         b'{"f":false,"n":3,"t":true,"z":null}')

    def test_non_string_keys_refused(self):
        with self.assertRaises(ValueError):
            rs.canonical_json({1: "a"})


class Kid(unittest.TestCase):
    def test_stable_and_16_hex(self):
        # MUTATION: changing the derivation -> every previously issued receipt
        # points at a kid absent from the JWKS and fails to verify.
        kid = rs.derive_kid(rs.ReceiptSigner(seed=SEED).public_key)
        self.assertEqual(len(kid), 16)
        self.assertEqual(kid, rs.derive_kid(rs.ReceiptSigner(seed=SEED).public_key))
        int(kid, 16)                                   # hex

    def test_different_keys_differ(self):
        self.assertNotEqual(rs.derive_kid(rs.ReceiptSigner(seed=SEED).public_key),
                            rs.derive_kid(rs.ReceiptSigner(seed=OTHER_SEED).public_key))

    def test_rejects_wrong_size_key(self):
        for bad in (b"", b"\x01" * 31, "not-bytes"):
            with self.assertRaises(ValueError):
                rs.derive_kid(bad)


class SeedLoading(unittest.TestCase):
    def env(self, **kw):
        return dict(kw)

    def test_unset_is_None_not_an_error(self):
        # Unset means "signing not configured" -> forecast omits the field.
        self.assertIsNone(rs.load_seed(self.env()))
        self.assertIsNone(rs.load_seed(self.env(BLACKWALL_SIGNING_SEED="   ")))

    def test_malformed_seed_RAISES_rather_than_degrading(self):
        # MUTATION: returning None on a bad seed -> the operator set the variable,
        # so they intended signing; silently serving unsigned receipts ships a
        # service that looks configured and is not.
        for bad in ("!!!not-base64!!!", rs.b64url(b"\x01" * 31), rs.b64url(b"\x01" * 33)):
            with self.assertRaises(ValueError):
                rs.load_seed(self.env(BLACKWALL_SIGNING_SEED=bad))

    def test_all_zero_seed_refused(self):
        # MUTATION: accepting it -> a placeholder value becomes a real signing key.
        with self.assertRaises(ValueError):
            rs.load_seed(self.env(BLACKWALL_SIGNING_SEED=rs.b64url(bytes(32))))

    def test_hmac_secret_may_not_be_reused_as_the_seed(self):
        # MUTATION: dropping this check -> the HMAC secret, which has different
        # exposure and may be shared for report-token verification, becomes the
        # private signing key.
        shared = rs.b64url(SEED)
        with self.assertRaises(ValueError) as ctx:
            rs.load_seed(self.env(BLACKWALL_SIGNING_SEED=shared,
                                  BLACKWALL_RECEIPT_KEY=shared))
        self.assertIn("must not be the same value", str(ctx.exception))

    def test_valid_seed_loads(self):
        self.assertEqual(rs.load_seed(self.env(BLACKWALL_SIGNING_SEED=rs.b64url(SEED))),
                         SEED)


class SignerContract(unittest.TestCase):
    def test_unconfigured_signer_returns_None_not_an_unsigned_envelope(self):
        # MUTATION: returning an envelope with an empty signature -> an unsigned
        # receipt that LOOKS signed, which is the worst possible outcome.
        signer = rs.ReceiptSigner(environ={})
        self.assertFalse(signer.available)
        self.assertIsNone(signer.sign({"verdict": "GO"}))
        self.assertEqual(signer.jwks(), {"keys": []})

    def test_no_dev_key_fallback(self):
        # MUTATION: adding a built-in default seed -> every deployment signs with
        # a key that is in the repo, so anyone can forge receipts.
        self.assertIsNone(rs.ReceiptSigner(environ={}).public_key)

    def test_envelope_shape_matches_traceipt(self):
        # MUTATION: renaming a field or b64url-ing `protected` -> clients/
        # traceipt-verify can no longer read it, and the "one verifier" property
        # this design exists for is lost.
        env = rs.ReceiptSigner(seed=SEED).sign({"verdict": "GO"})
        self.assertEqual(sorted(env), ["payload", "protected", "signature"])
        self.assertIsInstance(env["protected"], dict)
        self.assertEqual(env["protected"]["alg"], "EdDSA")
        self.assertEqual(env["protected"]["typ"], "x402-receipt+json")
        self.assertEqual(len(rs.b64url_decode(env["signature"])), 64)

    def test_signature_covers_protected_not_just_payload(self):
        # MUTATION: signing only the payload -> an attacker swaps `kid` to point
        # at a key they control and the envelope still verifies.
        signer = rs.ReceiptSigner(seed=SEED)
        payload = {"verdict": "GO"}
        honest = signer.signing_input if False else rs.signing_input
        a = honest(payload, rs.build_protected(signer.kid))
        b = honest(payload, rs.build_protected("0000000000000000"))
        self.assertNotEqual(a, b)

    def test_deterministic(self):
        # RFC 8032 signatures are deterministic; same verdict -> same envelope.
        s = rs.ReceiptSigner(seed=SEED)
        self.assertEqual(s.sign({"verdict": "GO"}), s.sign({"verdict": "GO"}))

    def test_jwks_publishes_retired_keys(self):
        # MUTATION: publishing only the active key -> every receipt signed before
        # a rotation silently stops verifying, invalidating customers' archives.
        retired = rs.ReceiptSigner(seed=OTHER_SEED).public_key
        jwks = rs.ReceiptSigner(seed=SEED, retired_public_keys=[retired]).jwks()
        kids = [k["kid"] for k in jwks["keys"]]
        self.assertEqual(len(kids), 2)
        self.assertEqual(kids[0], rs.derive_kid(rs.ReceiptSigner(seed=SEED).public_key))
        self.assertIn(rs.derive_kid(retired), kids)

    def test_jwks_deduplicates(self):
        active = rs.ReceiptSigner(seed=SEED).public_key
        jwks = rs.ReceiptSigner(seed=SEED, retired_public_keys=[active]).jwks()
        self.assertEqual(len(jwks["keys"]), 1)

    def test_jwk_is_a_valid_OKP_key(self):
        jwk = rs.ReceiptSigner(seed=SEED).jwks()["keys"][0]
        self.assertEqual((jwk["kty"], jwk["crv"], jwk["use"]), ("OKP", "Ed25519", "sig"))
        self.assertEqual(len(rs.b64url_decode(jwk["x"])), 32)


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
class CrossVerification(unittest.TestCase):
    """THE claim: a third party can verify without our code or our secret.

    Verified against an INDEPENDENT Ed25519 implementation. Also verified out of
    band against Node's WebCrypto (the path clients/traceipt-verify uses), which
    is the same check a real consumer performs.
    """

    def verify(self, envelope, jwk):
        pub = Ed25519PublicKey.from_public_bytes(rs.b64url_decode(jwk["x"]))
        pub.verify(rs.b64url_decode(envelope["signature"]),
                   rs.signing_input(envelope["payload"], envelope["protected"]))

    def test_signature_verifies_under_an_independent_implementation(self):
        # MUTATION: any change to signing_input, canonical_json, or the RFC 8032
        # signer breaks this. It is the only test that proves the public claim.
        signer = rs.ReceiptSigner(seed=SEED)
        env = signer.sign({"verdict": "HOLD", "receipt_id": "bw_abc", "amount": "0.25"})
        self.verify(env, signer.jwks()["keys"][0])

    def test_tampered_payload_is_rejected(self):
        signer = rs.ReceiptSigner(seed=SEED)
        env = signer.sign({"verdict": "HOLD"})
        env["payload"]["verdict"] = "GO"
        with self.assertRaises(InvalidSignature):
            self.verify(env, signer.jwks()["keys"][0])

    def test_tampered_kid_is_rejected(self):
        signer = rs.ReceiptSigner(seed=SEED)
        env = signer.sign({"verdict": "GO"})
        env["protected"]["kid"] = "0000000000000000"
        with self.assertRaises(InvalidSignature):
            self.verify(env, signer.jwks()["keys"][0])

    def test_a_different_key_does_not_verify(self):
        env = rs.ReceiptSigner(seed=SEED).sign({"verdict": "GO"})
        with self.assertRaises(InvalidSignature):
            self.verify(env, rs.ReceiptSigner(seed=OTHER_SEED).jwks()["keys"][0])


class BuildClaims(unittest.TestCase):
    VERDICT = {"verdict": "HOLD", "hard_stop": False, "score": 0.9961234,
               "reasons": ["a", "b"], "receipt_id": "bw_abc",
               "signals": {"price_anomaly": 0.0, "nested": {"x": 1.5}}}
    REQUEST = {"counterparty": "0xabc", "amount": "0.25", "asset": "USDC",
               "chain": "base", "agent_id": None}

    def test_score_becomes_a_decimal_string(self):
        # MUTATION: passing the float through -> canonical_json raises and the
        # receipt is silently dropped (fail-soft), so NO receipt is ever issued.
        # That is exactly the bug this caught in the first wiring attempt.
        claims = rs.build_claims(self.VERDICT, self.REQUEST)
        self.assertEqual(claims["score"], "0.996123")
        self.assertIsInstance(claims["score"], str)

    def test_signals_blob_is_not_signed(self):
        # MUTATION: signing the whole verdict -> every receipt depends on the
        # internal scoring shape, so adding a signal breaks old verifications,
        # and the float-laden blob cannot be canonicalized at all.
        self.assertNotIn("signals", rs.build_claims(self.VERDICT, self.REQUEST))

    def test_claims_are_canonicalizable(self):
        # The property that matters: whatever build_claims returns must sign.
        rs.canonical_json(rs.build_claims(self.VERDICT, self.REQUEST))

    def test_carries_the_audit_relevant_fields(self):
        claims = rs.build_claims(self.VERDICT, self.REQUEST)
        for field in ("receipt_id", "verdict", "counterparty", "amount", "asset",
                      "chain", "reasons", "hard_stop"):
            self.assertIn(field, claims)

    def test_verdict_wins_over_request_on_conflict(self):
        # The verdict is authoritative for what was DECIDED; a caller-supplied
        # field must never be able to overwrite it in the signed claim set.
        claims = rs.build_claims({"verdict": "STOP"}, {"verdict": "GO"})
        self.assertEqual(claims["verdict"], "STOP")

    def test_absent_fields_are_omitted_not_nulled(self):
        self.assertEqual(rs.build_claims({"verdict": "GO"}), {"verdict": "GO"})

    def test_bool_is_not_treated_as_a_score_number(self):
        # MUTATION: dropping the bool guard -> hard_stop/True would stringify
        # oddly if it ever landed in the score branch.
        self.assertIs(rs.build_claims({"score": True})["score"], True)


def _node_available():
    try:
        return subprocess.run(["node", "--version"], capture_output=True,
                              timeout=20).returncode == 0
    except Exception:
        return False


HAVE_NODE = _node_available()

#: Verifies an envelope the way a real consumer does: WebCrypto + a JWKS, with a
#: canonical-JSON reimplementation. Deliberately does NOT reuse our Python -- if
#: our canonicalization is wrong, this catches it.
_NODE_VERIFIER = r"""
import { webcrypto } from 'node:crypto';
const { subtle } = webcrypto;
const enc = new TextEncoder();
const b64 = (s) => Buffer.from(s.replace(/-/g,'+').replace(/_/g,'/'), 'base64');
function canon(v){
  if (v === null || typeof v !== 'object') return JSON.stringify(v);
  if (Array.isArray(v)) return '[' + v.map(canon).join(',') + ']';
  return '{' + Object.keys(v).sort().map(k => JSON.stringify(k)+':'+canon(v[k])).join(',') + '}';
}
const doc = JSON.parse(process.argv[2]);
const { envelope, jwks } = doc;
const jwk = (jwks.keys||[]).find(k => k.kid === envelope.protected.kid && k.crv === 'Ed25519');
if (!jwk) { console.log('NO_KEY'); process.exit(0); }
const key = await subtle.importKey('raw', b64(jwk.x), { name:'Ed25519' }, false, ['verify']);
const input = enc.encode(canon({ payload: envelope.payload, protected: envelope.protected }));
console.log(await subtle.verify({ name:'Ed25519' }, key, b64(envelope.signature), input) ? 'OK' : 'FAIL');
"""


@unittest.skipUnless(HAVE_NODE, "node not available")
class NodeWebCryptoVerification(unittest.TestCase):
    """The claim, checked through the exact path a real consumer uses.

    clients/traceipt-verify verifies with WebCrypto against a JWKS. This runs the
    same check in a separate process and a separate language, so nothing about
    our Python (canonical JSON, RFC 8032 arithmetic, base64url) is taken on trust.
    """

    def verify(self, envelope, jwks):
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, "v.mjs")
            with open(script, "w") as fh:
                fh.write(_NODE_VERIFIER)
            out = subprocess.run(
                ["node", script, json.dumps({"envelope": envelope, "jwks": jwks})],
                capture_output=True, text=True, timeout=90)
            self.assertEqual(out.returncode, 0, out.stderr)
            return out.stdout.strip()

    def test_signature_verifies_in_webcrypto(self):
        # MUTATION: any change to canonical_json, signing_input, or the RFC 8032
        # signer -> FAIL here. This is the test that proves "independently
        # verifiable" is a fact rather than a marketing line.
        signer = rs.ReceiptSigner(seed=SEED)
        env = signer.sign({"verdict": "HOLD", "receipt_id": "bw_abc",
                           "reasons": ["50.0x the median"], "amount": "0.25"})
        self.assertEqual(self.verify(env, signer.jwks()), "OK")

    def test_tampered_payload_fails_in_webcrypto(self):
        signer = rs.ReceiptSigner(seed=SEED)
        env = signer.sign({"verdict": "HOLD"})
        env["payload"]["verdict"] = "GO"
        self.assertEqual(self.verify(env, signer.jwks()), "FAIL")

    def test_unicode_payload_survives_canonicalization(self):
        # MUTATION: dropping ensure_ascii=False -> Python escapes non-ASCII while
        # JS does not, the two canonicalizations diverge, and any receipt with a
        # non-ASCII reason string fails to verify.
        signer = rs.ReceiptSigner(seed=SEED)
        env = signer.sign({"verdict": "HOLD", "note": "café · 50× median"})
        self.assertEqual(self.verify(env, signer.jwks()), "OK")


class ForecastIntegration(unittest.TestCase):
    """The signer must change what the ENGINE returns, not just its own helpers."""

    class Src:
        def lookup(self, counterparty):
            return {"settlement_count": 200, "dispute_rate": 0.0,
                    "distinct_payers": 50,
                    "price_observations": [{"payer": "0x%040x" % i, "amount": "0.01"}
                                           for i in range(50)],
                    "price_history": ["0.01"] * 50}

    REQ = {"counterparty": "0x" + "a" * 40, "amount": "0.01",
           "asset": "USDC", "chain": "base"}

    def test_no_signer_means_no_receipt_field(self):
        # MUTATION: emitting an empty/placeholder envelope when unconfigured ->
        # a receipt that looks signed but is not.
        import blackwall as bw
        out, err = bw.forecast(dict(self.REQ), self.Src())
        self.assertIsNone(err)
        self.assertNotIn("receipt", out)
        self.assertIn("receipt_id", out)          # HMAC id unaffected

    def test_signer_attaches_a_verifiable_envelope(self):
        # MUTATION: not wiring receipt_signer through forecast -> no receipt is
        # ever issued, which is the state this whole change exists to fix.
        import blackwall as bw
        signer = rs.ReceiptSigner(seed=SEED)
        out, err = bw.forecast(dict(self.REQ), self.Src(), receipt_signer=signer)
        self.assertIsNone(err)
        self.assertIn("receipt", out)
        self.assertEqual(out["receipt"]["payload"]["receipt_id"], out["receipt_id"])
        self.assertEqual(out["receipt"]["payload"]["verdict"], out["verdict"])

    def test_signing_failure_never_breaks_the_verdict(self):
        # MUTATION: letting the exception escape -> a signing bug takes down the
        # verdict path. A payment guardrail must degrade to "no receipt", never
        # to "no verdict".
        import blackwall as bw

        class Exploding:
            available = True
            def sign(self, payload):
                raise RuntimeError("boom")

        out, err = bw.forecast(dict(self.REQ), self.Src(), receipt_signer=Exploding())
        self.assertIsNone(err)
        self.assertIn("verdict", out)
        self.assertNotIn("receipt", out)

    def test_float_score_does_not_silently_kill_the_receipt(self):
        # REGRESSION: the first wiring signed the whole verdict, whose float
        # `score` is not canonicalizable, so canonical_json raised and fail-soft
        # dropped EVERY receipt while the service looked healthy.
        import blackwall as bw
        signer = rs.ReceiptSigner(seed=SEED)
        out, _ = bw.forecast(dict(self.REQ), self.Src(), receipt_signer=signer)
        self.assertIn("receipt", out)
        self.assertIsInstance(out["receipt"]["payload"]["score"], str)


if __name__ == "__main__":
    unittest.main()
