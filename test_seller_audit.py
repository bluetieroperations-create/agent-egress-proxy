"""
Tests for seller_audit.py -- the earned "verified merchant" tier. Each test states
the mutation it kills. The anti-corruption rules (earned, bounded, revocable, never
a STOP override) are the point, so they get the most coverage.
"""
import base64
import os
import time
import shutil
import tempfile
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


class TestVerifyOnceNotPerRequest(unittest.TestCase):
    """AUDIT FINDING (HIGH), found by measuring the hot path rather than reading it.

    `credential_for` called `verify_attestation`, which RE-SIGNS the payload to
    compare signatures. That put an Ed25519 signing operation on the VERDICT
    path, for every request naming a badged counterparty, reachable by any
    anonymous caller of /v1/forecast-payment. MEASURED:

        credential_for, native backend          0.133 ms
        credential_for, pure-Python backend   221.864 ms
        the whole forecast, no registry          0.109 ms

    So on the pure-Python fallback it is ~2000x the verdict it decorates -- a
    self-inflicted outage on a 0.1-CPU box, not a slow path. It was ALSO a
    variable-time oracle (`_scalarmult` leaks the nonce's Hamming weight, and
    the nonce derives from the secret prefix), though that half was already
    covered incidentally: the attestation signer shares BLACKWALL_SIGNING_SEED
    with receipt signing, so the existing public-bind boot guard fires. The
    LATENCY was covered by nothing.

    THE FIX IS ALSO THE RIGHT DESIGN. The signature protects against a tampered
    attestation FILE -- a load-time concern. Nothing mutates the in-memory
    envelope between load and use, so verifying once on entry is equivalent and
    removes both problems. Expiry and revocation MUST stay per-request: they are
    time- and state-dependent.
    """

    def setUp(self):
        self.signer = SA.attestation_signer(seed=SEED)
        self.audit = SA.run_audit(readiness=_ready(), record=_record())
        self.att = SA.sign_attestation(MERCHANT, self.audit,
                                       issued_at=int(time.time()), ttl=3600,
                                       signer=self.signer)

    def test_credential_for_does_NOT_sign(self):
        # MUTATION: restoring the per-request verify_attestation call.
        # Asserted by counting real signing operations, not by timing (a timing
        # assertion would be flaky on a loaded box and would not say WHY).
        reg = SA.SellerRegistry(signer=self.signer)
        reg.add(self.att)
        calls = []
        real = self.signer.sign
        self.signer.sign = lambda payload: (calls.append(1), real(payload))[1]
        try:
            for _ in range(5):
                self.assertIsNotNone(reg.credential_for(MERCHANT,
                                                        int(time.time())))
        finally:
            self.signer.sign = real
        self.assertEqual(calls, [], "credential_for signed %d time(s) on the "
                                    "verdict hot path" % len(calls))

    def test_add_REFUSES_an_envelope_it_cannot_verify(self):
        # The security property moves to entry, so entry must enforce it.
        # MUTATION: storing without verifying -- the signature check would then
        # exist nowhere at all, which is strictly worse than the slow version.
        forged = dict(self.att,
                      payload=dict(self.att["payload"], floor="0.990000"))
        reg = SA.SellerRegistry(signer=self.signer)
        with self.assertRaises(ValueError):
            reg.add(forged)
        self.assertIsNone(reg.credential_for(MERCHANT, int(time.time())))

    def test_add_REFUSES_a_verdict_envelope_signed_by_the_same_key(self):
        # The typ check must move with the signature check, or the
        # claim-confusion attack comes back at a different layer.
        import receipt_signer as rs
        verdict = rs.ReceiptSigner(seed=SEED).sign(dict(self.att["payload"]))
        reg = SA.SellerRegistry(signer=self.signer)
        with self.assertRaises(ValueError):
            reg.add(verdict)

    def test_add_REFUSES_an_envelope_signed_by_a_DIFFERENT_key(self):
        other = SA.attestation_signer(seed=OTHER_SEED)
        alien = SA.sign_attestation(MERCHANT, self.audit,
                                    issued_at=int(time.time()), ttl=3600,
                                    signer=other)
        reg = SA.SellerRegistry(signer=self.signer)
        with self.assertRaises(ValueError):
            reg.add(alien)

    def test_EXPIRY_still_evaluates_per_request(self):
        # MUTATION: caching the whole verdict at entry. Expiry is a function of
        # the clock, so a badge verified once must still die on time.
        reg = SA.SellerRegistry(signer=self.signer)
        att = SA.sign_attestation(MERCHANT, self.audit, issued_at=1000,
                                  ttl=1000, signer=self.signer)
        reg.add(att)
        self.assertIsNotNone(reg.credential_for(MERCHANT, 1500))
        self.assertIsNone(reg.credential_for(MERCHANT, 2000))

    def test_REVOCATION_still_evaluates_per_request(self):
        # MUTATION: same. Revocation is state, not a property of the envelope.
        reg = SA.SellerRegistry(signer=self.signer)
        reg.add(self.att)
        now = int(time.time())
        self.assertIsNotNone(reg.credential_for(MERCHANT, now))
        reg.revoke(MERCHANT)
        self.assertIsNone(reg.credential_for(MERCHANT, now))

    def test_verify_attestation_is_still_the_full_public_check(self):
        # RESTRAINT CONTROL. Third parties and the tests above use it; moving
        # the hot-path call must not weaken the function itself.
        ok, reason = SA.verify_attestation(self.att, now=int(time.time()),
                                           signer=self.signer)
        self.assertTrue(ok, reason)
        self.assertFalse(SA.verify_attestation(self.att, now=10 ** 12,
                                               signer=self.signer)[0])


