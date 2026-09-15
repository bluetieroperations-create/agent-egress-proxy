"""
Tests for seller_audit.py -- the earned "verified merchant" tier. Each test states
the mutation it kills. The anti-corruption rules (earned, bounded, revocable, never
a STOP override) are the point, so they get the most coverage.
"""
import base64
import unittest

import seller_audit as SA

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    _HAVE_CRYPTO = True
except Exception:                                        # pragma: no cover
    Ed25519PublicKey = None
    InvalidSignature = Exception
    _HAVE_CRYPTO = False

MERCHANT = "0x" + "c" * 40

#: Test seeds. base64url, 32 bytes, as `receipt_signer.load_seed` expects.
SEED = bytes(range(32))
OTHER_SEED = bytes(range(32, 64))


def _ready(grade="ready", score=90):
    return {"grade": grade, "score": score, "source": "local", "signals": {}}


def _record(**o):
    base = {"settlement_count": 40, "confirmed_settlement_count": 40,
            "distinct_payers": 6, "dispute_rate": 0.0}
    base.update(o)
    return base


class TestRunAudit(unittest.TestCase):
    """
    Mutation notes:
      - attest a sanctioned/known-bad merchant -> test_bad_actor_fails FAILS.
      - attest a needs_work/unknown endpoint -> test_endpoint_required FAILS.
      - ignore disputes / gouging -> those tests FAIL.
      - grade A without real history -> test_grade_a_needs_history FAILS.
    """
    def test_grade_a_full(self):
        a = SA.run_audit(readiness=_ready(), record=_record(), peer_ratio=1.1)
        self.assertTrue(a["passed"])
        self.assertEqual(a["grade"], "A")
        self.assertEqual(a["floor"], SA.GRADE_FLOORS["A"])

    def test_grade_b_thin_history(self):
        # good endpoint, sanctions-clear, fair price, but thin confirmed history
        a = SA.run_audit(readiness=_ready(), peer_ratio=1.0,
                         record=_record(confirmed_settlement_count=5,
                                        settlement_count=5))
        self.assertTrue(a["passed"])
        self.assertEqual(a["grade"], "B")

    def test_grade_a_needs_history(self):
        # 'ready' endpoint but no real history -> B, not A (floor is lower)
        a = SA.run_audit(readiness=_ready(), record=_record(
            confirmed_settlement_count=3, distinct_payers=1))
        self.assertEqual(a["grade"], "B")

    def test_bad_actor_fails(self):
        self.assertFalse(SA.run_audit(readiness=_ready(),
                                      record=_record(sanctioned=True))["passed"])
        self.assertFalse(SA.run_audit(readiness=_ready(),
                                      record=_record(known_bad=True))["passed"])

    def test_endpoint_required(self):
        self.assertFalse(SA.run_audit(readiness=_ready("needs_work", 20),
                                      record=_record())["passed"])
        self.assertFalse(SA.run_audit(readiness=None, record=_record())["passed"])

    def test_high_disputes_fail(self):
        self.assertFalse(SA.run_audit(readiness=_ready(),
                                      record=_record(dispute_rate=0.10))["passed"])

    def test_gouging_fails(self):
        self.assertFalse(SA.run_audit(readiness=_ready(), record=_record(),
                                      peer_ratio=3.0)["passed"])

    def test_close_endpoint_is_grade_b_not_a(self):
        a = SA.run_audit(readiness=_ready("close", 60), record=_record())
        self.assertTrue(a["passed"])
        self.assertEqual(a["grade"], "B")

    def test_floor_never_reaches_one(self):
        for g in SA.GRADE_FLOORS.values():
            self.assertLess(g, 1.0)             # earned, never a pay-to-whitelist
            self.assertGreater(g, 0.70)         # but above GO_REPUTATION_MIN


