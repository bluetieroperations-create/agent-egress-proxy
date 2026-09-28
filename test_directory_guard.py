"""
test_directory_guard.py -- guards the automated DIRECTORY-refresh release gate.

The sibling of test_refresh_guard.py. The point is the same: a bad refresh must NEVER
ship. Each test states the mutation it kills.

Why this gate needs its own tests rather than leaning on the seed's: the directory's
failure modes are not the store's. The store is guarded on payees and edges; the
directory is guarded on payTo-index HOSTS, because an absent host reads as `unknown` and
the gate silently stops covering the endpoint. And unlike the store, the directory can
fail by being UNDATED -- promoting a corpus with no verifiable sidecar swaps a working
gate for an inert one, which no size check can see.
"""
import json
import os
import tempfile
import unittest

import directory_guard as G
import payto_baseline as PB


def _stats(entries, hosts, *, age_days, priced=None, sanctioned=1, dated=True):
    """A stats dict shaped like directory_stats() output, built directly so the
    retention arithmetic is tested without hand-rolling 266 fake records."""
    return {"entries": entries, "hosts": hosts,
            "priced": entries if priced is None else priced,
            "sanctioned": sanctioned, "age_days": age_days, "dated": dated}


def _record(payee, hosts, min_price="0.01", sanctioned=False):
    return {"payee": payee, "min_price": min_price, "max_price": "1.00",
            "sanctioned": sanctioned, "distinct_payers": 7,
            "resources": ["https://%s/v1/quote" % h for h in hosts]}


