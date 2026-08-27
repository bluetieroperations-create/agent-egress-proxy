"""Tests for the ecosystem snapshot series.

Each test names the mutation it kills, per repo convention.
"""

import os
import tempfile
import unittest

from datetime import date

import history as h


def row(host, cls="body_accepts", settlements=0):
    return {"host": host, "class": cls, "settlements": settlements}


class TestNames(unittest.TestCase):
    def test_roundtrip(self):
        # kills: filename format drift between writer and reader
        d = date(2026, 8, 25)
        self.assertEqual(h.parse_snapshot_name(h.snapshot_name(d)), d)

    def test_rejects_non_snapshot(self):
        # kills: treating stray files (README, .DS_Store) as snapshots
        self.assertIsNone(h.parse_snapshot_name("directory.json"))
        self.assertIsNone(h.parse_snapshot_name("2026-08.json"))


class TestIndex(unittest.TestCase):
    def test_first_row_wins(self):
        # kills: last-wins, which would discard the survey's best-ranked probe
        idx = h.index_by_host([row("a", "body_accepts"), row("a", "opaque_402")])
        self.assertEqual(idx["a"]["class"], "body_accepts")

    def test_drops_hostless(self):
        # kills: a None key crashing set ops downstream
        self.assertEqual(h.index_by_host([{"class": "dead"}]), {})


class TestGrowth(unittest.TestCase):
    def test_normal(self):
        # kills: reversed subtraction
        self.assertEqual(h.growth(row("a", settlements=10), row("a", settlements=25)), 15)

    def test_clamps_negative(self):
        # kills: reporting a moved backfill window as a shrinking seller
        self.assertEqual(h.growth(row("a", settlements=25), row("a", settlements=10)), 0)

    def test_missing_counts_as_zero(self):
        # kills: None arithmetic on a row with no settlement field
        self.assertEqual(h.growth({"host": "a"}, {"host": "a"}), 0)


class TestDiff(unittest.TestCase):
    def setUp(self):
        self.prev = [row("stays"), row("dies"), row("breaks", "body_accepts")]
        self.curr = [row("stays"), row("born"), row("breaks", "opaque_402")]
        self.d = h.diff(self.prev, self.curr)

    def test_appeared_and_disappeared(self):
        # kills: swapped prev/curr in the set difference
        self.assertEqual(self.d["appeared"], ["born"])
        self.assertEqual(self.d["disappeared"], ["dies"])

    def test_became_unpayable(self):
        # kills: comparing liveness only, missing a host that is UP but unparseable
        self.assertEqual(self.d["became_unpayable"], ["breaks"])
        self.assertEqual(self.d["became_payable"], [])

    def test_became_payable(self):
        # kills: one-directional transition detection
        d = h.diff([row("x", "opaque_402")], [row("x", "hdr_accepts")])
        self.assertEqual(d["became_payable"], ["x"])

    def test_class_change_is_separate_from_payability(self):
        # kills: collapsing class_changed into became_(un)payable -- a move between
        # two non-scoreable classes is a real change but not a payability flip
        d = h.diff([row("x", "opaque_402")], [row("x", "dead")])
        self.assertEqual(d["class_changed"], ["x"])
        self.assertEqual(d["became_payable"], [])
        self.assertEqual(d["became_unpayable"], [])

    def test_growth_omits_flat_hosts(self):
        # kills: emitting a zero-growth entry for every host, burying real movement
        d = h.diff([row("a", settlements=5), row("b", settlements=5)],
                   [row("a", settlements=5), row("b", settlements=9)])
        self.assertEqual(d["growth"], {"b": 4})


class TestChurn(unittest.TestCase):
    def test_rate(self):
        # kills: dividing by the wrong snapshot's size
        self.assertAlmostEqual(
            h.churn_rate([row("a"), row("b"), row("c"), row("d")], [row("a")]), 0.75
        )

    def test_empty_prev_is_zero(self):
        # kills: ZeroDivisionError on a leading empty snapshot
        self.assertEqual(h.churn_rate([], [row("a")]), 0.0)

    def test_new_hosts_do_not_reduce_churn(self):
        # kills: counting arrivals against departures, hiding mortality
        self.assertAlmostEqual(
            h.churn_rate([row("a"), row("b")], [row("a"), row("x"), row("y")]), 0.5
        )


