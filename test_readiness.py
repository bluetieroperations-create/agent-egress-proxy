"""
Tests for readiness.py -- the Ontario endpoint-readiness enrichment signal.

Run: python -m unittest test_readiness.py -v
"""
import unittest

import readiness as R


class TestNormalizeReadiness(unittest.TestCase):
    """
    Mutation notes:
      - accept any string as grade -> test_rejects_unknown_grade FAILS.
      - ignore the can-pay `report` wrapper -> test_canpay_shape FAILS.
      - drop the decision->grade fallback -> test_decision_fallback FAILS.
      - treat bool as a numeric score -> test_score_typecheck FAILS.
    """
    def test_canpay_shape(self):
        r = R.normalize_readiness(
            {"decision": "allow", "report": {"grade": "ready", "readiness_score": 92}})
        self.assertEqual(r["grade"], "ready")
        self.assertEqual(r["score"], 92)
        self.assertEqual(r["decision"], "allow")
        self.assertEqual(r["source"], "ontario")

    def test_readiness_report_shape(self):
        r = R.normalize_readiness({"grade": "close", "readiness_score": 70})
        self.assertEqual(r["grade"], "close")
        self.assertEqual(r["score"], 70)

    def test_decision_fallback(self):
        # no explicit grade -> derive from the can-pay decision
        self.assertEqual(R.normalize_readiness({"decision": "deny"})["grade"], "needs_work")
        self.assertEqual(R.normalize_readiness({"decision": "review"})["grade"], "close")

    def test_rejects_unknown_grade(self):
        self.assertIsNone(R.normalize_readiness({"grade": "amazing"}))
        self.assertIsNone(R.normalize_readiness({"decision": "maybe"}))
        self.assertIsNone(R.normalize_readiness({}))

    def test_non_dict_safe(self):
        for bad in (None, "x", 5, [1, 2]):
            self.assertIsNone(R.normalize_readiness(bad))

    def test_score_typecheck(self):
        # bool is not a score; a non-numeric score is dropped, grade still parsed
        r = R.normalize_readiness({"grade": "ready", "readiness_score": True})
        self.assertIsNone(r["score"])
        r2 = R.normalize_readiness({"grade": "ready", "readiness_score": "high"})
        self.assertIsNone(r2["score"])


class TestApplyReadiness(unittest.TestCase):
    """
    Mutation notes:
      - let 'needs_work' escalate but also let 'ready' upgrade -> test_never_upgrades FAILS.
      - escalate HOLD/STOP too (not just GO) -> test_needs_work_only_touches_go FAILS.
      - mutate the input dict in place -> test_does_not_mutate FAILS.
      - skip the None no-op -> test_none_is_noop FAILS.
    """
    def _v(self, verdict="GO", reasons=None, signals=None):
        return {"verdict": verdict, "score": 0.8,
                "reasons": list(reasons or ["base reason"]),
                "signals": dict(signals or {"counterparty_reputation": 0.8})}

    def test_needs_work_escalates_go_to_hold(self):
        out = R.apply_readiness(self._v("GO"), {"grade": "needs_work"})
        self.assertEqual(out["verdict"], "HOLD")
        self.assertEqual(out["signals"]["endpoint_readiness"], "needs_work")
        self.assertTrue(any("escalated GO->HOLD" in r for r in out["reasons"]))

    def test_never_upgrades(self):
        # 'ready' must NOT turn a HOLD into a GO
        out = R.apply_readiness(self._v("HOLD"), {"grade": "ready"})
        self.assertEqual(out["verdict"], "HOLD")
        # ...and must NOT turn a STOP into anything softer
        out2 = R.apply_readiness(self._v("STOP"), {"grade": "ready"})
        self.assertEqual(out2["verdict"], "STOP")

    def test_needs_work_only_touches_go(self):
        # needs_work on a STOP stays STOP (already maximally cautious)
        out = R.apply_readiness(self._v("STOP"), {"grade": "needs_work"})
        self.assertEqual(out["verdict"], "STOP")
        # on a HOLD stays HOLD (no double-escalation target)
        out2 = R.apply_readiness(self._v("HOLD"), {"grade": "needs_work"})
        self.assertEqual(out2["verdict"], "HOLD")

    def test_ready_annotates_without_changing_verdict(self):
        out = R.apply_readiness(self._v("GO"), {"grade": "ready"})
        self.assertEqual(out["verdict"], "GO")
        self.assertEqual(out["signals"]["endpoint_readiness"], "ready")

    def test_none_is_noop(self):
        v = self._v("GO")
        self.assertIs(R.apply_readiness(v, None), v)
        self.assertIs(R.apply_readiness(v, {}), v)
        self.assertIs(R.apply_readiness(v, {"grade": "bogus"}), v)

    def test_does_not_mutate(self):
        v = self._v("GO")
        before_reasons = list(v["reasons"])
        before_verdict = v["verdict"]
        R.apply_readiness(v, {"grade": "needs_work"})
        self.assertEqual(v["reasons"], before_reasons)
        self.assertEqual(v["verdict"], before_verdict)


