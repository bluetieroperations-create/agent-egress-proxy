"""Tests for reachability_ledger -- what happened the OTHER times we looked.

Each test names the MUTATION it kills. The property that matters most here is
the one that keeps our own failures out of a seller's record: a proxy that hangs
and an endpoint that is down produce the same silence, and only one of them is
the seller's fault.
"""

import json
import os
import tempfile
import unittest

import reachability_ledger as RL
import seller_report as SR

HOST = "apiwitchcraft.duckdns.org"
DAY = 86400.0


class TestClassify(unittest.TestCase):
    def test_a_response_of_any_status_is_answered(self):
        # Mutation: treating a non-2xx as a failure. A 402 IS the success case
        # here -- it is the payment challenge we came to read.
        for status in (200, 402, 404, 500):
            self.assertEqual(RL.classify({"status": status})[0], RL.ANSWERED)

    def test_our_own_refusal_is_skipped_not_unreachable(self):
        # Mutation: classing it unreachable. THE central property. The SSRF guard
        # declining a URL is a fact about US; recording it against the seller
        # would let an over-strict guard build a case that a healthy endpoint was
        # down six times.
        outcome, _ = RL.classify({"error": "not probed: scheme file is not http(s)"})
        self.assertEqual(outcome, RL.SKIPPED)

    def test_a_timeout_is_unreachable_without_blaming_anyone(self):
        outcome, detail = RL.classify({"error": "URLError: timed out"})
        self.assertEqual(outcome, RL.UNREACHABLE)
        self.assertIn("timed out", detail)

    def test_a_missing_probe_is_skipped(self):
        # Mutation: recording None as unreachable. An offline report would then
        # accumulate silences for hosts nobody tried to contact.
        self.assertEqual(RL.classify(None)[0], RL.SKIPPED)
        self.assertEqual(RL.classify({})[0], RL.SKIPPED)

    def test_third_party_error_text_is_escaped(self):
        # Mutation: storing it raw. The string comes from a stranger's server and
        # is rendered back into a report; this is the same echo class the repo
        # has now hit six times.
        _, detail = RL.classify({"error": "boom\nhost: injected"})
        self.assertNotIn("\n", detail)


class TestSummarize(unittest.TestCase):
    def _events(self, spec, now=1_000_000.0):
        return [{"host": HOST, "outcome": o, "ts": now - d * DAY, "detail": ""}
                for d, o in spec]

    def test_our_skips_never_count_toward_a_silent_run(self):
        # Mutation: including SKIPPED in the run. This is the failure that would
        # manufacture evidence against an innocent seller -- six of our own
        # proxy failures reading as six outages of theirs.
        events = self._events([(9, RL.ANSWERED), (2, RL.SKIPPED),
                               (1, RL.SKIPPED), (0, RL.SKIPPED)])
        summary = RL.summarize(events, now=1_000_000.0)
        self.assertEqual(summary["consecutive_silent"], 0)
        self.assertEqual(summary["state"], "answering")
        self.assertEqual(summary["skipped"], 3)
        # The COUNTS matter as much as the run: "we reached it on 1 of 1
        # attempts" is true, "1 of 4" implies three failures that were ours.
        # A run-only assertion misses that, because a skip is not UNREACHABLE
        # and so ends the backward walk either way.
        self.assertEqual(summary["attempts"], 1)
        self.assertEqual(summary["answered"], 1)
        self.assertEqual(summary["unreachable"], 0)
        self.assertIn("of 1", RL.describe(summary))

    def test_a_recent_silence_after_success_is_flapping(self):
        # Mutation: calling it dead. THE case this module was built for: the same
        # host told four sessions four different stories, and each one picked
        # "live" or "dead" from a single look. Intermittent is its own answer.
        events = self._events([(19, RL.ANSWERED), (1, RL.ANSWERED),
                               (0, RL.UNREACHABLE)])
        self.assertEqual(RL.summarize(events, now=1_000_000.0)["state"], "flapping")

    def test_a_long_spread_run_is_a_silent_run(self):
        events = self._events([(30, RL.ANSWERED), (9, RL.UNREACHABLE),
                               (5, RL.UNREACHABLE), (1, RL.UNREACHABLE)])
        summary = RL.summarize(events, now=1_000_000.0)
        self.assertEqual(summary["state"], "silent_run")
        self.assertGreater(summary["silent_days"], 3.0)

    def test_one_bad_afternoon_is_not_a_silent_run(self):
        # Mutation: dropping the DAYS_FOR_CONCERN half of the rule. Three
        # timeouts in ten minutes is a network blip; calling that a silent run
        # would report a transient as a condition.
        events = self._events([(10, RL.ANSWERED), (0.02, RL.UNREACHABLE),
                               (0.01, RL.UNREACHABLE), (0, RL.UNREACHABLE)])
        self.assertEqual(RL.summarize(events, now=1_000_000.0)["state"], "flapping")

    def test_a_few_silences_over_a_long_time_is_not_yet_a_run(self):
        # Mutation: dropping the RUN_FOR_CONCERN half. One timeout a fortnight
        # ago is not a pattern.
        events = self._events([(30, RL.ANSWERED), (10, RL.UNREACHABLE)])
        self.assertEqual(RL.summarize(events, now=1_000_000.0)["state"], "flapping")

    def test_never_answered_is_distinct_from_flapping(self):
        # Mutation: collapsing them. "It has never worked for us" and "it worked
        # and stopped" call for different words to a seller.
        events = self._events([(9, RL.UNREACHABLE), (5, RL.UNREACHABLE),
                               (1, RL.UNREACHABLE)])
        self.assertEqual(RL.summarize(events, now=1_000_000.0)["state"],
                         "never_answered")

    def test_no_attempts_is_unobserved_not_broken(self):
        summary = RL.summarize([], now=1_000_000.0)
        self.assertEqual(summary["state"], "unobserved")
        self.assertEqual(summary["attempts"], 0)

    def test_a_success_ends_the_run(self):
        events = self._events([(9, RL.UNREACHABLE), (5, RL.UNREACHABLE),
                               (0, RL.ANSWERED)])
        self.assertEqual(RL.summarize(events, now=1_000_000.0)["state"], "answering")


