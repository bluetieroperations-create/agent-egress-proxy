"""
test_refresh_guard.py -- guards the automated-refresh release gate (Stage 2).

The point: a bad refresh must NEVER ship. Each test states the mutation it kills.
"""
import unittest

import refresh_guard as G


def _stats(payees, edges, age_days, gating_reachable=True):
    # a minimal stats dict shaped like store_stats() output.
    return {"payees": payees, "edges": edges, "age_days": age_days,
            "gating_reachable": gating_reachable, "payers": payees * 5,
            "anchors": max(0, payees // 8), "subfull_false_flag_rate": 0.0}


class TestAssessRefresh(unittest.TestCase):
    # a healthy refresh: similar size, clearly fresher.
    OLD = _stats(290, 23000, age_days=45)

    def test_accepts_healthy_refresh(self):
        new = _stats(300, 24000, age_days=2)
        r = G.assess_refresh(self.OLD, new)
        self.assertTrue(r["accept"], r["reasons"])
        self.assertFalse(r["reasons"])

    def test_rejects_payee_collapse(self):
        # partial crawl: 290 -> 5 payees. Mutation: drop the retention check -> a sparse
        # store ships and sends the whole corpus cold-start (HOLD).
        new = _stats(5, 400, age_days=1)
        r = G.assess_refresh(self.OLD, new)
        self.assertFalse(r["accept"])
        self.assertTrue(any("collapsed" in x for x in r["reasons"]))

    def test_rejects_edge_collapse_even_if_payees_ok(self):
        # payees retained but edges gutted (crawl truncated each payee's history).
        new = _stats(290, 1000, age_days=1)
        r = G.assess_refresh(self.OLD, new)
        self.assertFalse(r["accept"])
        self.assertTrue(any("edge count" in x for x in r["reasons"]))

    def test_rejects_no_progress(self):
        # A TRUE no-op: no fresher AND no new settlements -> pointless churn.
        #
        # THIS TEST USED TO ASSERT THE BUG. It passed `_stats(300, 24000)` against an OLD
        # of `_stats(290, 23000)` -- a candidate carrying ONE THOUSAND MORE EDGES -- and
        # demanded a reject, because it encoded the same "progress means age" assumption
        # the code did. So the guard and its test agreed with each other and both were
        # wrong, which is why this survived until production showed it.
        #
        # Mutation: use `>` instead of `>=` -> an equal-age genuine no-op would ship.
        new = _stats(290, 23000, age_days=45)          # identical store, re-crawled
        r = G.assess_refresh(self.OLD, new)
        self.assertFalse(r["accept"])
        self.assertTrue(any("no progress" in x for x in r["reasons"]))

    def test_equal_age_with_NEW_SETTLEMENTS_is_progress(self):
        # THE 2026-09-28 PRODUCTION REJECT, to the number. Run #9 crawled 9,388 new
        # settlements (76,544 -> 85,932 edges, +12%) and the guard threw all of it away
        # because both stores rounded to 0 days old and `0 >= 0`.
        #
        # The failure is structural, not a near-miss: age is whole days, so once a fresh
        # corpus is committed, NO refresh that day can ever show age progress. It hid for
        # weeks because the cron runs against a 7-day-old store where age always improves.
        #
        # kills: reverting to an age-only test, which re-discards every same-day refresh.
        old = _stats(281, 76544, age_days=0)
        new = _stats(281, 85932, age_days=0)
        r = G.assess_refresh(old, new)
        self.assertTrue(r["accept"], r["reasons"])
        self.assertFalse(any("no progress" in x for x in r["reasons"]))

    def test_equal_age_and_FEWER_edges_is_never_progress(self):
        # kills: reading "gained" as "changed". A shrinking store is not progress in any
        # reading; the collapse check owns the large case, and this pins the small one so
        # a one-edge loss cannot sneak through as movement.
        old = _stats(281, 76544, age_days=0)
        new = _stats(281, 76543, age_days=0)
        r = G.assess_refresh(old, new)
        self.assertFalse(r["accept"])
        self.assertTrue(any("no progress" in x for x in r["reasons"]))

    def test_the_reject_reports_both_halves_it_judged(self):
        # kills: a message that still says only "age N >= M". The operator's next question
        # after "nothing to ship" is "did it really find nothing?", and the edge counts
        # are the answer -- withholding them is what sent this run to the job log.
        r = G.assess_refresh(_stats(281, 76544, age_days=0),
                             _stats(281, 76544, age_days=0))
        msg = [x for x in r["reasons"] if "no progress" in x][0]
        self.assertIn("76544", msg)

    def test_rejects_stale_result(self):
        # even if "fresher" than a very old store, a result past the warn window is not
        # fresh enough to ship. old very stale, new still stale.
        old = _stats(290, 23000, age_days=200)
        new = _stats(290, 23000, age_days=80)   # fresher than 200 but > 60-day warn
        r = G.assess_refresh(old, new)
        self.assertFalse(r["accept"])
        self.assertTrue(any("not fresh" in x for x in r["reasons"]))

    def test_convergence_regression_warns_but_accepts(self):
        # a fresher, healthy-size store whose coverage convergence regressed: ACCEPT
        # (freshness wins) but WARN. Mutation: turn this into a reject -> we'd keep a
        # stale store just because convergence dipped, which is worse.
        new = _stats(300, 24000, age_days=2, gating_reachable=False)
        r = G.assess_refresh(self.OLD, new)
        self.assertTrue(r["accept"])
        self.assertTrue(any("REGRESSED" in x for x in r["warnings"]))

    def test_empty_old_store_does_not_divide_by_zero(self):
        old = _stats(0, 0, age_days=None)
        new = _stats(10, 100, age_days=1)
        r = G.assess_refresh(old, new)
        self.assertTrue(r["accept"])


class TestStoreStats(unittest.TestCase):
    def test_stats_shape_and_convergence(self):
        # a tiny synthetic graph: two anchors + a vouched payee. store_stats should
        # report structure and a convergence verdict without raising. Mutation: drop
        # the empty-store guard -> convergence_verdict on 0 payees raises.
        edges = [("0xp%d" % i, "0xanchor1") for i in range(22)]
        edges += [("0xp%d" % i, "0xanchor2") for i in range(22)]
        s = G.store_stats(edges, age_days=3)
        self.assertEqual(s["age_days"], 3)
        self.assertGreaterEqual(s["payees"], 2)
        self.assertIn("gating_reachable", s)

    def test_empty_store_stats_safe(self):
        s = G.store_stats([], age_days=None)
        self.assertEqual(s["payees"], 0)
        self.assertFalse(s["gating_reachable"])


class TestGatingCapableUtility(unittest.TestCase):
    """The UTILITY metric. REGRESSION: the 2026-08-17 refresh kept 85% of payees and 87%
    of edges -- clearing MIN_RETENTION -- while payees able to EARN a GO fell 237 -> 207.
    Size retention and the boolean `gating_reachable` were both blind to it."""

    def _edges(self, spec):
        """spec: {payee: (n_settlements, n_distinct_payers)} -> edge list."""
        out = []
        for payee, (n, dp) in spec.items():
            for i in range(n):
                out.append(("0xpayer%d" % (i % dp), payee))
        return out

    def test_counts_only_payees_clearing_BOTH_gates(self):
        e = self._edges({
            "0xgood": (25, 4),     # clears both -> counts
            "0xthin": (5, 4),      # too few settlements -> excluded
            "0xsybil": (25, 2),    # too few distinct payers -> excluded
        })
        self.assertEqual(G.gating_capable(e), 1)

    def test_boundaries_are_inclusive(self):
        self.assertEqual(G.gating_capable(self._edges({"0xa": (20, 3)})), 1)
        self.assertEqual(G.gating_capable(self._edges({"0xa": (19, 3)})), 0)
        self.assertEqual(G.gating_capable(self._edges({"0xa": (20, 2)})), 0)

    def test_normalization_matches_build_index(self):
        # self-vouch edges are dropped by build_index; the count must agree or the two
        # halves of the metric would disagree about who exists.
        e = [("0xa", "0xa")] * 30 + [("0xp%d" % (i % 3), "0xb") for i in range(30)]
        self.assertEqual(G.gating_capable(e), 1)      # only 0xb

    def test_never_raises_on_junk_edges(self):
        for bad in (None, [], [None], [("a",)], [(None, None)], ["notatuple"], [(1, 2)]):
            G.gating_capable(bad)

    def test_reject_on_utility_collapse_that_size_check_MISSES(self):
        # THE REGRESSION, with the real shape: size retention passes, utility does not.
        old = {"payees": 281, "edges": 23037, "gating_capable": 237, "age_days": 13,
               "gating_reachable": True}
        new = {"payees": 240, "edges": 20141, "gating_capable": 207, "age_days": 0,
               "gating_reachable": True}
        # size-only view would have been happy: 85% and 87%, both over MIN_RETENTION
        self.assertGreater(new["payees"], old["payees"] * G.MIN_RETENTION)
        self.assertGreater(new["edges"], old["edges"] * G.MIN_RETENTION)
        r = G.assess_refresh(old, new)
        self.assertFalse(r["accept"])
        self.assertTrue(any("gating-capable" in x for x in r["reasons"]), r["reasons"])

    def test_accept_when_utility_is_retained(self):
        old = {"payees": 281, "edges": 23037, "gating_capable": 237, "age_days": 13,
               "gating_reachable": True}
        # what MERGE semantics actually produced on the real corpora: utility GREW.
        new = {"payees": 281, "edges": 32971, "gating_capable": 251, "age_days": 0,
               "gating_reachable": True}
        self.assertTrue(G.assess_refresh(old, new)["accept"])

    def test_absent_metric_does_not_block(self):
        # fail-open: a stats dict without the new key must not start rejecting refreshes.
        old = {"payees": 100, "edges": 1000, "age_days": 13, "gating_reachable": True}
        new = {"payees": 100, "edges": 1000, "age_days": 0, "gating_reachable": True}
        self.assertTrue(G.assess_refresh(old, new)["accept"])


class TestCrawlHealth(unittest.TestCase):
    """The guard's checks on the STORE are all floors computed from the candidate
    itself, so they cannot see how it was produced. These read the crawl summary.

    Each test names the mutation it kills."""

    def _stores(self):
        good = {"payees": 100, "edges": 500, "gating_capable": 50, "age_days": 10}
        fresh = {"payees": 100, "edges": 500, "gating_capable": 50, "age_days": 2}
        return good, fresh

    def test_absent_summary_asserts_nothing(self):
        # Mutation: treat a missing summary as healthy and return a warning-free
        # pass -- "we did not look" would then read identically to "we looked and
        # it was fine".
        self.assertEqual(G.crawl_health(None), {"reasons": [], "warnings": []})
        self.assertEqual(G.crawl_health({}), {"reasons": [], "warnings": []})

    def test_a_few_failed_payees_warn_but_do_not_reject(self):
        # THE STALE-CLIFF GUARD. The candidate is seeded FROM the committed store
        # (refresh_seed.sh: "MERGE, don't REPLACE"), so an errored payee keeps its
        # old rows -- it goes stale, it does not vanish. Mutation: reject on the
        # first error -> one flaky payee blocks every refresh, and the 90-day
        # stale cliff this module exists to prevent arrives on schedule.
        old, new = self._stores()
        r = G.assess_refresh(old, new, crawl={"payees": 95, "errors": 5})
        self.assertTrue(r["accept"])
        self.assertTrue(any("failed to fetch" in w for w in r["warnings"]))
        self.assertEqual(r["reasons"], [])

    def test_a_mostly_failed_crawl_is_rejected(self):
        # Mutation: warn instead of reject at any rate -> a crawl where 40% of
        # payees errored ships as a "refresh". It passes every store check: the
        # merge keeps retention at 100%, and `age_days` is read from the NEWEST
        # row, so a handful of refreshed payees carry the freshness of a corpus
        # that mostly did not move.
        old, new = self._stores()
        r = G.assess_refresh(old, new, crawl={"payees": 60, "errors": 40})
        self.assertFalse(r["accept"])
        self.assertTrue(any("40 of 100" in x for x in r["reasons"]))

    def test_the_error_rate_is_over_ATTEMPTED_not_over_successes(self):
        # `payees` counts the ones that SUCCEEDED, so the denominator is
        # payees+errors. Mutation: divide by `payees` -> 40 errors against 60
        # successes reads as 67% instead of 40%, and the threshold fires at the
        # wrong place in both directions.
        h = G.crawl_health({"payees": 60, "errors": 40})
        self.assertTrue(any("40 of 100 payees" in x for x in h["reasons"]))

    def test_threshold_boundary_accepts_at_the_limit(self):
        # Exactly at MAX_CRAWL_ERROR_RATE is not "past" it. Mutation: >= instead
        # of > -> the documented threshold is off by one case.
        h = G.crawl_health({"payees": 75, "errors": 25}, max_error_rate=0.25)
        self.assertEqual(h["reasons"], [])
        self.assertTrue(h["warnings"])

    def test_truncation_warns_and_never_rejects(self):
        # Truncation is the STEADY STATE at the page cap -- every high-volume
        # payee is truncated on every run. Mutation: reject on truncated > 0 ->
        # no refresh ever ships again.
        old, new = self._stores()
        r = G.assess_refresh(old, new, crawl={"payees": 100, "errors": 0, "truncated": 70})
        self.assertTrue(r["accept"])
        self.assertTrue(any("page cap" in w for w in r["warnings"]))
        self.assertEqual(r["reasons"], [])

    def test_truncated_finally_has_a_reader(self):
        # `truncated` shipped with no consumer anywhere in the repo -- the
        # wired-and-inert pattern, which no mutation test can catch because
        # deleting an unread field breaks nothing. This IS the consumer.
        # Mutation: drop the truncated branch -> the field is inert again.
        self.assertTrue(G.crawl_health({"payees": 1, "truncated": 3})["warnings"])
        self.assertFalse(G.crawl_health({"payees": 1, "truncated": 0})["warnings"])

    def test_store_checks_still_run_with_a_crawl_supplied(self):
        # Mutation: return early on a healthy crawl -> the retention and
        # freshness checks stop running whenever a summary is passed.
        old = {"payees": 100, "edges": 500, "gating_capable": 50, "age_days": 10}
        collapsed = {"payees": 10, "edges": 50, "gating_capable": 5, "age_days": 2}
        r = G.assess_refresh(old, collapsed, crawl={"payees": 100, "errors": 0})
        self.assertFalse(r["accept"])
        self.assertTrue(any("collapsed" in x for x in r["reasons"]))


class TestCrawlHealthNeverRaises(unittest.TestCase):
    """AUDIT REGRESSIONS. `crawl_health` read summary fields with `or 0` and fed
    them straight to arithmetic, so a corrupt or hand-edited JSON raised.

    A guard exists to fail SAFE. Dying instead of deciding is worse than any
    verdict it could return -- and under refresh_seed.sh's `set -eu` the crash
    also aborted a refresh the guard had already accepted. `gating_capable` in
    this same module documents the standard: NEVER raises."""

    HOSTILE = [
        ("string counts", {"payees": "270", "errors": "11", "truncated": "70"}),
        ("a list, not a dict", [1, 2, 3]),
        ("nested dicts as counts", {"payees": {"a": 1}, "errors": 2}),
        ("negative counts", {"payees": 100, "errors": -5, "truncated": -1}),
        ("floats", {"payees": 270.0, "errors": 11.0, "truncated": 2.5}),
        ("None counts", {"payees": None, "errors": None, "truncated": None}),
        ("a string, not a dict", "not a summary"),
        ("huge", {"payees": 10 ** 9, "errors": 10 ** 9, "truncated": 10 ** 9}),
    ]

    def test_never_raises_on_any_malformed_summary(self):
        for label, crawl in self.HOSTILE:
            with self.subTest(label):
                out = G.crawl_health(crawl)
                self.assertIsInstance(out["reasons"], list)
                self.assertIsInstance(out["warnings"], list)

    def test_assess_refresh_never_raises_either(self):
        old = {"payees": 100, "edges": 500, "gating_capable": 50, "age_days": 10}
        new = {"payees": 100, "edges": 500, "gating_capable": 50, "age_days": 2}
        for label, crawl in self.HOSTILE:
            with self.subTest(label):
                self.assertIn("accept", G.assess_refresh(old, new, crawl=crawl))

    def test_unusable_counts_are_treated_as_zero_not_trusted(self):
        # Mutation: coerce junk to a large number -> a corrupt summary could
        # manufacture a passing error rate.
        self.assertEqual(G.crawl_health({"payees": {"a": 1}, "errors": 0}),
                         {"reasons": [], "warnings": []})

    def test_junk_in_the_ERROR_field_cannot_manufacture_a_REJECT(self):
        # Sharper than the case above, which put junk in `payees` where a wrong
        # value is inert. Junk in `errors` drives the threshold directly.
        # Mutation: return a large number for unparseable input -> a corrupt or
        # truncated summary REJECTS a perfectly good refresh, and the corpus
        # walks toward the stale cliff because a JSON file was malformed.
        out = G.crawl_health({"payees": 100, "errors": {"x": 1}})
        self.assertEqual(out["reasons"], [])
        self.assertEqual(out["warnings"], [])

    def test_a_negative_error_count_is_zero_not_a_negative_rate(self):
        # A count below zero is corrupt, not informative. Mutation: pass it
        # through -> `errors` is truthy, the rate goes NEGATIVE, and the guard
        # emits a warning reading "-5 of 95 payees failed to fetch" -- a number
        # that cannot exist, presented as a measurement.
        self.assertEqual(G.crawl_health({"payees": 100, "errors": -5}),
                         {"reasons": [], "warnings": []})
        self.assertEqual(G.crawl_health({"payees": 100, "truncated": -3})["warnings"], [])


class TestRefreshScriptKeepsProvenanceNonFatal(unittest.TestCase):
    """AUDIT REGRESSION (the expensive one). scripts/refresh_seed.sh runs
    `set -eu`. Provenance generation was added as a bare command BEFORE the
    promotion `mv`, so a crash there discarded a refresh the guard had ACCEPTED
    -- safety work disabling the safety mechanism, which is the same shape as the
    non-zero-exit regression in chain_backfill.

    Asserted on the script text because the failure is a shell control-flow
    property, invisible to any Python-level test."""

    def _script(self):
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "scripts", "refresh_seed.sh")
        with open(path) as fh:
            return fh.read()

    def test_the_script_still_aborts_on_error_by_default(self):
        # If this ever stops being true the test below is measuring nothing.
        self.assertIn("set -eu", self._script())

    def test_provenance_failure_cannot_block_promotion(self):
        text = self._script()
        # The call must be INSIDE a conditional, never a bare statement whose
        # failure trips `set -e`.
        self.assertIn("if python3 seed_provenance.py", text)
        # And the promotion must still happen after it.
        prov = text.index("seed_provenance.py")
        promote = text.index('mv "$TMP_GZ"  data/reputation_seed.db.gz')
        self.assertLess(prov, promote, "provenance must be attempted before the mv")

    def test_a_failed_run_removes_the_stale_record(self):
        # A provenance file describing the PREVIOUS store, sitting beside the new
        # one, is confidently wrong -- worse than absent. Mutation: leave it.
        self.assertIn("rm -f data/reputation_seed.json", self._script())


if __name__ == "__main__":
    unittest.main()

class TestTheWorkflowPreservesTheRejectDiagnostic(unittest.TestCase):
    """A reject files an issue; the issue has to carry the reason.

    Run #9 (2026-09-28) rejected and filed issue #61 saying "which one, and why, is in
    the job log". The provenance record holds the crawl aggregates but seed_provenance.py
    runs ONLY on the accept path, so the run that most needed explaining produced the
    least explanation -- and job logs expire while the issue outlives them.
    """

    def _wf(self):
        with open(".github/workflows/seed-refresh.yml") as fh:
            return fh.read()

    def test_tee_really_does_mask_the_exit_code(self):
        # BEHAVIOURAL, and the reason the next test matters. Piping into `tee` makes the
        # pipeline's status `tee`'s, so a plain `$?` reads 0 on a REJECT and the workflow
        # would take the accept path with nothing to promote. Proven, not asserted from
        # memory -- this is the trap the transcript change walks straight into.
        import subprocess
        masked = subprocess.run(
            ["bash", "-c", 'set +e; (exit 1) | tee /dev/null >/dev/null; echo $?'],
            capture_output=True, text=True).stdout.strip()
        honest = subprocess.run(
            ["bash", "-c", 'set +e; (exit 1) | tee /dev/null >/dev/null; echo ${PIPESTATUS[0]}'],
            capture_output=True, text=True).stdout.strip()
        self.assertEqual(masked, "0")    # tee's status -- the reject vanishes
        self.assertEqual(honest, "1")    # the script's own status

    def test_the_refresh_step_reads_PIPESTATUS_not_dollar_question(self):
        # kills: teeing the transcript and keeping `code=$?`, which the test above shows
        # would report every reject as a success and send the job to the PR path.
        wf = self._wf()
        self.assertIn("PIPESTATUS[0]", wf)
        self.assertNotIn('echo "code=$?"', wf)

    def test_the_reject_nag_quotes_the_guard_verdicts(self):
        # kills: reverting the nag to a bare pointer at the job log. The issue is the
        # artifact that survives; it has to contain the verdicts, not a link to them.
        wf = self._wf()
        self.assertIn("refresh.log", wf)
        self.assertIn("What the guards actually said", wf)

    # The real transcript shape, copied from run #9 (2026-09-28), which is the run this
    # whole change exists because of.
    RUN_9 = """refresh_seed: running the refresh guard (store: candidate vs committed) ...
old: {'payees': 281, 'edges': 76544, 'age_days': 0, 'gating_reachable': True}
new: {'payees': 281, 'edges': 85932, 'age_days': 0, 'gating_reachable': True}
WARN: 201 payee(s) hit the page cap -- their history is a recent WINDOW
REJECT -- keep the current store:
  - no progress: new store age 0 d >= current 0 d -- nothing to ship
Seeded 292 payee(s): 45517 transfers, 9388 new settlements.
refresh_seed: REJECTED the candidate (see reasons above):
refresh_seed:   - refresh_guard rejected the STORE
"""

    def test_the_extraction_actually_selects_the_verdict_lines(self):
        # kills: a heading over an EMPTY block -- neutering the grep to `true` leaves the
        # nag saying "What the guards actually said:" and then saying nothing, which
        # passes every structural check while carrying exactly as much information as the
        # pointer-at-the-job-log it replaced. Found by mutation, not by reading.
        #
        # Runs the WORKFLOW'S OWN pattern against the real log shape, rather than a copy
        # of it that could drift.
        import re, subprocess, tempfile, os
        wf = self._wf()
        m = re.search(r'grep -E "([^"]+)"', wf)
        self.assertIsNotNone(m, "the nag no longer extracts anything")
        pattern = m.group(1)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "refresh.log")
            with open(path, "w") as fh:
                fh.write(self.RUN_9)
            out = subprocess.run(["grep", "-E", pattern, path],
                                 capture_output=True, text=True).stdout
        # the verdict, the reason, and the numbers that justify re-running.
        #
        # Assert the VERDICT LINE, not the substring: a mutation narrowing the pattern to
        # ^(WARN|ACCEPT) still emitted "refresh_seed:   - refresh_guard rejected the
        # STORE" via the other alternation, and that line CONTAINS "REJECT" -- so a
        # substring check passed while the guard's own verdict had been dropped.
        lines = out.splitlines()
        self.assertTrue(any(l.startswith("REJECT") for l in lines),
                        "the guard's verdict line itself was not extracted: %r" % lines)
        self.assertIn("no progress", out)
        self.assertIn("85932", out)          # the candidate's edge count, from `new:`
        self.assertIn("refresh_guard rejected the STORE", out)
        # WARNINGS belong in the issue too. A reject is read alongside them -- "201
        # payees hit the page cap" is the context that tells an operator whether to
        # re-run or to go look at the data source.
        self.assertTrue(any(l.startswith("WARN") for l in lines),
                        "warnings were dropped from the transcript: %r" % lines)

    def test_the_transcript_is_captured_before_the_nag_needs_it(self):
        # kills: writing the transcript somewhere the nag step cannot read, or capturing
        # it after the branch that consumes it.
        wf = self._wf()
        self.assertLess(wf.index('tee "$RUNNER_TEMP/refresh.log"'),
                        wf.index("What the guards actually said"))


class TestTheLockfileFallbackIsNotSilent(unittest.TestCase):
    """`npm ci || npm install` passes CI while the lockfile has drifted."""

    def test_both_integration_steps_announce_a_failed_npm_ci(self):
        # kills: restoring the bare `npm ci || npm install`. The fallback itself is fine
        # -- a drifted lockfile should not take CI down -- but silence is not: the suite
        # then runs against RESOLVED versions while the check reports success for a thing
        # it did not verify. Same fail-quiet shape as a gate that stops gating.
        with open(".github/workflows/tests.yml") as fh:
            wf = fh.read()
        self.assertEqual(wf.count("::warning::npm ci FAILED here"), 2,
                         "both the openclaw and lucid steps must warn")
        self.assertNotIn("npm ci --no-audit --no-fund || npm install", wf)
