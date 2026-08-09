"""
Tests for secret_scan.py. The FALSE-POSITIVE controls matter most: a false HIGH would
STOP a legit payment, so the structural-field exclusion and pattern precision are the
things under guard. Each test states its mutation.
"""
import unittest

import secret_scan as S


class TestScanText(unittest.TestCase):
    def test_detects_aws_key_high(self):
        f = S.scan_text("here is AKIAIOSFODNN7EXAMPLE for you")
        self.assertEqual(f[0]["type"], "aws_access_key_id")
        self.assertEqual(f[0]["severity"], "high")

    def test_detects_github_and_openai_style(self):
        self.assertTrue(any(x["type"] == "github_pat" for x in
                            S.scan_text("tok ghp_" + "a" * 36)))
        self.assertTrue(any(x["severity"] == "high" for x in
                            S.scan_text("key sk-live_" + "A" * 24)))

    def test_labeled_private_key_high(self):
        # a key LABEL next to 64-hex -> unambiguous HIGH
        f = S.scan_text("private_key: 0x" + "a" * 64)
        self.assertTrue(any(x["severity"] == "high" for x in f))

    def test_ssn_medium(self):
        f = S.scan_text("SSN 123-45-6789")
        self.assertEqual(f[0]["type"], "ssn")
        self.assertEqual(f[0]["severity"], "medium")

    def test_mnemonic_shape_medium(self):
        f = S.scan_text("legal winner thank year wave sausage worth useful "
                        "legal winner thank yellow")            # 12 words
        self.assertTrue(any(x["type"] == "seed_phrase_shape" for x in f))
        self.assertTrue(all(x["severity"] == "medium" for x in f))

    def test_never_echoes_the_secret(self):
        # the raw secret must NOT appear in any finding (only a redacted hint).
        secret = "AKIAIOSFODNN7EXAMPLE"
        f = S.scan_text("leak " + secret)
        blob = repr(f)
        self.assertNotIn(secret, blob)                          # full value never present
        self.assertIn("hint", f[0])

    # ---- FALSE-POSITIVE controls (a false HIGH STOPs a legit payment) ----
    def test_plain_memo_no_finding(self):
        self.assertEqual(S.scan_text("thanks for the weather data, invoice 42"), [])

    def test_short_sentence_not_a_mnemonic(self):
        # 6 words, not a mnemonic length -> no flag
        self.assertEqual(S.scan_text("please pay me for services"), [])

    def test_bare_hex64_in_freetext_is_medium_not_high(self):
        # a bare 64-hex (could be a hash pasted into a memo) -> MEDIUM, never a HIGH STOP
        f = S.scan_text("ref 0x" + "b" * 64)
        self.assertTrue(f)
        self.assertTrue(all(x["severity"] == "medium" for x in f))

    def test_sk_short_sku_not_flagged(self):
        # REGRESSION (audit): a hyphenated SKU/promo like "sk-SUMMERSALE2024PROMO" must
        # NOT be flagged as an API key (that was a false STOP). Bare sk- needs 40+ chars.
        for benign in ["sku sk-SUMMERSALE2024PROMO50", "code sk-PRODUCTREF1234567890AB"]:
            self.assertEqual(S.scan_text(benign), [], benign)

    def test_real_sk_key_forms_flagged(self):
        # REGRESSION (audit): real key forms the old pattern MISSED must be caught.
        self.assertTrue(any(x["severity"] == "high" for x in
                            S.scan_text("key sk-proj-abcdEFGH1234567890ijklmnop")))
        self.assertTrue(any(x["severity"] == "high" for x in
                            S.scan_text("key sk_live_" + "a" * 24)))
        self.assertTrue(any(x["severity"] == "high" for x in       # legacy bare 48-char
                            S.scan_text("key sk-" + "A1b2" * 12)))