class TestAttestation(unittest.TestCase):
    """Ed25519 attestations. WAS HMAC-SHA256 with a COMMITTED dev-key fallback.

    Two defects, fixed together because either alone is worse:
      1. SYMMETRIC -- only the holder of the secret could verify, so the
         "verified merchant" badge was not independently checkable by the
         merchant it was issued to or by any buyer relying on it.
      2. `_audit_key` fell back to `_DEV_AUDIT_KEY`, a key IN THE PUBLIC REPO.
         With BLACKWALL_AUDIT_KEY unset, anybody could forge a badge granting a
         trust floor. `receipt_signer.py` documents exactly this lesson ("a
         receipt signed with a committed key is WORSE than none -- it looks
         verifiable"); this module never got it.

    Unexploitable in production only because `seller_registry` is never bound in
    `serve_forever`, i.e. two defects cancelling. Wiring the registry without
    fixing the signing would have activated the forgeable badge.

    Mutation notes are on each test.
    """

    def setUp(self):
        self.audit = SA.run_audit(readiness=_ready(), record=_record())
        self.signer = SA.attestation_signer(seed=SEED)
        self.att = SA.sign_attestation(MERCHANT, self.audit, issued_at=1000,
                                       ttl=1000, signer=self.signer)

    # -- the two defects ----------------------------------------------------
    def test_there_is_NO_committed_key_fallback(self):
        # MUTATION: restoring _DEV_AUDIT_KEY (or any default seed). A badge
        # signed with a key in the public repo is forgeable by anyone who can
        # read GitHub, and it LOOKS verifiable, which is worse than unsigned.
        self.assertFalse(hasattr(SA, "_DEV_AUDIT_KEY"))
        self.assertFalse(hasattr(SA, "_audit_key"))

    def test_an_unconfigured_signer_RAISES_rather_than_issuing_an_unsigned_badge(self):
        # MUTATION: returning None or an unsigned dict, mirroring
        # ReceiptSigner.sign(). That asymmetry is deliberate: omitting a receipt
        # from a verdict is honest, because the verdict is still valid without
        # it. An UNSIGNED ATTESTATION has no value at all -- its entire content
        # is "we vouch for this merchant" -- and worse, it would flow into
        # SellerRegistry and grant a trust floor on no evidence.
        unconfigured = SA.attestation_signer(seed=None, environ={})
        self.assertFalse(unconfigured.available)
        with self.assertRaises(SA.AttestationUnavailable):
            SA.sign_attestation(MERCHANT, self.audit, issued_at=1000,
                                signer=unconfigured)
        with self.assertRaises(SA.AttestationUnavailable):
            SA.sign_attestation(MERCHANT, self.audit, issued_at=1000, signer=None)

    # -- shape --------------------------------------------------------------
    def test_envelope_is_byte_compatible_with_the_receipt_verifier(self):
        # One verifier implementation must read verdicts, Traceipt receipts AND
        # attestations. MUTATION: inventing a different envelope shape.
        self.assertEqual(set(self.att), {"protected", "payload", "signature"})
        self.assertEqual(self.att["protected"]["alg"], "EdDSA")
        self.assertEqual(len(self.att["protected"]["kid"]), 16)

    def test_it_is_LABELLED_an_attestation_not_a_verdict(self):
        # MUTATION: leaving typ at receipt_signer.TYP. One key signs both claim
        # types, so the label is the ONLY thing separating "we vouch for this
        # merchant" from "we judged this payment".
        import receipt_signer as rs
        self.assertEqual(self.att["protected"]["typ"], rs.ATTESTATION_TYP)
        self.assertNotEqual(self.att["protected"]["typ"], rs.TYP)

    def test_the_claims_are_in_the_payload_where_a_verifier_can_read_them(self):
        p = self.att["payload"]
        self.assertEqual(p["subject"], MERCHANT)          # lowercased
        self.assertEqual(p["grade"], self.audit["grade"])
        # A DECIMAL STRING, not a float -- see seller_audit._decimalize. Pinned
        # deliberately: the value a verifier reads must be the value that was
        # signed, and canonical_json refuses floats outright.
        self.assertIsInstance(p["floor"], str)
        self.assertEqual(p["floor"], "%.6f" % self.audit["floor"])
        self.assertEqual(p["expires_at"], 2000)
        self.assertTrue(p["attestation_id"].startswith("sa_"))

    # -- THE PAYOFF of parameterizing typ -----------------------------------
    def test_a_VERDICT_signed_by_the_SAME_KEY_is_not_a_valid_attestation(self):
        # THE ATTACK this whole design exists to stop. One Ed25519 key signs
        # verdicts and attestations, so a raw signature check would accept
        # either for the other: a caller who can get ANY verdict signed (which
        # is every anonymous caller of /v1/forecast-payment) could otherwise
        # present it as a verified-merchant badge and claim a trust floor.
        # MUTATION: dropping the typ check from verify_attestation -- the
        # signature is genuine, so nothing else catches it.
        import receipt_signer as rs
        verdict_signer = rs.ReceiptSigner(seed=SEED)      # default typ
        forged = verdict_signer.sign(dict(self.att["payload"]))
        ok, reason = SA.verify_attestation(forged, now=1500, signer=self.signer)
        self.assertFalse(ok)
        self.assertIn("typ", reason)

    def test_an_old_HMAC_attestation_is_REFUSED_no_downgrade_path(self):
        # MUTATION: keeping a legacy `sig` branch for compatibility. That would
        # be an algorithm-confusion downgrade: anyone holding the old symmetric
        # key (including the committed dev one) could forge again. Safe to
        # refuse outright because the registry was never wired, so no
        # attestation exists in the wild.
        legacy = {"v": 1, "subject": MERCHANT, "grade": "A", "floor": 0.8,
                  "issued_at": 1000, "expires_at": 2000, "criteria": {},
                  "attestation_id": "sa_deadbeef", "sig": "00" * 32}
        ok, reason = SA.verify_attestation(legacy, now=1500, signer=self.signer)
        self.assertFalse(ok, reason)

    # -- the properties that already held, re-asserted on the new scheme ----
    def test_cannot_attest_failed_audit(self):
        with self.assertRaises(ValueError):
            SA.sign_attestation(MERCHANT,
                                SA.run_audit(record=_record(sanctioned=True)),
                                issued_at=1000, signer=self.signer)

    def test_valid(self):
        ok, reason = SA.verify_attestation(self.att, now=1500, signer=self.signer)
        self.assertTrue(ok, reason)

    def test_tamper_rejected(self):
        # MUTATION: not covering a field in the signature.
        for field, value in (("floor", 0.99), ("grade", "B"),
                             ("subject", "0x" + "9" * 40),
                             ("expires_at", 9999999999),
                             ("attestation_id", "sa_other")):
            forged = dict(self.att,
                          payload=dict(self.att["payload"], **{field: value}))
            ok, _ = SA.verify_attestation(forged, now=1500, signer=self.signer)
            self.assertFalse(ok, "tamper on %s accepted" % field)

    def test_tampering_with_the_PROTECTED_header_is_also_rejected(self):
        # MUTATION: signing only the payload. `signing_input` wraps BOTH, so the
        # kid and typ are covered -- this pins that.
        forged = dict(self.att, protected=dict(self.att["protected"],
                                               kid="0" * 16))
        self.assertFalse(SA.verify_attestation(forged, now=1500,
                                               signer=self.signer)[0])

    def test_wrong_key_rejected(self):
        other = SA.attestation_signer(seed=OTHER_SEED)
        self.assertFalse(SA.verify_attestation(self.att, now=1500,
                                               signer=other)[0])

    def test_expiry(self):
        ok, reason = SA.verify_attestation(self.att, now=2000, signer=self.signer)
        self.assertFalse(ok)
        self.assertEqual(reason, "expired")

    def test_revocation_by_id_and_subject(self):
        aid = self.att["payload"]["attestation_id"]
        self.assertFalse(SA.verify_attestation(
            self.att, now=1500, signer=self.signer, revoked={aid})[0])
        self.assertFalse(SA.verify_attestation(
            self.att, now=1500, signer=self.signer, revoked={MERCHANT})[0])

    def test_verify_never_raises_on_junk(self):
        for junk in (None, [], "x", {}, {"protected": 1}, {"payload": None},
                     {"protected": {}, "payload": {}, "signature": None}):
            ok, reason = SA.verify_attestation(junk, now=1500,
                                               signer=self.signer)
            self.assertFalse(ok)
            self.assertIsInstance(reason, str)


