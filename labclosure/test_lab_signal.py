"""Tests for lab_signal. Each states the mutation it kills (repo convention)."""
import unittest
import datetime as dt

from lab_signal import (
    to_date, pi_ids, support_end, is_desk_science, build_labs,
    is_equipment_bearing, find_dark_labs, drop_still_funded,
    dedupe_shared_grants,
)

D = dt.date


def award(pids=(1,), end="2025-01-31", amount=1_000_000, code="R01",
          added="2020-01-01", dept="PHYSIOLOGY", org="TEST U"):
    return {
        "principal_investigators": [
            {"profile_id": p, "full_name": f"PI {p}"} for p in pids],
        "budget_end": end + "T00:00:00" if end else None,
        "award_amount": amount,
        "activity_code": code,
        "date_added": added + "T00:00:00" if added else None,
        "organization": {"org_name": org, "dept_type": dept},
    }


class TestPure(unittest.TestCase):
    def test_to_date_none_safe(self):
        # kills: dropping the None guard (would raise on missing dates,
        # and ~0.03% of measured records carry an empty budget_start)
        self.assertIsNone(to_date(None))
        self.assertEqual(to_date("2025-06-01T12:00:00"), D(2025, 6, 1))

    def test_pi_ids_returns_every_pi(self):
        # kills: taking only the contact PI -- a co-PI on a live grant is
        # still funded, and missing that manufactures a false closure
        self.assertEqual(pi_ids(award(pids=(7, 8))), [7, 8])

    def test_pi_ids_skips_missing_profile(self):
        # kills: assuming profile_id is always present
        a = award()
        a["principal_investigators"].append({"full_name": "No Id"})
        self.assertEqual(pi_ids(a), [1])

    def test_support_end_falls_back_to_project_end(self):
        # kills: reading only budget_end, which drops awards that carry
        # solely a project end date
        a = award(end=None)
        a["project_end_date"] = "2026-03-31T00:00:00"
        self.assertEqual(support_end(a), D(2026, 3, 31))

    def test_desk_science_needs_all_known_depts_desk(self):
        # kills: flagging desk science on ANY match -- a PI with one
        # biostatistics grant and a physiology lab still owns freezers
        self.assertTrue(is_desk_science({"BIOSTATISTICS & OTHER MATH SCI"}))
        self.assertFalse(is_desk_science({"BIOSTATISTICS & OTHER MATH SCI",
                                          "PHYSIOLOGY"}))

    def test_desk_science_unknown_dept_is_not_desk(self):
        # kills: treating missing dept_type as desk science; dept_type is
        # absent on ~45% of measured awards, so that would gut the list
        self.assertFalse(is_desk_science({None}))
        self.assertFalse(is_desk_science(set()))


class TestRollup(unittest.TestCase):
    def test_vintage_hides_later_records(self):
        # kills: ignoring date_added -- point-in-time reconstruction is the
        # whole basis of the backtest, and look-ahead invalidates it
        rows = [award(added="2024-01-01"), award(end="2026-12-31",
                                                 added="2026-01-01")]
        past = build_labs(rows, vintage=D(2025, 1, 1))
        self.assertEqual(past[1]["last_end"], D(2025, 1, 31))
        now = build_labs(rows)
        self.assertEqual(now[1]["last_end"], D(2026, 12, 31))

    def test_last_end_is_the_maximum(self):
        # kills: taking the first or last row rather than the max, which
        # would call a still-funded lab dark
        rows = [award(end="2026-06-30"), award(end="2024-01-01")]
        self.assertEqual(build_labs(rows)[1]["last_end"], D(2026, 6, 30))


class TestFiltering(unittest.TestCase):
    def test_trainee_only_pi_is_not_a_lab(self):
        # kills: counting fellowships as labs -- an F31 is a graduate
        # student with no equipment to sell
        lab = build_labs([award(code="F31")])[1]
        self.assertFalse(is_equipment_bearing(lab))

    def test_small_award_is_not_a_lab(self):
        lab = build_labs([award(amount=50_000)])[1]
        self.assertFalse(is_equipment_bearing(lab))

    def test_quiet_period_suppresses_recent_endings(self):
        # kills: dropping QUIET_DAYS -- 18% of awards post more than 30 days
        # after their budget start, so recent silence is usually lag
        labs = build_labs([award(end="2026-08-01")])
        self.assertEqual(find_dark_labs(labs, D(2026, 8, 25)), [])
        self.assertEqual(len(find_dark_labs(labs, D(2026, 8, 25),
                                            quiet_days=0)), 1)

    def test_stale_darkness_is_excluded(self):
        # kills: dropping MAX_DARK_DAYS -- a lab dark since 2020 is history,
        # not a lead, and stale rows were 62% of the raw MI flag set
        labs = build_labs([award(end="2021-01-01")])
        self.assertEqual(find_dark_labs(labs, D(2026, 8, 25)), [])

    def test_still_funded_pi_is_dropped(self):
        # kills: skipping the national pass -- 24.3% of state-flagged PIs
        # had simply moved institution
        labs = build_labs([award(end="2026-01-31")])
        cands = find_dark_labs(labs, D(2026, 8, 25))
        self.assertEqual(len(cands), 1)
        elsewhere = [award(pids=(1,), end="2027-01-01", org="OTHER U")]
        self.assertEqual(
            drop_still_funded(cands, elsewhere, D(2026, 8, 25)), [])

    def test_shared_grant_yields_one_site(self):
        # kills: emitting a row per co-PI, which sends a dealer the same
        # address twice and reads as a sloppy list
        labs = build_labs([award(pids=(1, 2), end="2026-01-31")])
        cands = find_dark_labs(labs, D(2026, 8, 25))
        self.assertEqual(len(cands), 2)
        self.assertEqual(len(dedupe_shared_grants(cands)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