class TestRevokeTokenKeyHandling(unittest.TestCase):
    """AUDIT FINDING (MEDIUM): revoke tokens derived from a COMMITTED key.

    `sign_revoke_token` fell back to `blackwall._receipt_key()`, which returns
    `_DEV_RECEIPT_KEY` -- `b"blackwall-dev-receipt-key-not-for-production"`, in
    the public repo -- when BLACKWALL_RECEIPT_KEY is unset. MEASURED: a forged
    token computed from that constant was ACCEPTED. So any reader of GitHub
    could strip any merchant's badge.

    Bounded by the monotonic-safety design -- revocation only ever REMOVES
    trust, so this is merchant griefing, not privilege escalation -- which is
    why it is MEDIUM and not HIGH. It is still the third instance in this repo
    of the same root cause (`seller_audit._DEV_AUDIT_KEY`,
    `receipt_signer`'s refusal to have one, and now this).
    """

    def test_an_unset_key_REFUSES_rather_than_falling_back(self):
        # MUTATION: accepting an ephemeral or constant key for revocation.
        #
        # This test used to forge a token from `blackwall._DEV_RECEIPT_KEY` and
        # assert it was refused. That constant NO LONGER EXISTS -- `hmac_key`
        # owns the secret now and has no committed fallback -- so the test broke,
        # which is the fix working. Rewritten to assert the PROPERTY rather than
        # the absence of one particular guessable value: with no secret set,
        # NOTHING mints or verifies a revoke token.
        subject = "0x" + "c" * 40
        with self.assertRaises(SA.RevocationNotConfigured):
            SA.sign_revoke_token(subject, key=None, environ={})
        # Tokens an attacker would try, given the repo is public.
        for guess in (b"dev-insecure-key", b"blackwall-dev-receipt-key",
                      b"changeme", b"secret", b""):
            forged = SA.sign_revoke_token(subject, key=guess) if guess else "0" * 32
            self.assertFalse(
                SA.verify_revoke_token(subject, forged, key=None, environ={}),
                "a token forged from %r was accepted" % guess)

    def test_an_explicitly_set_key_works(self):
        env = {"BLACKWALL_RECEIPT_KEY": "a-real-operator-secret"}
        tok = SA.sign_revoke_token("0x" + "c" * 40, environ=env)
        self.assertTrue(SA.verify_revoke_token("0x" + "c" * 40, tok, environ=env))

    def test_verify_never_raises_when_unconfigured(self):
        # The HTTP path must get False, not a 500.
        self.assertFalse(SA.verify_revoke_token("0xabc", "whatever", environ={}))