class TestDescribe(unittest.TestCase):
    # Both directions: claiming a seller is UP is the same error as claiming
    # they are down. We observe our own attempts and nothing else.
    FORBIDDEN = ("is down", "was down", "offline", "your server is",
                 "uptime", "outage", "is up", "your endpoint is",
                 "endpoint is up", "is healthy", "is working")

    def test_no_wording_claims_the_sellers_uptime(self):
        # Mutation: writing "your endpoint was down". We measure OUR
        # observations: today's six timeouts came through a proxy while a direct
        # TLS handshake to the same host succeeded, and from here those are
        # indistinguishable. Claiming uptime would be asserting what we cannot see.
        for spec in ([], [(9, RL.ANSWERED)], [(9, RL.ANSWERED), (0, RL.UNREACHABLE)],
                     [(30, RL.ANSWERED), (9, RL.UNREACHABLE), (5, RL.UNREACHABLE),
                      (1, RL.UNREACHABLE)], [(1, RL.UNREACHABLE)]):
            events = [{"host": HOST, "outcome": o, "ts": 1_000_000.0 - d * DAY}
                      for d, o in spec]
            text = RL.describe(RL.summarize(events, now=1_000_000.0)).lower()
            for bad in self.FORBIDDEN:
                self.assertNotIn(bad, text, text)

    def test_it_says_how_many_times_we_looked(self):
        # Mutation: describing only the latest state. "We could not reach you"
        # and "we reached you twice in nine attempts, most recently 12 days ago"
        # are different claims, and only the second is worth acting on.
        events = [{"host": HOST, "outcome": o, "ts": 1_000_000.0 - d * DAY}
                  for d, o in [(9, RL.ANSWERED), (0, RL.UNREACHABLE)]]
        self.assertIn("of 2", RL.describe(RL.summarize(events, now=1_000_000.0)))


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "sub", "reach.jsonl")

    def test_a_round_trip_preserves_the_observation(self):
        self.assertTrue(RL.record(HOST, RL.ANSWERED, "http 402", path=self.path,
                                  now=1000.0))
        events = RL.load(self.path, HOST)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], RL.ANSWERED)

    def test_events_come_back_oldest_first(self):
        # Mutation: trusting file order. The run detection walks from the END, so
        # unsorted events would compute a run over the wrong tail.
        RL.record(HOST, RL.ANSWERED, path=self.path, now=500.0)
        RL.record(HOST, RL.UNREACHABLE, path=self.path, now=100.0)
        timestamps = [e["ts"] for e in RL.load(self.path, HOST)]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_a_corrupt_line_is_skipped_not_raised(self):
        # Mutation: letting the parse error escape. This file is appended from
        # several processes; one torn write must not blind the reader to the
        # other rows.
        RL.record(HOST, RL.ANSWERED, path=self.path, now=1.0)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("{not json\n\n")
        RL.record(HOST, RL.UNREACHABLE, path=self.path, now=2.0)
        self.assertEqual(len(RL.load(self.path, HOST)), 2)

    def test_hosts_do_not_bleed_into_each_other(self):
        RL.record(HOST, RL.ANSWERED, path=self.path, now=1.0)
        RL.record("other.example", RL.UNREACHABLE, path=self.path, now=2.0)
        self.assertEqual(len(RL.load(self.path, HOST)), 1)

    def test_the_host_key_is_case_insensitive(self):
        # Mutation: exact match. The same join that missed 64 of 69 endpoints in
        # advertised_prices; here it would silently split one host's history in
        # two and reset its run.
        RL.record(HOST.upper(), RL.ANSWERED, path=self.path, now=1.0)
        self.assertEqual(len(RL.load(self.path, HOST)), 1)
        # Asserted on the STORED row, not only on the query: `load` normalizes
        # both sides, so a reader-only fix passes this while the FILE -- the
        # durable record other tools group by -- still holds two spellings of
        # one host.
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.loads(fh.readline())["host"], HOST.lower())

    def test_an_unwritable_path_fails_soft(self):
        # Mutation: raising. Logging must never break a report -- the same rule
        # rwa_ledger follows.
        self.assertFalse(RL.record(HOST, RL.ANSWERED, path="/proc/nope/x.jsonl"))

    def test_a_missing_file_reads_as_no_history(self):
        self.assertEqual(RL.load(os.path.join(self.dir, "absent.jsonl")), [])

    def test_an_unknown_outcome_is_refused(self):
        # Mutation: accepting anything. A typo'd outcome would silently vanish
        # from every count while looking recorded.
        self.assertFalse(RL.record(HOST, "probably-fine", path=self.path))