@unittest.skipUnless(_HAVE_CRYPTO, "needs `cryptography` for an independent verifier")
class TestThirdPartyVerifiable(unittest.TestCase):
    """The POINT of the change: someone who is not us can check the badge.

    Verified against an INDEPENDENT Ed25519 implementation using only the
    PUBLIC key -- which is what `/jwks.json` publishes. Under the old HMAC
    scheme this test was impossible to write, which is the whole finding.
    """

    def setUp(self):
        self.audit = SA.run_audit(readiness=_ready(), record=_record())
        self.signer = SA.attestation_signer(seed=SEED)
        self.att = SA.sign_attestation(MERCHANT, self.audit, issued_at=1000,
                                       ttl=1000, signer=self.signer)

    def _verify(self, att):
        import receipt_signer as rs
        pub = Ed25519PublicKey.from_public_bytes(self.signer.public_key)
        pub.verify(rs.b64url_decode(att["signature"]),
                   rs.signing_input(att["payload"], att["protected"]))

    def test_a_stranger_with_only_the_public_key_can_verify(self):
        self._verify(self.att)          # raises InvalidSignature on failure

    def test_and_a_forgery_fails_under_that_same_verifier(self):
        # The forged floor is a decimal STRING, matching the signed shape. A
        # float forgery cannot even be encoded -- canonical_json refuses it
        # before a signature is involved -- so the string form is both the
        # realistic attack and the only one worth asserting against.
        forged = dict(self.att,
                      payload=dict(self.att["payload"], floor="0.990000"))
        with self.assertRaises(InvalidSignature):
            self._verify(forged)


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.reg = SA.SellerRegistry(signer=SA.attestation_signer(seed=SEED))
        self.reg.issue(MERCHANT, SA.run_audit(readiness=_ready(), record=_record()),
                       issued_at=1000, ttl=1000)

    def test_credential_for_valid(self):
        cred = self.reg.credential_for(MERCHANT, now=1500)
        self.assertEqual(cred["grade"], "A")
        # The floor travels as the signed decimal string; blackwall.py applies
        # it through float(), so a string is what the verdict path wants.
        self.assertEqual(float(cred["floor"]), SA.GRADE_FLOORS["A"])

    def test_an_unsigned_registry_REFUSES_to_issue(self):
        # MUTATION: falling back to an unsigned or dev-key-signed badge. An
        # operator with no BLACKWALL_SIGNING_SEED must get an error, not a
        # forgeable badge that grants a trust floor.
        reg = SA.SellerRegistry(signer=SA.attestation_signer(seed=None,
                                                             environ={}))
        with self.assertRaises(SA.AttestationUnavailable):
            reg.issue(MERCHANT, SA.run_audit(readiness=_ready(), record=_record()),
                      issued_at=1000)

    def test_add_keys_off_the_SIGNED_subject_not_an_outer_field(self):
        # MUTATION: reading a top-level "subject" off the envelope. An envelope
        # whose outer dict claims a different subject than the one inside its
        # own signature would then be filed under the attacker's chosen address,
        # and credential_for would hand out a floor for it.
        att = SA.sign_attestation(MERCHANT,
                                  SA.run_audit(readiness=_ready(), record=_record()),
                                  issued_at=1000, ttl=1000,
                                  signer=SA.attestation_signer(seed=SEED))
        victim = "0x" + "9" * 40
        reg = SA.SellerRegistry(signer=SA.attestation_signer(seed=SEED))
        reg.add(dict(att, subject=victim))       # lying outer field
        self.assertIsNone(reg.credential_for(victim, now=1500))
        self.assertIsNotNone(reg.credential_for(MERCHANT, now=1500))

    def test_add_refuses_an_envelope_with_no_signed_subject(self):
        reg = SA.SellerRegistry(signer=SA.attestation_signer(seed=SEED))
        for bad in ({}, {"payload": {}}, {"payload": None}, None):
            with self.assertRaises(ValueError):
                reg.add(bad)

    def test_case_insensitive_subject(self):
        self.assertIsNotNone(self.reg.credential_for(MERCHANT.upper(), now=1500))

    def test_unknown_subject_is_none(self):
        self.assertIsNone(self.reg.credential_for("0x" + "9" * 40, now=1500))

    def test_expired_is_none(self):
        self.assertIsNone(self.reg.credential_for(MERCHANT, now=99999))

    def test_revoked_is_none(self):
        self.reg.revoke(MERCHANT)
        self.assertIsNone(self.reg.credential_for(MERCHANT, now=1500))


if __name__ == "__main__":
    unittest.main()
