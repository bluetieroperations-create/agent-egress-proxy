"""
Tests for traceipt_attest.py -- Black_Wall verdict -> Traceipt /attest bridge.

Each test states the mutation it kills. The request-shape test asserts against
Traceipt's OWN /attest contract (replicated here) so the bridge can't drift from
what the live service accepts.
"""
import re
import unittest

import traceipt_attest as T

# Traceipt's contract (traceipt/traceipt/schema.py::validate_attestation_request):
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _traceipt_would_accept(body):
    """Replicate Traceipt's validate_attestation_request acceptance rules."""
    if not isinstance(body, dict):
        return False
    if not set(body) <= {"hash", "type", "ref"}:
        return False
    if not _HASH_RE.match(body.get("hash", "")):
        return False
    if "type" in body and not (isinstance(body["type"], str) and 0 < len(body["type"]) <= 40):
        return False
    if "ref" in body and not (isinstance(body["ref"], str) and 0 < len(body["ref"]) <= 300):
        return False
    return True


class TestDigest(unittest.TestCase):
    """
    Mutation notes:
      - non-canonical json (unsorted) -> test_deterministic FAILS.
      - wrong prefix/length -> test_format FAILS (Traceipt would reject).
    """
    def test_format_matches_traceipt(self):
        d = T.verdict_digest({"verdict": "GO", "score": 0.9})
        self.assertRegex(d, r"^sha256:[0-9a-f]{64}$")

    def test_deterministic_and_order_independent(self):
        a = T.verdict_digest({"verdict": "GO", "score": 0.9})
        b = T.verdict_digest({"score": 0.9, "verdict": "GO"})  # keys reordered
        self.assertEqual(a, b)

    def test_content_sensitive(self):
        self.assertNotEqual(
            T.verdict_digest({"verdict": "GO"}),
            T.verdict_digest({"verdict": "STOP"}))


class TestBuildAttestRequest(unittest.TestCase):
    """
    Mutation notes:
      - emit an extra key -> test_conforms FAILS (Traceipt rejects unknown keys).
      - don't truncate ref -> test_ref_truncated FAILS.
    """
    DIGEST = "sha256:" + "a" * 64

    def test_conforms_to_traceipt(self):
        self.assertTrue(_traceipt_would_accept(T.build_attest_request(self.DIGEST)))
        self.assertTrue(_traceipt_would_accept(
            T.build_attest_request(self.DIGEST, ref="bw_123")))

    def test_default_type(self):
        self.assertEqual(T.build_attest_request(self.DIGEST)["type"], T.ATTEST_TYPE)

    def test_ref_truncated_to_300(self):
        body = T.build_attest_request(self.DIGEST, ref="x" * 500)
        self.assertEqual(len(body["ref"]), 300)
        self.assertTrue(_traceipt_would_accept(body))

    def test_type_truncated_to_40(self):
        body = T.build_attest_request(self.DIGEST, type_="y" * 60)
        self.assertLessEqual(len(body["type"]), 40)
        self.assertTrue(_traceipt_would_accept(body))


class TestAnchorVerdict(unittest.TestCase):
    """
    Mutation notes:
      - let a transport exception propagate -> test_fail_open_on_error FAILS.
      - treat 402 as success -> test_payment_required FAILS.
      - not parse the 201 envelope -> test_success FAILS.
    """
    VERDICT = {"verdict": "STOP", "score": 0.0, "receipt_id": "bw_abc"}

    def test_success(self):
        def ok(url, data, headers, timeout):
            self.assertTrue(url.endswith("/attest"))
            return 201, {"attestation": {"attestation_id": "att_1", "status": "pending"},
                         "proof_url": "https://api.traceipt.xyz/attest/att_1/proof"}
        r = T.anchor_verdict("https://api.traceipt.xyz", self.VERDICT, _transport=ok)
        self.assertTrue(r["ok"])
        self.assertEqual(r["attestation_id"], "att_1")
        self.assertTrue(r["proof_url"].endswith("/proof"))
        self.assertTrue(r["digest"].startswith("sha256:"))

    def test_idempotent_200(self):
        def dup(url, data, headers, timeout):
            return 200, {"attestation": {"attestation_id": "att_1", "status": "anchored"}}
        self.assertTrue(T.anchor_verdict("https://x", self.VERDICT, _transport=dup)["ok"])

    def test_payment_required(self):
        r = T.anchor_verdict("https://x", self.VERDICT,
                             _transport=lambda *a: (402, {}))
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "payment_required")
        self.assertIn("digest", r)  # still computed

    def test_fail_open_on_error(self):
        def boom(*a):
            raise ConnectionError("traceipt unreachable")
        r = T.anchor_verdict("https://x", self.VERDICT, _transport=boom)
        self.assertFalse(r["ok"])  # never raises -- verdict unaffected

    def test_sends_payment_header_when_given(self):
        seen = {}
        def cap(url, data, headers, timeout):
            seen.update(headers)
            return 201, {"attestation": {}}
        T.anchor_verdict("https://x", self.VERDICT, x_payment="b64pay", _transport=cap)
        self.assertEqual(seen.get("X-PAYMENT"), "b64pay")


if __name__ == "__main__":
    unittest.main()
