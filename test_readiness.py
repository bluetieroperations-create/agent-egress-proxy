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