class TestScoreReadiness(unittest.TestCase):
    """
    Mutation notes:
      - count unknown signals as satisfied -> test_empty_is_needs_work FAILS.
      - drop normalization (raw weight sum) -> test_score_is_0_100 FAILS.
      - wrong grade thresholds -> test_grades FAILS.
    """
    def test_empty_is_needs_work(self):
        r = R.score_readiness({})
        self.assertEqual(r["score"], 0)
        self.assertEqual(r["grade"], "needs_work")
        self.assertEqual(r["source"], "local")

    def test_full_is_ready_100(self):
        allsig = {k: True for k in R._READINESS_WEIGHTS}
        r = R.score_readiness(allsig)
        self.assertEqual(r["score"], 100)
        self.assertEqual(r["grade"], "ready")

    def test_score_is_0_100(self):
        # only the 402 challenge (weight 20 of 95) -> normalized, not raw 20
        r = R.score_readiness({"payment_challenge": True})
        self.assertGreater(r["score"], 0)
        self.assertLessEqual(r["score"], 100)
        self.assertNotEqual(r["score"], 20)  # normalized, not the raw weight

    def test_grades(self):
        # craft observed sets around the thresholds
        self.assertEqual(R.score_readiness({k: True for k in R._READINESS_WEIGHTS})["grade"], "ready")
        # 402 + manifest present/well-formed = 50/95 ~ 53 -> close
        mid = {"payment_challenge": True, "manifest_present": True, "manifest_well_formed": True}
        self.assertEqual(R.score_readiness(mid)["grade"], "close")
        self.assertEqual(R.score_readiness({"robots": True})["grade"], "needs_work")


class TestLocalReadinessSource(unittest.TestCase):
    """
    Mutation notes:
      - treat 'reached nothing' as needs_work instead of None -> test_all_unreachable FAILS.
      - count a 404 manifest as present -> test_partial FAILS.
      - skip the https check from the URL scheme -> test_https_from_scheme FAILS.
    """
    def _fetch_map(self, mapping):
        # mapping: url-suffix-substring -> (status, body bytes); missing -> None
        def fetch(url):
            for key, val in mapping.items():
                if key in url:
                    return (val[0], {}, val[1])
            return None
        return fetch

    def test_ready_endpoint(self):
        x402 = b'{"x402Version":1,"accepts":[{}]}'
        fetch = self._fetch_map({
            "/paid": (402, x402),
            "/.well-known/x402": (200, x402),
            "/openapi.json": (200, b"{}"),
            "robots.txt": (200, b"User-agent: *"),
            "https://api.example.com/": (200, b'<html>schema.org</html>'),
        })
        src = R.LocalReadinessSource(fetch=fetch)
        out = src.check("https://api.example.com/paid")
        self.assertEqual(out["source"], "local")
        self.assertTrue(out["signals"]["payment_challenge"])
        self.assertTrue(out["signals"]["manifest_well_formed"])
        self.assertTrue(out["signals"]["https"])
        self.assertEqual(out["grade"], "ready")

    def test_partial(self):
        # manifest 404 (not present), endpoint reachable but no 402
        fetch = self._fetch_map({
            "/paid": (200, b"ok"),
            "/.well-known/x402": (404, b"nope"),
            "https://api.example.com/": (200, b"<html></html>"),
        })
        out = R.LocalReadinessSource(fetch=fetch).check("https://api.example.com/paid")
        self.assertFalse(out["signals"]["manifest_present"])
        self.assertFalse(out["signals"]["payment_challenge"])
        self.assertIn(out["grade"], ("needs_work", "close"))

    def test_all_unreachable_is_unknown(self):
        # fetch returns None for everything -> reached nothing -> None (not needs_work)
        src = R.LocalReadinessSource(fetch=lambda url: None)
        self.assertIsNone(src.check("https://api.example.com/paid"))

    def test_https_from_scheme(self):
        fetch = self._fetch_map({"api.example.com": (200, b"{}")})
        out = R.LocalReadinessSource(fetch=fetch).check("http://api.example.com/paid")
        self.assertFalse(out["signals"]["https"])

    def test_bad_url_is_none(self):
        src = R.LocalReadinessSource(fetch=lambda url: (200, {}, b"{}"))
        self.assertIsNone(src.check("not-a-url"))
        self.assertIsNone(src.check(None))


class TestOntarioReadinessSource(unittest.TestCase):
    """
    Mutation notes:
      - raise instead of fail-open on transport error -> test_fail_open FAILS.
      - call Ontario even with no endpoint url -> test_no_endpoint_skips FAILS.
    """
    def test_happy_path_via_transport(self):
        seen = {}
        def fake(url, body):
            seen["url"] = url; seen["body"] = body
            return {"decision": "allow", "report": {"grade": "ready", "readiness_score": 90}}
        src = R.OntarioReadinessSource("https://ontarioprotocol.com", transport=fake)
        out = src.check("https://api.example.com/paid")
        self.assertEqual(out["grade"], "ready")
        self.assertEqual(seen["url"], "https://ontarioprotocol.com/api/agent/can-pay")
        self.assertEqual(seen["body"]["endpoint"], "https://api.example.com/paid")

    def test_fail_open_on_transport_error(self):
        def boom(url, body):
            raise OSError("network down")
        src = R.OntarioReadinessSource("https://ontarioprotocol.com", transport=boom)
        self.assertIsNone(src.check("https://api.example.com/paid"))  # not an exception

    def test_fail_open_on_malformed(self):
        src = R.OntarioReadinessSource("https://x", transport=lambda u, b: {"junk": 1})
        self.assertIsNone(src.check("https://api.example.com/paid"))

    def test_no_endpoint_skips(self):
        called = {"n": 0}
        def fake(url, body):
            called["n"] += 1; return {"grade": "ready"}
        src = R.OntarioReadinessSource("https://x", transport=fake)
        self.assertIsNone(src.check(None))
        self.assertIsNone(src.check(""))
        self.assertEqual(called["n"], 0)


if __name__ == "__main__":
    unittest.main()
