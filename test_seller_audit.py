"""
Tests for seller_audit.py -- the earned "verified merchant" tier. Each test states
the mutation it kills. The anti-corruption rules (earned, bounded, revocable, never
a STOP override) are the point, so they get the most coverage.
"""
import unittest

import seller_audit as SA

MERCHANT = "0x" + "c" * 40


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
    """
    Mutation notes:
      - don't cover a field in the signature -> test_tamper_rejected FAILS.
      - ignore expiry -> test_expiry FAILS.
      - ignore revocation -> test_revocation FAILS.
    """
    def setUp(self):
        self.audit = SA.run_audit(readiness=_ready(), record=_record())
        self.att = SA.sign_attestation(MERCHANT, self.audit, issued_at=1000,
                                       ttl=1000, key=b"k")

    def test_cannot_attest_failed_audit(self):
        with self.assertRaises(ValueError):
            SA.sign_attestation(MERCHANT, SA.run_audit(record=_record(sanctioned=True)),
                                issued_at=1000)

    def test_valid(self):
        ok, reason = SA.verify_attestation(self.att, now=1500, key=b"k")
        self.assertTrue(ok, reason)
        self.assertEqual(self.att["subject"], MERCHANT)   # lowercased

    def test_tamper_rejected(self):
        forged = dict(self.att, floor=0.99)               # try to raise the floor
        self.assertFalse(SA.verify_attestation(forged, now=1500, key=b"k")[0])
        forged2 = dict(self.att, grade="B")   # original is A; any change breaks the sig
        self.assertFalse(SA.verify_attestation(forged2, now=1500, key=b"k")[0])
        forged3 = dict(self.att, subject="0x" + "9" * 40)
        self.assertFalse(SA.verify_attestation(forged3, now=1500, key=b"k")[0])

    def test_wrong_key_rejected(self):
        self.assertFalse(SA.verify_attestation(self.att, now=1500, key=b"other")[0])

    def test_expiry(self):
        self.assertFalse(SA.verify_attestation(self.att, now=2000, key=b"k")[0])
        self.assertEqual(SA.verify_attestation(self.att, now=2000, key=b"k")[1],
                         "expired")

    def test_revocation_by_id_and_subject(self):
        self.assertFalse(SA.verify_attestation(
            self.att, now=1500, key=b"k",
            revoked={self.att["attestation_id"]})[0])
        self.assertFalse(SA.verify_attestation(
            self.att, now=1500, key=b"k", revoked={MERCHANT})[0])


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.reg = SA.SellerRegistry(key=b"k")
        self.reg.issue(MERCHANT, SA.run_audit(readiness=_ready(), record=_record()),
                       issued_at=1000, ttl=1000)

    def test_credential_for_valid(self):
        cred = self.reg.credential_for(MERCHANT, now=1500)
        self.assertEqual(cred["grade"], "A")
        self.assertEqual(cred["floor"], SA.GRADE_FLOORS["A"])

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