class TestDurableRevocation(unittest.TestCase):
    """Revocation MUST survive a restart.

    `SellerRegistry._revoked` was an in-memory set, so a redeploy -- which is
    exactly when the process restarts -- RESTORED every revoked badge and with
    it the trust floor. That is not a fail-open: fail-open means declining to
    add caution, and this actively GRANTS trust the operator had withdrawn. The
    badge also expires, so the window is bounded by the TTL rather than
    infinite, which makes it easy to under-rate.

    Second half of the same problem: an attestation is now independently
    verifiable, and a third party checking one has no way to learn it was
    revoked unless the revocation list is PUBLISHED. An expiring signed badge
    with unpublished revocation is only as good as its TTL.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "revocations.jsonl")
        self.signer = SA.attestation_signer(seed=SEED)
        self.audit = SA.run_audit(readiness=_ready(), record=_record())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reg(self):
        return SA.SellerRegistry(signer=self.signer,
                                 revocations=SA.FileRevocationStore(self.path))

    def test_a_revocation_survives_a_restart(self):
        # MUTATION: keeping the in-memory set. The second registry is a fresh
        # process for all practical purposes -- same file, new object.
        reg = self._reg()
        reg.issue(MERCHANT, self.audit, issued_at=1000, ttl=10000)
        att = reg._by_subject[MERCHANT]
        self.assertIsNotNone(reg.credential_for(MERCHANT, now=1500))
        reg.revoke(MERCHANT)
        self.assertIsNone(reg.credential_for(MERCHANT, now=1500))

        restarted = self._reg()
        restarted.add(att)
        self.assertIsNone(restarted.credential_for(MERCHANT, now=1500),
                          "a restart restored a REVOKED badge and its floor")

    def test_revocation_works_by_attestation_id_too(self):
        reg = self._reg()
        att = reg.issue(MERCHANT, self.audit, issued_at=1000, ttl=10000)
        reg.revoke(att["payload"]["attestation_id"])
        restarted = self._reg()
        restarted.add(att)
        self.assertIsNone(restarted.credential_for(MERCHANT, now=1500))

    def test_a_subject_can_be_revoked_BEFORE_any_badge_is_loaded(self):
        # Pre-emptive revocation. MUTATION: requiring the attestation to exist.
        # The operator learns a merchant went bad and must be able to act before
        # the next boot loads its badge off disk.
        reg = self._reg()
        reg.revoke(MERCHANT)
        att = SA.sign_attestation(MERCHANT, self.audit, issued_at=1000,
                                  ttl=10000, signer=self.signer)
        reg2 = self._reg()
        reg2.add(att)
        self.assertIsNone(reg2.credential_for(MERCHANT, now=1500))

    def test_revocation_is_APPEND_ONLY_and_idempotent(self):
        # MUTATION: rewriting the file, or a delete path. There is deliberately
        # NO un-revoke: restoring trust must be a deliberate operator act, not
        # a call. So the store only ever grows.
        reg = self._reg()
        reg.revoke(MERCHANT)
        reg.revoke(MERCHANT)
        reg.revoke("sa_other")
        self.assertEqual(SA.FileRevocationStore(self.path).all(),
                         {MERCHANT, "sa_other"})
        self.assertFalse(hasattr(SA.FileRevocationStore, "unrevoke"))
        self.assertFalse(hasattr(SA.SellerRegistry, "unrevoke"))

    def test_subjects_are_normalized_so_case_cannot_evade(self):
        # MUTATION: storing the raw string. A live 402 returns EIP-55 checksummed
        # while crawls store lowercase -- the join that missed 64 of 69 endpoints
        # in advertised_prices. Here the same slip would make a revoked merchant
        # readable again by asking with different capitalization.
        reg = self._reg()
        reg.revoke(MERCHANT.upper())
        att = SA.sign_attestation(MERCHANT, self.audit, issued_at=1000,
                                  ttl=10000, signer=self.signer)
        reg2 = self._reg()
        reg2.add(att)
        self.assertIsNone(reg2.credential_for(MERCHANT, now=1500))

    def test_the_list_is_PUBLISHABLE_so_a_third_party_can_check(self):
        # The completion of third-party verifiability: a signed badge whose
        # revocation nobody can see is only as good as its TTL.
        # MUTATION: returning the internal set, which would let a caller mutate
        # the registry's revocations through the published view.
        reg = self._reg()
        reg.revoke(MERCHANT)
        pub = reg.published_revocations()
        self.assertEqual(pub["revoked"], [MERCHANT])
        pub["revoked"].append("0xdeadbeef")
        self.assertEqual(reg.published_revocations()["revoked"], [MERCHANT])

    def test_an_unwritable_store_FAILS_CLOSED_on_revoke(self):
        # MUTATION: fail-soft, mirroring reachability_ledger. Opposite call here
        # and the asymmetry is the point: a diagnostic that cannot log should
        # still answer, but a REVOCATION that silently does not persist leaves
        # the operator believing trust was withdrawn when it was not. Raise.
        store = SA.FileRevocationStore(os.path.join(self.tmp, "nope", "r.jsonl"))
        with self.assertRaises(Exception):
            store.add(MERCHANT)

    def test_an_unreadable_store_does_not_silently_forget_revocations(self):
        # MUTATION: returning an empty set on a corrupt file. Empty means "no
        # merchant is revoked", which is the most permissive possible answer to
        # a question about withdrawn trust. A junk LINE is skipped (one bad row
        # must not erase the rest), but an unreadable FILE raises.
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"subject": "%s"}\n' % MERCHANT)
            fh.write("not json at all\n")
            fh.write('{"subject": "sa_x"}\n')
        self.assertEqual(SA.FileRevocationStore(self.path).all(),
                         {MERCHANT, "sa_x"})

    def test_load_registry_REFUSES_when_the_revocation_list_is_unreadable(self):
        # MUTATION TESTING CAUGHT THIS GAP TOO: the asymmetric-failure rule was
        # implemented and no test exercised it, so `load_registry` could have
        # loaded badges while blind to revocations and stayed green.
        #
        # The asymmetry is the point. A bad ATTESTATION file costs a merchant
        # its floor -- strictly more conservative, so fail-open. An unreadable
        # REVOCATION list is the opposite: loading badges we cannot check
        # revocation for GRANTS trust the operator withdrew. Refuse.
        # A directory in place of the file makes open() raise without needing
        # chmod, which does nothing when the tests run as root.
        blocked = os.path.join(self.tmp, "as_a_dir.jsonl")
        os.mkdir(blocked)
        reg, err = SA.load_registry(None, blocked, signer=self.signer)
        self.assertIsNone(reg, "loaded badges despite being unable to read "
                               "the revocation list")
        self.assertIn("revocation", err)

    def test_load_registry_still_returns_a_registry_for_a_bad_BADGE_file(self):
        # The other half of the asymmetry, as a RESTRAINT CONTROL: a garbled
        # attestation file must not disable the tier's revocation machinery.
        # MUTATION: making the badge side fail-closed too -- that would let a
        # corrupt badge file take out the revocation list with it.
        path = os.path.join(self.tmp, "badges.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not json\n")
            fh.write('{"payload": {}}\n')
        reg, err = SA.load_registry(path, self.path, signer=self.signer)
        self.assertIsNotNone(reg)
        self.assertEqual(reg.loaded, 0)
        self.assertEqual(reg.skipped, 2)
        self.assertIsNone(err)

    def test_an_UNREADABLE_badge_file_still_returns_a_working_registry(self):
        # The stronger half of the restraint, and mutation testing showed the
        # test above did NOT reach it: bad LINES in a readable file are skipped
        # line-by-line and never touch the unreadable-FILE branch. So that
        # branch could have been made fail-closed and stayed green -- letting a
        # corrupt badge file take the revocation machinery down with it, which
        # is the failure this asymmetry exists to prevent.
        # MUTATION: `return None, (...)` on that branch -> this fails.
        blocked = os.path.join(self.tmp, "badges_as_dir.jsonl")
        os.mkdir(blocked)
        reg, err = SA.load_registry(blocked, self.path, signer=self.signer)
        self.assertIsNotNone(reg, "a corrupt BADGE file disabled the tier; only "
                                  "an unreadable REVOCATION list may do that")
        self.assertIn("attestation file", err)
        # And it is genuinely usable -- revocation still works.
        reg.revoke(MERCHANT)
        self.assertIn(MERCHANT, reg.published_revocations()["revoked"])

    def test_no_store_configured_still_revokes_for_this_process(self):
        # RESTRAINT CONTROL. Durability is an upgrade, not a precondition: a
        # registry with no store must still honour revoke() in-process.
        reg = SA.SellerRegistry(signer=self.signer)
        reg.issue(MERCHANT, self.audit, issued_at=1000, ttl=10000)
        reg.revoke(MERCHANT)
        self.assertIsNone(reg.credential_for(MERCHANT, now=1500))


class TestRevocationAuthority(unittest.TestCase):
    """Only the key holder may revoke, and revoking is MONOTONICALLY SAFE."""

    def test_the_token_is_per_subject_so_a_leak_revokes_ONE_merchant(self):
        # MUTATION: a single global admin secret. One leaked value would then
        # let the holder revoke every merchant in the registry.
        a = SA.sign_revoke_token(MERCHANT, key=b"k")
        b = SA.sign_revoke_token("0x" + "9" * 40, key=b"k")
        self.assertNotEqual(a, b)
        self.assertTrue(SA.verify_revoke_token(MERCHANT, a, key=b"k"))
        self.assertFalse(SA.verify_revoke_token(MERCHANT, b, key=b"k"))

    def test_it_is_domain_separated_from_a_BARE_hmac_of_the_subject(self):
        # THIS TEST WAS WRONG AND MUTATION TESTING CAUGHT IT. It used to compare
        # against `approvals.sign_approval_token`, which carries its OWN
        # "approve:" prefix -- so removing "revoke:" left the two still
        # unequal and the test still passing. A check aimed slightly to the
        # left of the thing it verifies, the same shape as cdp_preflight's
        # wrong default payee.
        #
        # The property that actually matters: the token must not be a bare HMAC
        # of the subject under the shared key. All these capabilities derive
        # from ONE secret (`blackwall._receipt_key`), so an unprefixed token
        # would be interchangeable with any future bare-HMAC capability over
        # the same string -- and the collision would be discovered by whoever
        # adds that one, not by us.
        # MUTATION: dropping the "revoke:" prefix -> this fails.
        import hashlib
        import hmac as _hmac
        bare = _hmac.new(b"k", MERCHANT.encode(), hashlib.sha256).hexdigest()[:32]
        self.assertNotEqual(SA.sign_revoke_token(MERCHANT, key=b"k"), bare)
        # And distinct from the two sibling capabilities, for the same reason.
        import approvals
        self.assertNotEqual(SA.sign_revoke_token(MERCHANT, key=b"k"),
                            approvals.sign_approval_token(MERCHANT, key=b"k"))

    def test_a_wrong_key_cannot_revoke(self):
        tok = SA.sign_revoke_token(MERCHANT, key=b"k")
        self.assertFalse(SA.verify_revoke_token(MERCHANT, tok, key=b"other"))

    def test_junk_tokens_are_refused_without_raising(self):
        for junk in (None, "", 7, b"x", "0" * 32):
            self.assertFalse(SA.verify_revoke_token(MERCHANT, junk, key=b"k"))

    def test_case_normalized_so_the_token_matches_either_spelling(self):
        # A live 402 returns EIP-55; an operator may type either.
        tok = SA.sign_revoke_token(MERCHANT.upper(), key=b"k")
        self.assertTrue(SA.verify_revoke_token(MERCHANT, tok, key=b"k"))


class TestTheLiveWire(unittest.TestCase):
    """A REAL server. Everything above is reachable only if it is WIRED.

    This tier was `seller_registry` -- a `forecast` parameter bound by nothing,
    so for its entire life no badge could be issued, consulted or revoked over
    HTTP while every unit test passed. Fifth instance of that pattern in this
    repo, which is why the coverage here is a running process rather than a
    function call. Wiring it took SEVEN edits and the sixth (the
    `BlackwallServer.__init__` parameter) was caught only by an AttributeError
    at boot -- the loudest of the seven and the only one that is not silent.
    """

    @classmethod
    def setUpClass(cls):
        import threading
        import blackwall
        # A REAL operator secret. `sign_revoke_token` now REFUSES the committed
        # dev fallback (audit finding: a token forged from it was accepted), so
        # the tier's revocation endpoint requires this to be set -- exactly as a
        # deploy must.
        cls._prev_key = os.environ.get("BLACKWALL_RECEIPT_KEY")
        os.environ["BLACKWALL_RECEIPT_KEY"] = "audit-test-operator-secret"
        cls.tmp = tempfile.mkdtemp()
        cls.revpath = os.path.join(cls.tmp, "revocations.jsonl")
        cls.signer = SA.attestation_signer(seed=SEED)
        cls.registry = SA.SellerRegistry(
            signer=cls.signer, revocations=SA.FileRevocationStore(cls.revpath))
        audit = SA.run_audit(readiness=_ready(), record=_record())
        # ISSUED AT THE REAL CLOCK. `forecast` calls credential_for with
        # int(time.time()) when no `now` is passed, so a badge issued at t=1000
        # is long expired and the tier silently does nothing -- which is how
        # this first failed, and is worth pinning as the reason.
        cls.att = cls.registry.issue(MERCHANT, audit,
                                     issued_at=int(time.time()), ttl=3600)
        cls.server = blackwall.BlackwallServer(
            host="127.0.0.1", port=0,
            reputation_source=blackwall.MockReputationSource(),
            seller_registry=cls.registry)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        for _ in range(200):
            if getattr(cls.server, "_httpd", None):
                break
            time.sleep(0.02)
        cls.base = "http://127.0.0.1:%d" % cls.server._httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server._httpd.shutdown()
        except Exception:
            pass
        if cls._prev_key is None:
            os.environ.pop("BLACKWALL_RECEIPT_KEY", None)
        else:
            os.environ["BLACKWALL_RECEIPT_KEY"] = cls._prev_key
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _post(self, path, body):
        import json as _json
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            self.base + path, data=_json.dumps(body).encode(),
            headers={"content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, _json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, _json.loads(e.read() or b"{}")

    def _get(self, path):
        import json as _json
        import urllib.request
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, _json.loads(r.read())

    def test_the_revocation_list_is_served_publicly(self):
        # MUTATION: removing the GET route, or returning the empty default even
        # when a registry is bound (the wired-and-inert shape).
        code, body = self._get("/v1/seller/revocations")
        self.assertEqual(code, 200)
        self.assertIn("revoked", body)
        self.assertIn("count", body)

    def test_revoking_requires_the_TOKEN(self):
        # MUTATION: dropping the verify_revoke_token call. Without it any
        # anonymous caller could strip any merchant's badge.
        # Its OWN subject: revocation is durable and append-only, so sharing
        # MERCHANT would make another test's revocation this test's
        # precondition -- unittest orders by name, so that passed or failed
        # depending on the method names.
        subject = "0x" + "1" * 40
        code, body = self._post("/v1/seller/revoke",
                                {"subject": subject, "token": "0" * 32})
        self.assertEqual(code, 403)
        self.assertNotIn(subject,
                         self._get("/v1/seller/revocations")[1]["revoked"])

    def test_a_missing_token_is_refused(self):
        code, _ = self._post("/v1/seller/revoke", {"subject": MERCHANT})
        self.assertEqual(code, 403)

    def test_an_unknown_subject_gets_the_SAME_403_not_a_404(self):
        # MUTATION: 404 for an unknown subject. That turns the endpoint into an
        # enumeration oracle for who holds a badge -- the same rule the
        # approvals endpoint follows.
        code, _ = self._post("/v1/seller/revoke",
                             {"subject": "0x" + "7" * 40, "token": "0" * 32})
        self.assertEqual(code, 403)

    def test_a_VALID_token_revokes_and_it_shows_up_in_the_public_list(self):
        import blackwall
        victim = "0x" + "d" * 40
        token = SA.sign_revoke_token(victim)
        code, body = self._post("/v1/seller/revoke",
                                {"subject": victim, "token": token})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["revoked"], victim)
        self.assertTrue(body["durable"])
        self.assertIn(victim, self._get("/v1/seller/revocations")[1]["revoked"])

    def test_revocation_actually_removes_the_FLOOR_from_a_live_verdict(self):
        # THE POINT. Everything else is plumbing; this asserts the verdict
        # changes. MUTATION: not threading seller_registry into forecast --
        # the badge would be stored, served and revocable and never affect a
        # verdict, which is the inert state this change exists to end.
        import blackwall
        req = {"counterparty": MERCHANT, "amount": "0.09", "asset": "USDC",
               "chain": "base"}
        code, before = self._post("/v1/forecast-payment", req)
        self.assertEqual(code, 200, before)
        reasons_before = " ".join(before.get("reasons") or [])

        token = SA.sign_revoke_token(MERCHANT)
        self.assertEqual(
            self._post("/v1/seller/revoke",
                       {"subject": MERCHANT, "token": token})[0], 200)

        code, after = self._post("/v1/forecast-payment", req)
        self.assertEqual(code, 200, after)
        reasons_after = " ".join(after.get("reasons") or [])
        self.assertIn("verified", reasons_before.lower(),
                      "the badge was not applied before revocation, so this "
                      "test cannot show it being withdrawn")
        self.assertNotIn("verified", reasons_after.lower())

    def test_an_UNCONFIGURED_revocation_secret_reports_503_not_403(self):
        # MUTATION TESTING CAUGHT THIS GAP: the branch was implemented and no
        # test reached it over HTTP. 403 for a missing secret would send an
        # operator hunting a token problem that does not exist, and it leaks
        # nothing -- it is a fact about our own configuration.
        prev = os.environ.pop("BLACKWALL_RECEIPT_KEY", None)
        try:
            code, body = self._post("/v1/seller/revoke",
                                    {"subject": MERCHANT, "token": "x" * 32})
            self.assertEqual(code, 503, body)
            self.assertEqual(body["error"], "revocation not configured")
        finally:
            if prev is not None:
                os.environ["BLACKWALL_RECEIPT_KEY"] = prev

    def test_there_is_NO_unrevoke_route(self):
        # MONOTONIC SAFETY. MUTATION: adding a restore endpoint. A stolen token
        # must only ever be able to REMOVE trust.
        for path in ("/v1/seller/unrevoke", "/v1/seller/restore"):
            code, _ = self._post(path, {"subject": MERCHANT, "token": "x"})
            self.assertIn(code, (404, 405), path)


if __name__ == "__main__":
    unittest.main()