class TestBounds(unittest.TestCase):
    """The portal is public, so this file grows from strangers' traffic."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "reach.jsonl")
        self.saved = (RL.COMPACT_ABOVE_BYTES, RL.KEEP_PER_HOST)
        RL.COMPACT_ABOVE_BYTES, RL.KEEP_PER_HOST = 100_000, 40

    def tearDown(self):
        RL.COMPACT_ABOVE_BYTES, RL.KEEP_PER_HOST = self.saved

    def test_the_file_does_not_grow_without_bound(self):
        # Mutation: removing the compaction. 514 probeable corpus hosts against a
        # 15-minute report cache is ~49k rows/day, about 16 MB, from public
        # traffic alone -- and `load` scans the whole file per report, so an
        # uncapped ledger degrades the thing it exists to serve.
        for i in range(3000):
            RL.record("h%d" % (i % 10), RL.ANSWERED, "x" * 150,
                      path=self.path, now=float(i))
        # The FILE bound is the guarantee. Compaction is size-triggered, so rows
        # accumulate normally between crossings and a per-host count asserted at
        # an arbitrary instant sits above KEEP_PER_HOST -- which is the contract
        # working, not failing. Unbounded would be ~750 KB here and climbing
        # forever; the point is that it stops.
        self.assertLess(os.path.getsize(self.path), 400_000)
        self.assertLess(len(RL.load(self.path, "h1")), 300)

    def test_compaction_keeps_the_MOST_RECENT_rows(self):
        # Mutation: keeping the oldest (`host_events[:keep]`). Asserting only the
        # MAXIMUM timestamp does not catch it -- the newest row survives either
        # way, because it is appended after the last compaction. The OLDEST
        # retained row is what distinguishes them: keeping the head leaves ts=0
        # sitting in the file forever while the recent tail is discarded, which
        # bounds the file and destroys the entire signal.
        for i in range(3000):
            RL.record("h1", RL.ANSWERED, "x" * 150, path=self.path, now=float(i))
        RL.COMPACT_ABOVE_BYTES = 0
        RL.compact(self.path)
        stamps = [e["ts"] for e in RL.load(self.path, "h1")]
        self.assertEqual(max(stamps), 2999.0)
        self.assertGreater(min(stamps), 2000.0)

    def test_a_busy_host_cannot_evict_a_quiet_one(self):
        # Mutation: capping globally instead of per host. Round-robin traffic
        # does NOT catch that -- a global cap still leaves every host a few of
        # the newest rows. The real failure needs the asymmetry it describes: one
        # host observed constantly, another observed a few times long ago. Under
        # a global cap the quiet host's history is evicted entirely, which is
        # exactly the seller whose report then says "we have no record of you".
        RL.record("quiet.example", RL.ANSWERED, "x", path=self.path, now=1.0)
        RL.record("quiet.example", RL.UNREACHABLE, "x", path=self.path, now=2.0)
        for i in range(3000):
            RL.record("busy.example", RL.ANSWERED, "x" * 150,
                      path=self.path, now=100.0 + i)
        RL.COMPACT_ABOVE_BYTES = 0
        RL.compact(self.path)
        self.assertEqual(len(RL.load(self.path, "quiet.example")), 2)
        self.assertLessEqual(len(RL.load(self.path, "busy.example")),
                             RL.KEEP_PER_HOST)

    def test_the_retention_knob_is_read_at_call_time(self):
        # Mutation: `def compact(path=None, keep=KEEP_PER_HOST)`. A default
        # argument captures the constant when the function is DEFINED, so
        # retuning the module constant leaves the documented knob inert -- and
        # it fails silently, which is how it got shipped that way for a minute.
        RL.KEEP_PER_HOST = 5
        for i in range(3000):
            RL.record("h1", RL.ANSWERED, "x" * 150, path=self.path, now=float(i))
        # Asserted right after an EXPLICIT compaction, which is the only moment
        # the floor is exactly the floor. With the default-argument capture the
        # constant is ignored entirely and this stays at 200.
        RL.COMPACT_ABOVE_BYTES = 0
        RL.compact(self.path)
        self.assertLessEqual(len(RL.load(self.path, "h1")), 5)

    def test_a_summary_survives_compaction(self):
        # Restraint control: bounding the file must not change the answer.
        for i in range(3000):
            RL.record("h1", RL.ANSWERED, "x", path=self.path, now=float(i))
        RL.record("h1", RL.UNREACHABLE, "x", path=self.path, now=3001.0)
        summary = RL.summarize(RL.load(self.path, "h1"), now=3002.0)
        self.assertEqual(summary["state"], "flapping")


class TestDeployPath(unittest.TestCase):
    def test_the_path_is_configurable_for_a_persistent_disk(self):
        # Mutation: hardcoding the path beside the module. The root .gitignore
        # excludes *.jsonl, so the ledger is never committed -- which is correct
        # for a growing log and means a container boots with no memory. The
        # memory IS the feature, so a deploy must be able to point it at the
        # disk that survives a restart.
        import importlib
        import os
        saved = os.environ.get("BLACKWALL_REACHABILITY")
        os.environ["BLACKWALL_REACHABILITY"] = "/data/reach.jsonl"
        try:
            reloaded = importlib.reload(RL)
            self.assertEqual(reloaded.DEFAULT_PATH, "/data/reach.jsonl")
        finally:
            if saved is None:
                os.environ.pop("BLACKWALL_REACHABILITY", None)
            else:
                os.environ["BLACKWALL_REACHABILITY"] = saved
            importlib.reload(RL)


class TestProbedHostAttribution(unittest.TestCase):
    """58 of 266 corpus payees advertise more than one host."""

    ROW = {"payee": "0x" + "11" * 20, "settlement_count": 5,
           "min_price": "0.01", "max_price": "0.02",
           "resources": ["https://first.example/a", "https://second.example/b"]}

    def _record_via_report(self, probe, path):
        saved = RL.DEFAULT_PATH
        RL.DEFAULT_PATH = path
        try:
            SR.build_report("first.example", [dict(self.ROW)],
                            probe_fn=lambda r: probe)
        finally:
            RL.DEFAULT_PATH = saved
        return RL.load(path)

    def test_the_observation_is_recorded_against_the_host_we_PROBED(self):
        # Mutation: recording against hosts[0]. `probe_resources` returns the
        # first ANSWERING resource, so on 24 corpus payees it lands on a
        # different host than the first one listed -- and recording that against
        # hosts[0] writes false evidence in BOTH directions: a silent host
        # credited with a sibling's success, and a host nobody tried charged
        # with a failure. The exact cross-attribution this ledger exists to stop.
        path = os.path.join(tempfile.mkdtemp(), "r.jsonl")
        events = self._record_via_report(
            {"url": "https://second.example/b", "status": 402}, path)
        self.assertEqual([e["host"] for e in events], ["second.example"])

    def test_it_falls_back_to_the_listed_host_when_nothing_was_probed(self):
        path = os.path.join(tempfile.mkdtemp(), "r.jsonl")
        saved = RL.DEFAULT_PATH
        RL.DEFAULT_PATH = path
        try:
            SR.build_report("first.example", [dict(self.ROW)], probe_fn=None)
        finally:
            RL.DEFAULT_PATH = saved
        self.assertEqual(RL.load(path), [])

    def test_the_source_says_which_surface_observed_it(self):
        # Mutation: hardcoding the source. A record that cannot distinguish a
        # public portal visit from an operator's CLI run is much harder to read
        # back -- and the portal is the one that fires from strangers' traffic.
        path = os.path.join(tempfile.mkdtemp(), "r.jsonl")
        saved = RL.DEFAULT_PATH
        RL.DEFAULT_PATH = path
        try:
            SR.build_report("first.example", [dict(self.ROW)],
                            probe_fn=lambda r: {"url": "https://first.example/a",
                                                "status": 402},
                            source="seller_portal")
        finally:
            RL.DEFAULT_PATH = saved
        self.assertEqual(RL.load(path)[0]["source"], "seller_portal")


class TestReportIntegration(unittest.TestCase):
    def _summary(self, spec, now=1_000_000.0):
        return RL.summarize(
            [{"host": HOST, "outcome": o, "ts": now - d * DAY} for d, o in spec],
            now=now)

    def test_history_never_turns_reachability_into_a_defect(self):
        # Mutation: grading a silent run as a blocker. Rule 2 does not weaken
        # with repetition -- more observations make the STATEMENT stronger, not
        # the accusation. We still cannot tell their outage from our own.
        history = self._summary([(30, RL.ANSWERED), (9, RL.UNREACHABLE),
                                 (5, RL.UNREACHABLE), (1, RL.UNREACHABLE)])
        row = SR.assess_reach({"url": "https://h/x", "error": "timeout"}, history)
        self.assertEqual(row["severity"], SR.UNKNOWN)

    def test_a_long_silence_reads_differently_from_one_timeout(self):
        # Mutation: ignoring history in the finding. This is the whole point --
        # one timeout and a three-week silence said exactly the same thing
        # before, which is why the same host produced four contradictory reports.
        once = SR.assess_reach({"url": "https://h/x", "error": "timeout"},
                               self._summary([(0, RL.UNREACHABLE)]))
        run = SR.assess_reach({"url": "https://h/x", "error": "timeout"},
                              self._summary([(30, RL.ANSWERED), (9, RL.UNREACHABLE),
                                             (5, RL.UNREACHABLE), (1, RL.UNREACHABLE)]))
        self.assertNotEqual(once["title"], run["title"])
        self.assertEqual(run["history"], "silent_run")

    def test_flapping_is_surfaced_even_when_the_probe_succeeds(self):
        # Mutation: reporting only the current probe. A host that answers now and
        # failed the last three times is worth telling its owner about.
        row = SR.assess_reach({"url": "https://h/x", "status": 402},
                              self._summary([(9, RL.ANSWERED), (5, RL.UNREACHABLE),
                                             (1, RL.UNREACHABLE), (0, RL.ANSWERED)]))
        self.assertEqual(row["severity"], SR.INFO)

    def test_no_history_leaves_the_original_wording_intact(self):
        # Restraint control: the common case must not gain noise.
        row = SR.assess_reach({"url": "https://h/x", "status": 402}, None)
        self.assertEqual(row["title"], "Your endpoint answered")

    def test_a_report_still_builds_when_the_ledger_is_unusable(self):
        # Mutation: letting a ledger failure escape build_report. A read-only
        # deploy would then serve 500s instead of reports.
        # A bad PATH is not enough: `record` and `load` already swallow their
        # own errors, so the guard in build_report is never reached that way.
        # Making the ledger itself raise is what exercises it -- and what a
        # future change to this module could reintroduce.
        saved = RL.observe

        def boom(*a, **kw):
            raise RuntimeError("ledger exploded")

        RL.observe = boom
        try:
            report = SR.build_report("api.example.com", [
                {"payee": "0x" + "11" * 20, "resources": ["https://api.example.com/a"],
                 "settlement_count": 5, "min_price": "0.01", "max_price": "0.02"}],
                probe_fn=lambda r: {"url": r[0], "status": 402})
            self.assertTrue(report["found"])
        finally:
            RL.observe = saved


if __name__ == "__main__":
    unittest.main()