class TestScanPayload(unittest.TestCase):
    def test_structural_fields_are_not_scanned(self):
        # THE false-positive guard: a tx hash / address in a STRUCTURAL field must NOT
        # be flagged. Mutation: scan structural keys -> this flags a legit tx and FAILS.
        payload = {
            "counterparty": "0x" + "1" * 40,
            "transaction": {"to": "0x" + "2" * 40, "data": "0x" + "f" * 200},
            "hash": "0x" + "c" * 64,                            # a real tx hash
            "amount": "0.09", "asset": "USDC", "chain": "base",
        }
        self.assertEqual(S.scan_payload(payload), [])

    def test_scans_free_text_fields(self):
        payload = {"counterparty": "0x" + "1" * 40, "amount": "1.00",
                   "memo": "here is my seed: private key 0x" + "a" * 64}
        f = S.scan_payload(payload)
        self.assertEqual(S.top_severity(f), "high")
        self.assertEqual(f[0]["field"], "memo")

    def test_scans_nested_metadata(self):
        payload = {"counterparty": "0xabc",
                   "context": {"metadata": {"note": "AKIAIOSFODNN7EXAMPLE"}}}
        f = S.scan_payload(payload)
        self.assertTrue(any(x["type"] == "aws_access_key_id" for x in f))

    def test_credential_hidden_in_structural_field_is_caught(self):
        # REGRESSION (audit bypass): a CREDENTIAL nested under a structural key name
        # ("data"/"value") must still be caught -- HIGH patterns never collide with hex,
        # so they run on structural fields too. Mutation: skip structural fields entirely
        # -> this MISSES the credential and FAILS.
        self.assertTrue(any(x["type"] == "aws_access_key_id"
                            for x in S.scan_payload({"memo": {"data": "AKIAIOSFODNN7EXAMPLE"}})))
        self.assertTrue(any(x["severity"] == "high"
                            for x in S.scan_payload({"value": "ghp_" + "a" * 36})))
        # ...but a real tx hash in a structural field is STILL not flagged (the FP guard).
        self.assertEqual(S.scan_payload({"transaction": {"data": "0x" + "f" * 200},
                                         "hash": "0x" + "c" * 64}), [])

    def test_top_severity_and_summary(self):
        self.assertIsNone(S.top_severity([]))
        f = [{"severity": "medium", "type": "ssn"}, {"severity": "high", "type": "x"}]
        self.assertEqual(S.top_severity(f), "high")
        self.assertIn("ssn", S.redacted_summary(f))


class TestBoundaryInVerdict(unittest.TestCase):
    """HIGH secret -> STOP; MEDIUM -> HOLD; both fold via decide_payment(secret_findings=)."""
    GOOD = {"settlement_count": 500, "dispute_rate": 0.0, "distinct_payers": 30}

    def test_high_secret_stops(self):
        import blackwall as bw
        v = bw.decide_payment("0.09", dict(self.GOOD), ["0.09"], counterparty="0xA",
                              secret_findings=[{"type": "aws_access_key_id",
                                                "severity": "high", "field": "memo",
                                                "hint": "AKIA***LE"}])
        self.assertEqual(v["verdict"], "STOP")
        self.assertTrue(v["hard_stop"])
        # the reason must NOT contain a raw secret -- only type/field
        self.assertTrue(any("secret" in r.lower() for r in v["reasons"]))

    def test_medium_secret_holds(self):
        import blackwall as bw
        v = bw.decide_payment("0.09", dict(self.GOOD), ["0.09"], counterparty="0xA",
                              secret_findings=[{"type": "ssn", "severity": "medium",
                                                "field": "note", "hint": "1***9"}])
        self.assertEqual(v["verdict"], "HOLD")
        self.assertFalse(v["hard_stop"])

    def test_no_findings_no_effect(self):
        import blackwall as bw
        v = bw.decide_payment("0.09", dict(self.GOOD), ["0.09"], counterparty="0xA",
                              secret_findings=[])
        self.assertEqual(v["verdict"], "GO")


if __name__ == "__main__":
    unittest.main()
