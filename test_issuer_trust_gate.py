#!/usr/bin/env python3
"""
test_issuer_trust_gate.py -- TDD for graduating the earned issuer-trust grade into the
RWA verdict. Each test names the MUTATION it kills. Mirrors the SYBIL_RING_GATES
descriptive-first discipline: the lock defaults OFF (descriptive only).
"""
import os
import tempfile
import unittest

import issuer_trust_gate as itg
from issuer_trust_gate import (IssuerTrustSource, apply_issuer_trust,
                               build_issuer_grades)
from rwa_ledger import RwaLedger


def _buy(issuer, token, tx):
    return {"event": "buy", "receipt_id": "r:%s" % tx, "issuer": issuer,
            "token": token, "chain": "ethereum", "verdict": "GO", "payer": token}


def _out(issuer, token, tx, settled=True):
    return {"event": "outcome", "receipt_id": "r:%s" % tx, "issuer": issuer,
            "token": token, "chain": "ethereum", "settled": settled,
            "holds_balance": settled}


class TestBuildGrades(unittest.TestCase):
    def setUp(self):
        self.led = RwaLedger(os.path.join(tempfile.mkdtemp(), "rwa.jsonl"))

    def test_gradeable_issuer_appears(self):
        # >=5 settled outcomes -> a real grade (not "insufficient").
        for i in range(6):
            tk = "0x" + ("%02x" % i) * 20
            self.led.record(_buy("ondo", tk, i))
            self.led.record(_out("ondo", tk, i))
        grades = build_issuer_grades(self.led)
        self.assertIn("ondo", grades)
        self.assertNotEqual(grades["ondo"]["grade"], "insufficient")
        self.assertEqual(grades["ondo"]["outcomes"], 6)

    def test_thin_issuer_insufficient(self):
        # MUTATION: grading an issuer with <5 outcomes would fabricate trust from nothing.
        self.led.record(_buy("thin", "0x" + "11" * 20, 1))
        self.led.record(_out("thin", "0x" + "11" * 20, 1))
        grades = build_issuer_grades(self.led)
        self.assertEqual(grades["thin"]["grade"], "insufficient")

    def test_failsoft_bad_ledger(self):
        # MUTATION: build must NEVER raise -- a broken corpus returns {} not a crash.
        class Boom:
            def load(self):
                raise IOError("disk")
        self.assertEqual(build_issuer_grades(Boom()), {})

    def test_events_passthrough_avoids_reload(self):
        evs = [_buy("ondo", "0x" + "0%d" % i * 20 if False else "0x" + ("%02x" % i) * 20, i)
               for i in range(6)]
        evs += [_out("ondo", "0x" + ("%02x" % i) * 20, i) for i in range(6)]
        grades = build_issuer_grades(self.led, events=evs)
        self.assertIn("ondo", grades)


class TestSource(unittest.TestCase):
    def test_o1_lookup_and_len(self):
        src = IssuerTrustSource({"ondo": {"grade": "medium", "outcomes": 7}})
        self.assertEqual(src.grade("ondo")["grade"], "medium")
        self.assertIsNone(src.grade("unknown"))
        self.assertEqual(len(src), 1)

    def test_from_ledger(self):
        led = RwaLedger(os.path.join(tempfile.mkdtemp(), "rwa.jsonl"))
        for i in range(6):
            tk = "0x" + ("%02x" % i) * 20
            led.record(_buy("ondo", tk, i))
            led.record(_out("ondo", tk, i))
        src = IssuerTrustSource.from_ledger(led)
        self.assertIsNotNone(src.grade("ondo"))


class TestApplyFold(unittest.TestCase):
    def test_descriptive_when_lock_off(self):
        # MUTATION: with the lock OFF, a LOW grade must NOT gate -- gated=False always.
        v = apply_issuer_trust({"verdict": "GO"}, {"grade": "low", "outcomes": 9},
                               gates_on=False)
        self.assertEqual(v["signals"]["issuer_trust"]["grade"], "low")
        self.assertFalse(v["signals"]["issuer_trust"]["gated"])
        self.assertEqual(v["verdict"], "GO")   # unchanged -- fold itself never gates
        self.assertTrue(any("advisory only" in r for r in v["reasons"]))

    def test_gated_flag_when_lock_on_and_low(self):
        # MUTATION: when the lock is ON and grade is LOW, gated must be True so the
        # aggregate can weigh it.
        v = apply_issuer_trust({"verdict": "GO"}, {"grade": "low", "outcomes": 9},
                               gates_on=True)
        self.assertTrue(v["signals"]["issuer_trust"]["gated"])
        # even gated, the fold ITSELF does not flip the verdict (aggregate does).
        self.assertEqual(v["verdict"], "GO")

    def test_high_grade_never_gated(self):
        # MUTATION: a HIGH/MEDIUM grade must never be marked gated even with lock on.
        for g in ("high", "medium"):
            v = apply_issuer_trust({"verdict": "GO"}, {"grade": g, "outcomes": 9},
                                   gates_on=True)
            self.assertFalse(v["signals"]["issuer_trust"]["gated"])

    def test_no_grade_info_noop(self):
        base = {"verdict": "GO"}
        self.assertEqual(apply_issuer_trust(base, None), base)
        self.assertEqual(apply_issuer_trust(base, {"grade": "bogus"}), base)

    def test_non_mutating(self):
        base = {"verdict": "GO", "signals": {"x": 1}}
        apply_issuer_trust(base, {"grade": "low", "outcomes": 9}, gates_on=True)
        self.assertNotIn("issuer_trust", base["signals"])   # original untouched

    def test_default_gates_on_reads_module_lock(self):
        # MUTATION: apply must consult ISSUER_TRUST_GATES when gates_on is omitted.
        self.assertFalse(itg.ISSUER_TRUST_GATES)   # ship default
        v = apply_issuer_trust({"verdict": "GO"}, {"grade": "low", "outcomes": 9})
        self.assertFalse(v["signals"]["issuer_trust"]["gated"])


class TestAggregateIntegration(unittest.TestCase):
    def test_issuer_trust_advisory_bad_only_when_gated(self):
        from rwa_aggregate import aggregate
        # gated -> counted as an advisory concern.
        agg = aggregate({"issuer_trust": {"grade": "low", "gated": True, "outcomes": 9}})
        self.assertGreater(agg["advisory_bad_weight"], 0)
        self.assertTrue(any("issuer" in c.lower() for c in agg["advisory"]))
        # not gated (lock off) -> NOT a concern, descriptive only.
        agg2 = aggregate({"issuer_trust": {"grade": "low", "gated": False, "outcomes": 9}})
        self.assertEqual(agg2["advisory_bad_weight"], 0.0)


if __name__ == "__main__":
    unittest.main()