class TestSurvival(unittest.TestCase):
    def setUp(self):
        self.snaps = [
            (date(2026, 1, 1), [row("old"), row("gone")]),
            (date(2026, 2, 1), [row("old"), row("mid")]),
            (date(2026, 3, 1), [row("old"), row("mid")]),
        ]
        self.s = h.survival(self.snaps)

    def test_first_and_last_seen(self):
        # kills: overwriting first_seen on every observation
        self.assertEqual(self.s["old"]["first_seen"], date(2026, 1, 1))
        self.assertEqual(self.s["old"]["last_seen"], date(2026, 3, 1))

    def test_departed_host_keeps_its_last_seen(self):
        # kills: extending last_seen to the newest snapshot for absent hosts
        self.assertEqual(self.s["gone"]["last_seen"], date(2026, 1, 1))
        self.assertEqual(self.s["gone"]["observations"], 1)

    def test_days_observed(self):
        # kills: off-by-one / reversed date subtraction
        self.assertEqual(self.s["old"]["days_observed"], 59)

    def test_unsorted_input_still_orders(self):
        # kills: trusting caller ordering -- load_all sorts, but callers may not
        s = h.survival(list(reversed(self.snaps)))
        self.assertEqual(s["old"]["first_seen"], date(2026, 1, 1))
        self.assertEqual(s["old"]["last_seen"], date(2026, 3, 1))

    def test_still_alive_uses_latest_on_or_before(self):
        # kills: reading the newest snapshot regardless of as_of (look-ahead bias)
        self.assertEqual(h.still_alive(self.snaps, date(2026, 1, 15)), {"old", "gone"})

    def test_still_alive_empty_before_first(self):
        # kills: returning the first snapshot for an as_of predating all data
        self.assertEqual(h.still_alive(self.snaps, date(2025, 1, 1)), set())


class TestStore(unittest.TestCase):
    def test_roundtrip(self):
        # kills: writer/reader disagreement on layout
        with tempfile.TemporaryDirectory() as d:
            h.store(d, [row("a")], date(2026, 8, 25))
            loaded = h.load_all(d)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0][0], date(2026, 8, 25))
            self.assertEqual(loaded[0][1][0]["host"], "a")

    def test_refuses_overwrite(self):
        # kills: silently replacing a past measurement -- destroys the only
        # thing a competitor cannot reproduce
        with tempfile.TemporaryDirectory() as d:
            h.store(d, [row("a")], date(2026, 8, 25))
            with self.assertRaises(FileExistsError):
                h.store(d, [row("b")], date(2026, 8, 25))

    def test_load_ignores_foreign_files(self):
        # kills: crashing on a README or notes file in the snapshot dir
        with tempfile.TemporaryDirectory() as d:
            h.store(d, [row("a")], date(2026, 8, 25))
            open(os.path.join(d, "README.md"), "w").write("notes")
            self.assertEqual(len(h.load_all(d)), 1)

    def test_load_is_chronological(self):
        # kills: filesystem ordering leaking through and scrambling the series
        with tempfile.TemporaryDirectory() as d:
            h.store(d, [row("b")], date(2026, 9, 1))
            h.store(d, [row("a")], date(2026, 8, 25))
            self.assertEqual([w for w, _ in h.load_all(d)],
                             [date(2026, 8, 25), date(2026, 9, 1)])


if __name__ == "__main__":
    unittest.main()


class TestCompressedStore(unittest.TestCase):
    def test_gzip_roundtrip(self):
        # kills: writing gzip but reading as plain text (or the reverse)
        with tempfile.TemporaryDirectory() as d:
            h.store(d, [row("a")], date(2026, 8, 27), compress=True)
            loaded = h.load_all(d)
            self.assertEqual(loaded[0][1][0]["host"], "a")

    def test_gzip_blocks_plain_for_same_date(self):
        # kills: the extension becoming the identity instead of the DATE --
        # storing both forms would silently duplicate a reading and let a
        # rewritten one shadow the original
        with tempfile.TemporaryDirectory() as d:
            h.store(d, [row("a")], date(2026, 8, 27), compress=True)
            with self.assertRaises(FileExistsError):
                h.store(d, [row("b")], date(2026, 8, 27))

    def test_plain_blocks_gzip_for_same_date(self):
        # kills: the collision check being one-directional
        with tempfile.TemporaryDirectory() as d:
            h.store(d, [row("a")], date(2026, 8, 27))
            with self.assertRaises(FileExistsError):
                h.store(d, [row("b")], date(2026, 8, 27), compress=True)

    def test_mixed_formats_load_in_date_order(self):
        # kills: sorting by filename, where ".json.gz" and ".json" interleave
        # wrongly and scramble the series
        with tempfile.TemporaryDirectory() as d:
            h.store(d, [row("b")], date(2026, 9, 1), compress=True)
            h.store(d, [row("a")], date(2026, 8, 27))
            self.assertEqual([w for w, _ in h.load_all(d)],
                             [date(2026, 8, 27), date(2026, 9, 1)])

    def test_parses_gz_name(self):
        # kills: the filename regex rejecting the compressed form, which would
        # make every gzipped reading invisible to load_all
        self.assertEqual(h.parse_snapshot_name("2026-08-27.json.gz"),
                         date(2026, 8, 27))