class TestAssessDirectoryRefresh(unittest.TestCase):
    # a healthy refresh: similar size, same hosts, clearly fresher and reachable.
    OLD = _stats(266, 514, age_days=20.0)

    def test_accepts_healthy_refresh(self):
        r = G.assess_directory_refresh(self.OLD, _stats(270, 520, age_days=0.1))
        self.assertTrue(r["accept"], r["reasons"])
        self.assertFalse(r["reasons"])

    def test_rejects_empty_candidate(self):
        # Mutation: drop the emptiness check -> a crawl that produced nothing ships as
        # valid JSON, the payTo index empties, and every host reads `unknown`.
        r = G.assess_directory_refresh(self.OLD, _stats(0, 0, age_days=0.1))
        self.assertFalse(r["accept"])
        self.assertTrue(any("EMPTY" in x for x in r["reasons"]))

    def test_rejects_entry_collapse(self):
        # partial Bazaar crawl: 266 -> 12 entries.
        r = G.assess_directory_refresh(self.OLD, _stats(12, 20, age_days=0.1))
        self.assertFalse(r["accept"])
        self.assertTrue(any("entry count collapsed" in x for x in r["reasons"]))

    def test_rejects_host_loss_behind_a_healthy_entry_count(self):
        # THE BLIND SPOT, and the reason MIN_HOST_RETENTION is tighter than
        # MIN_RETENTION. 260 of 266 entries retained (97.7%, sails past 80%) while the
        # multi-host payees are gone: 514 -> 400 hosts. Mutation: drop the host check ->
        # 114 endpoint hosts the gate used to cover start reading as `unknown`, and
        # nothing in the entry count says so.
        r = G.assess_directory_refresh(self.OLD, _stats(260, 400, age_days=0.1))
        self.assertFalse(r["accept"])
        self.assertTrue(any("hosts collapsed" in x for x in r["reasons"]))

    def test_rejects_undated_candidate(self):
        # Mutation: drop the dated check -> a candidate with no verifiable sidecar
        # replaces a dated corpus. payto_baseline reads it as stale and refuses to gate,
        # so the refresh makes the gate WORSE while reporting success.
        r = G.assess_directory_refresh(
            self.OLD, _stats(270, 520, age_days=None, dated=False))
        self.assertFalse(r["accept"])
        self.assertTrue(any("UNDATED" in x for x in r["reasons"]))

    def test_rejects_no_progress(self):
        # same age as committed: the commit/redeploy churn buys nothing.
        r = G.assess_directory_refresh(self.OLD, _stats(266, 514, age_days=20.0))
        self.assertFalse(r["accept"])
        self.assertTrue(any("no progress" in x for x in r["reasons"]))

    def test_rejects_a_candidate_that_is_already_stale(self):
        # Fresher than committed, but still past MAX_INDEX_AGE_DAYS. Mutation: keep only
        # the no-progress check -> a refresh from 40 days to 25 days "makes progress" and
        # ships a corpus that is STILL un-reachable, which is the exact state the refresh
        # exists to leave behind.
        r = G.assess_directory_refresh(_stats(266, 514, age_days=40.0),
                                       _stats(266, 514, age_days=25.0))
        self.assertFalse(r["accept"])
        self.assertTrue(any("already stale" in x for x in r["reasons"]))

    def test_the_stale_threshold_tracks_payto_baseline(self):
        # kills: hardcoding 21 here or in the guard. The gate's reachability is defined
        # by PB.MAX_INDEX_AGE_DAYS; a guard with its own copy would drift out of
        # agreement with the module that actually decides.
        just_stale = _stats(266, 514, age_days=float(PB.MAX_INDEX_AGE_DAYS))
        just_fresh = _stats(266, 514, age_days=PB.MAX_INDEX_AGE_DAYS - 0.5)
        old = _stats(266, 514, age_days=60.0)
        self.assertFalse(G.assess_directory_refresh(old, just_stale)["accept"])
        self.assertTrue(G.assess_directory_refresh(old, just_fresh)["accept"])

    def test_price_loss_warns_but_does_not_block(self):
        # A priced entry is what the baseline compares against, but host coverage -- the
        # thing `unknown` depends on -- is intact, so freshness still wins. Mutation:
        # promote this to a reject -> a legitimate refresh is thrown away and the corpus
        # walks to the 21-day cliff over a degradation that is not a safety problem.
        r = G.assess_directory_refresh(_stats(266, 514, age_days=20.0, priced=265),
                                       _stats(266, 514, age_days=0.1, priced=100))
        self.assertTrue(r["accept"], r["reasons"])
        self.assertTrue(any("carry no advertised price" in w for w in r["warnings"]))

    def test_a_sanctioned_entry_in_the_candidate_warns(self):
        # A sanctioned payee shipping in the corpus the gate reads is worth saying out
        # loud -- but it does not block, because the directory is a map of what IS
        # advertised, and dropping the refresh would not remove the payee from the
        # ecosystem. Kills: silently promoting a corpus that gained a flagged entry.
        r = G.assess_directory_refresh(_stats(266, 514, age_days=20.0, sanctioned=0),
                                       _stats(266, 514, age_days=0.1, sanctioned=2))
        self.assertTrue(r["accept"], r["reasons"])
        self.assertTrue(any("SANCTIONED" in w for w in r["warnings"]))

    def test_an_all_clean_corpus_says_nothing_about_sanctions(self):
        # THE AUDIT FIX, pinned. The previous version of this guard warned when the
        # sanctioned count FELL to zero, claiming zero meant NOT SCREENED. Every entry
        # in the real corpus carries `sanctioned: false`, so that condition was dead
        # code -- and it was also a category error, because the artifact cannot tell
        # "screened, none sanctioned" from "the list was unavailable". Kills a
        # reintroduction of either: a clean corpus must produce NO sanctions warning.
        r = G.assess_directory_refresh(_stats(266, 514, age_days=20.0, sanctioned=0),
                                       _stats(266, 514, age_days=0.1, sanctioned=0))
        self.assertTrue(r["accept"], r["reasons"])
        self.assertFalse([w for w in r["warnings"] if "SANCTIONED" in w.upper()])

    def test_the_real_corpus_is_all_clean_so_a_count_based_check_would_be_dead(self):
        # Pins the FACT the audit turned on, straight from the shipped artifact, so a
        # future guard cannot quietly reintroduce a sanctions check that never fires.
        records, _age, _dated = G._load("data/directory.json")
        self.assertTrue(records)
        self.assertEqual(G.directory_stats(records, age_days=1.0)["sanctioned"], 0)

    def test_warnings_never_populate_reasons(self):
        # kills: appending a warning to `reasons` by copy-paste, which would turn every
        # annotated accept into a silent reject.
        r = G.assess_directory_refresh(_stats(266, 514, age_days=20.0, sanctioned=0,
                                              priced=265),
                                       _stats(266, 514, age_days=0.1, sanctioned=2,
                                              priced=100))
        self.assertTrue(r["accept"])
        self.assertFalse(r["reasons"])
        self.assertEqual(len(r["warnings"]), 2)


