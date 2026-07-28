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


class TestAnchorVerdictAutoPay(unittest.TestCase):
    """
    The x402 auto-pay loop: on 402, mint a payment from the challenge and retry.

    Mutation notes:
      - don't retry after minting -> test_pays_then_succeeds FAILS.
      - retry more than once / ignore the mint -> test_retries_only_once FAILS.
      - pass the wrong requirements to pay() -> test_pay_sees_accepts FAILS.
      - re-pay when x_payment already given -> test_no_double_pay FAILS.
      - let a raising pay() escape -> test_pay_failure_fails_open FAILS.
    """
    VERDICT = {"verdict": "STOP", "score": 0.0, "receipt_id": "bw_abc"}
    # a Traceipt v1 /attest 402 challenge (maxAmountRequired, bare network name)
    CHALLENGE = {"x402Version": 1, "error": "X-PAYMENT header is required",
                 "accepts": [{"scheme": "exact", "network": "base",
                              "maxAmountRequired": "1000", "payTo": "0x" + "e" * 40,
                              "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                              "resource": "https://api.traceipt.xyz/attest"}]}

    def _transport_402_then_201(self, calls, pay_ok=True):
        def t(url, data, headers, timeout):
            calls.append(headers.get("X-PAYMENT"))
            if headers.get("X-PAYMENT"):
                return 201, {"attestation": {"attestation_id": "att_paid",
                                             "status": "pending"},
                             "proof_url": "https://api.traceipt.xyz/attest/att_paid/proof"}
            return 402, self.CHALLENGE
        return t

    def test_pays_then_succeeds(self):
        calls = []
        r = T.anchor_verdict("https://api.traceipt.xyz", self.VERDICT,
                             pay=lambda req: "MINTED_B64",
                             _transport=self._transport_402_then_201(calls))
        self.assertTrue(r["ok"])
        self.assertEqual(r["attestation_id"], "att_paid")
        self.assertEqual(calls, [None, "MINTED_B64"])  # unpaid, then paid retry

    def test_retries_only_once(self):
        # pay yields a header but the server STILL 402s -> we must not loop forever
        calls = []
        def always_402(url, data, headers, timeout):
            calls.append(headers.get("X-PAYMENT"))
            return 402, self.CHALLENGE
        r = T.anchor_verdict("https://x", self.VERDICT, pay=lambda req: "H",
                             _transport=always_402)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "payment_required")
        self.assertEqual(len(calls), 2)  # original + exactly one paid retry

    def test_pay_sees_accepts(self):
        seen = {}
        def pay(req):
            seen.update(req)
            return "H"
        T.anchor_verdict("https://x", self.VERDICT, pay=pay,
                         _transport=self._transport_402_then_201([]))
        self.assertEqual(seen.get("payTo"), "0x" + "e" * 40)
        self.assertEqual(seen.get("maxAmountRequired"), "1000")

    def test_no_double_pay_when_header_given(self):
        # an explicit x_payment is used as-is; pay() must NOT be invoked
        calls = []
        def pay(req):
            raise AssertionError("pay must not be called when x_payment is given")
        def t(url, data, headers, timeout):
            calls.append(headers.get("X-PAYMENT"))
            return 201, {"attestation": {"attestation_id": "att_1"}}
        r = T.anchor_verdict("https://x", self.VERDICT, x_payment="PREPAID",
                             pay=pay, _transport=t)
        self.assertTrue(r["ok"])
        self.assertEqual(calls, ["PREPAID"])

    def test_pay_failure_fails_open(self):
        def boom(req):
            raise RuntimeError("no funds")
        r = T.anchor_verdict("https://x", self.VERDICT, pay=boom,
                             _transport=lambda *a: (402, self.CHALLENGE))
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "payment_required")  # never raises

    def test_pay_returning_none_stays_402(self):
        r = T.anchor_verdict("https://x", self.VERDICT, pay=lambda req: None,
                             _transport=lambda *a: (402, self.CHALLENGE))
        self.assertEqual(r["reason"], "payment_required")

    def test_spend_cap_refuses_oversized_challenge(self):
        # a spoofed 402 demanding more than the cap -> we must NOT sign
        def pay(req):
            raise AssertionError("must not sign a payment over the cap")
        big = {"accepts": [{"payTo": "0x" + "b" * 40, "maxAmountRequired": "5000000",
                            "asset": "0x0", "network": "base"}]}
        r = T.anchor_verdict("https://x", self.VERDICT, pay=pay,
                             max_amount_atomic=1_000_000,
                             _transport=lambda *a: (402, big))
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "amount_exceeds_cap")

    def test_spend_cap_allows_within_budget(self):
        calls = []
        r = T.anchor_verdict("https://api.traceipt.xyz", self.VERDICT,
                             pay=lambda req: "MINTED", max_amount_atomic=1_000_000,
                             _transport=self._transport_402_then_201(calls))
        self.assertTrue(r["ok"])  # 1000 <= 1_000_000
        self.assertEqual(calls, [None, "MINTED"])

    def test_spend_cap_refuses_unparseable_amount(self):
        # fail-safe: with a cap set, an amount we can't parse is treated as over it
        def pay(req):
            raise AssertionError("must not sign when the amount can't be bounded")
        bad = {"accepts": [{"payTo": "0x" + "b" * 40, "maxAmountRequired": "lots"}]}
        r = T.anchor_verdict("https://x", self.VERDICT, pay=pay,
                             max_amount_atomic=1_000_000,
                             _transport=lambda *a: (402, bad))
        self.assertEqual(r["reason"], "amount_exceeds_cap")

    def test_no_accepts_no_pay(self):
        # a 402 with no accepts array -> nothing to satisfy, pay() not called
        def pay(req):
            raise AssertionError("pay must not be called without requirements")
        r = T.anchor_verdict("https://x", self.VERDICT, pay=pay,
                             _transport=lambda *a: (402, {"error": "pay"}))
        self.assertEqual(r["reason"], "payment_required")


if __name__ == "__main__":
    unittest.main()