class TestDirectoryStats(unittest.TestCase):
    def test_hosts_counts_the_payto_index_not_the_entries(self):
        # 2 entries, 3 distinct hosts -- because one payee advertises on two. This is
        # the whole reason the guard measures hosts: 58 of 266 corpus payees are
        # multi-host, so entries and hosts are different numbers by design.
        records = [_record("0xaa", ["a.example", "b.example"]),
                   _record("0xbb", ["c.example"])]
        s = G.directory_stats(records, age_days=1.0)
        self.assertEqual(s["entries"], 2)
        self.assertEqual(s["hosts"], 3)

    def test_a_non_list_corpus_is_empty_not_a_crash(self):
        # the artifact is refreshed by crawling third parties; a dict or a string must
        # read as "nothing", which the guard then rejects as EMPTY.
        for junk in ({"payees": []}, "nope", None, 7):
            s = G.directory_stats(junk, age_days=1.0)
            self.assertEqual(s["entries"], 0)
            self.assertEqual(s["hosts"], 0)

    def test_dated_is_false_when_age_is_unknown(self):
        s = G.directory_stats([_record("0xaa", ["a.example"])], age_days=None)
        self.assertFalse(s["dated"])


class TestLoadingFromDisk(unittest.TestCase):
    """_load must agree with payto_baseline about what counts as dated -- the hash
    guard is the load-bearing part, so a sidecar that does not pin these bytes has to
    read as UNDATED rather than as a date the guard trusts."""

    def _write(self, tmp, records):
        path = os.path.join(tmp, "directory.json")
        with open(path, "w") as fh:
            json.dump(records, fh)
        return path

    def test_a_correctly_pinned_sidecar_reads_as_dated(self):
        import datetime
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [_record("0xaa", ["a.example"])])
            PB.write_meta(path, datetime.datetime.now(datetime.timezone.utc)
                          .strftime("%Y-%m-%dT%H:%M:%SZ"))
            records, age, dated = G._load(path)
            self.assertEqual(len(records), 1)
            self.assertTrue(dated)
            self.assertIsNotNone(age)

    def test_a_sidecar_pinning_other_bytes_reads_as_undated(self):
        # kills: trusting the sidecar's date without its hash. This is the forgotten
        # refresh -- regenerate the corpus, leave the sidecar -- and it must NOT read as
        # a fresh corpus, or the guard would accept a date describing a file that is gone.
        import datetime
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [_record("0xaa", ["a.example"])])
            PB.write_meta(path, datetime.datetime.now(datetime.timezone.utc)
                          .strftime("%Y-%m-%dT%H:%M:%SZ"))
            with open(path, "w") as fh:                     # corpus changes, sidecar does not
                json.dump([_record("0xaa", ["a.example"]),
                           _record("0xbb", ["b.example"])], fh)
            _records, age, dated = G._load(path)
            self.assertFalse(dated)
            self.assertIsNone(age)

    def test_a_missing_file_is_empty_and_undated(self):
        records, age, dated = G._load("/nonexistent/directory.json")
        self.assertEqual(records, [])
        self.assertIsNone(age)
        self.assertFalse(dated)


class TestTheShippedCorpus(unittest.TestCase):
    def test_the_shipped_directory_would_pass_its_own_guard_when_refreshed(self):
        # An END-TO-END sanity check on the real artifact: a candidate identical to the
        # shipped corpus but freshly dated must ACCEPT. Kills a guard so strict that no
        # real refresh could ever ship -- which would be indistinguishable from having
        # no automation, the state this module was written to end.
        records, _age, _dated = G._load("data/directory.json")
        self.assertTrue(records, "the shipped directory failed to load")
        old = G.directory_stats(records, age_days=30.0)
        new = G.directory_stats(records, age_days=0.05)
        r = G.assess_directory_refresh(old, new)
        self.assertTrue(r["accept"], r["reasons"])


if __name__ == "__main__":
    unittest.main()
