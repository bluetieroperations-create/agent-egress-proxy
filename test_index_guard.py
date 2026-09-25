"""
test_index_guard.py -- guards the SEED INDEX release gate.

The third of three guards (store, directory, indexes). The point is the same: a bad
refresh must NEVER ship. Each test states the mutation it kills.

The hard part of this gate is not catching a collapse -- it is NOT catching an honest
shrink, because a category legitimately falling under MIN_CATEGORY_PAYEES(5) is correct
behaviour and blocking the refresh over it would be the bug. Both directions are pinned
against MEASURED numbers, not invented ones.
"""
import json
import unittest

import index_guard as G


def _stats(cats, divs):
    """Stats shaped like index_stats() output, from two key lists."""
    return G.index_stats({c: "0.01" for c in cats}, {d: "2.0" for d in divs})


# The real committed index on 2026-09-16, and the refresh of 2026-09-21.
MAIN_CATS = ["ai-agents", "commerce", "content-media", "dev-tools", "finance",
             "onchain", "search-data"]                                    # 7
REFRESHED_CATS = [c for c in MAIN_CATS if c != "commerce"]                # 6
DIVS_18 = ["0x%02d" % i for i in range(18)]
DIVS_15 = DIVS_18[:8] + ["0xff%02d" % i for i in range(7)]                # 8 kept, 7 new


class TestTheTwoMeasuredCases(unittest.TestCase):
    """The whole design rests on telling these two apart. Both are real."""

    def test_the_real_2026_09_21_shrink_is_accepted(self):
        # MEASURED LEGITIMATE. 7 -> 6 categories, losing `commerce`. Verified by rebuild:
        # still 6 at DOUBLE depth (48 pages), and at --min-payees 1 the index has NINE
        # categories with commerce among the three under the threshold. So commerce
        # genuinely has < 5 distinct payees and omitting it is correct.
        #
        # Kills: any threshold strict enough to reject this. That mutation would block
        # every honest refresh whenever one category thins out, walking the corpus toward
        # the stale cliff to protect against nothing.
        r = G.assess_index_refresh(_stats(MAIN_CATS, DIVS_18),
                                   _stats(REFRESHED_CATS, DIVS_15))
        self.assertTrue(r["accept"], r["reasons"])

    def test_the_measured_shallow_crawl_is_rejected(self):
        # MEASURED SHALLOW. refresh_seed.sh's own note, 2026-08-28: 8 pages produced FOUR
        # baselines where 24 produced SEVEN. Four of seven is a crawl that failed.
        #
        # Kills: dropping the collapse check, or loosening it past 4/7 -- which is the
        # entire reason this module exists.
        r = G.assess_index_refresh(_stats(MAIN_CATS, DIVS_18),
                                   _stats(MAIN_CATS[:4], DIVS_18))
        self.assertFalse(r["accept"])
        self.assertTrue(any("collapsed" in x for x in r["reasons"]))

    def test_the_threshold_sits_between_the_two_measured_points(self):
        # kills: a threshold picked for looking round rather than for separating the two
        # observations. 5/7 (71%) must pass and 4/7 (57%) must fail; anything that breaks
        # either end has lost the measurement it was derived from.
        old = _stats(MAIN_CATS, DIVS_18)
        self.assertTrue(G.assess_index_refresh(old, _stats(MAIN_CATS[:5], DIVS_18))["accept"])
        self.assertFalse(G.assess_index_refresh(old, _stats(MAIN_CATS[:4], DIVS_18))["accept"])


class TestRejects(unittest.TestCase):
    OLD = _stats(MAIN_CATS, DIVS_18)

    def test_rejects_an_empty_category_index(self):
        # Kills: treating empty as merely "small". An empty index disables the
        # category-price check for EVERY category at once, and fail-open means nothing
        # reports it.
        r = G.assess_index_refresh(self.OLD, _stats([], DIVS_18))
        self.assertFalse(r["accept"])
        self.assertTrue(any("EMPTY" in x for x in r["reasons"]))

    def test_rejects_an_empty_divergence_index(self):
        r = G.assess_index_refresh(self.OLD, _stats(MAIN_CATS, []))
        self.assertFalse(r["accept"])
        self.assertTrue(any("divergence index is EMPTY" in x for x in r["reasons"]))

    def test_a_hard_divergence_shrink_WARNS_and_still_ships(self):
        # AUDITED DOWN FROM A REJECT, and this test is the reason pinned in place. The
        # healthy 2026-09-21 refresh replaced 56% of this index's membership (lost 10 of
        # 18, gained 7). A refresh losing the same ten and gaining one sits at 9/18, so a
        # count-based reject would have blocked a good refresh -- and because the STORE
        # rides on this verdict, it would have blocked the store too.
        #
        # Kills: promoting this back to a reject. Count cannot separate churn from a
        # failed crawl for a per-payee index; only EMPTY is unambiguous.
        r = G.assess_index_refresh(self.OLD, _stats(MAIN_CATS, DIVS_18[:4]))
        self.assertTrue(r["accept"], r["reasons"])
        self.assertTrue(any("shrank hard" in w for w in r["warnings"]))

    def test_only_an_EMPTY_divergence_index_rejects(self):
        # kills: silently accepting a build that produced nothing. Empty is the one
        # unambiguous divergence failure, so it is the one that blocks.
        self.assertTrue(G.assess_index_refresh(self.OLD, _stats(MAIN_CATS, DIVS_18[:1]))["accept"])
        self.assertFalse(G.assess_index_refresh(self.OLD, _stats(MAIN_CATS, []))["accept"])

    def test_the_divergence_notice_line_is_looser_than_the_category_reject(self):
        # kills: collapsing the two constants into one. They are different KINDS of
        # threshold -- one blocks, one annotates -- and the per-payee index is the looser.
        self.assertLess(G.DIVERGENCE_WARN_RETENTION, G.MIN_CATEGORY_RETENTION)


class TestWarnings(unittest.TestCase):
    OLD = _stats(MAIN_CATS, DIVS_18)

    def test_a_lost_category_is_always_named(self):
        # THE RECORD WHOSE ABSENCE COST TWO EXTRA CRAWLS. Establishing that the
        # 2026-09-21 shrink was legitimate required rebuilding the index twice, because
        # nothing said which category went or what losing it costs. An accept must still
        # say so.
        r = G.assess_index_refresh(self.OLD, _stats(REFRESHED_CATS, DIVS_18))
        self.assertTrue(r["accept"])
        self.assertTrue(any("commerce" in w for w in r["warnings"]))

    def test_the_warning_says_the_check_goes_fail_open(self):
        # kills: a warning that names the category but not the consequence. An operator
        # reading "commerce no longer indexed" cannot tell whether that means the check
        # got stricter or stopped existing. It stopped existing.
        r = G.assess_index_refresh(self.OLD, _stats(REFRESHED_CATS, DIVS_18))
        self.assertTrue(any("FAIL-OPEN" in w for w in r["warnings"]))

    def test_a_gained_category_is_reported_too(self):
        r = G.assess_index_refresh(self.OLD, _stats(MAIN_CATS + ["storage-files"], DIVS_18))
        self.assertTrue(r["accept"])
        self.assertTrue(any("newly indexed" in w for w in r["warnings"]))

    def test_warnings_never_populate_reasons(self):
        # kills: appending a warning to `reasons` by copy-paste, which would turn every
        # annotated accept into a silent reject -- and this gate warns on almost every
        # healthy refresh, so that mutation would block nearly all of them.
        r = G.assess_index_refresh(self.OLD, _stats(REFRESHED_CATS, DIVS_15))
        self.assertTrue(r["accept"])
        self.assertFalse(r["reasons"])
        self.assertTrue(r["warnings"])

    def test_an_unchanged_index_warns_about_nothing(self):
        r = G.assess_index_refresh(self.OLD, _stats(MAIN_CATS, DIVS_18))
        self.assertTrue(r["accept"])
        self.assertEqual(r["warnings"], [])


class TestIndexStats(unittest.TestCase):
    def test_a_non_dict_index_reads_as_empty_not_a_crash(self):
        # These files are rebuilt by crawling third parties and `load_index_json` fails
        # soft, so a truncated or malformed write must reach the guard as "nothing" and
        # be rejected as EMPTY rather than raising inside the refresh.
        for junk in ([], "nope", None, 7):
            s = G.index_stats(junk, junk)
            self.assertEqual(s["categories"], 0)
            self.assertEqual(s["divergences"], 0)

    def test_a_missing_file_loads_as_empty(self):
        self.assertEqual(G._load("/nonexistent/category_index.json"), {})

    def test_a_malformed_file_loads_as_empty(self):
        import os
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write('{"truncated": ')
        try:
            self.assertEqual(G._load(path), {})
        finally:
            os.unlink(path)


class TestTheShippedIndexes(unittest.TestCase):
    def test_the_shipped_indexes_would_pass_their_own_guard(self):
        # END-TO-END on the real artifacts: a candidate identical to what ships must
        # ACCEPT. Kills a gate so strict that no real refresh could pass it, which would
        # be indistinguishable from having no automation -- the state this module ends.
        cats = G._load("data/category_index.json")
        divs = G._load("data/divergence_index.json")
        self.assertTrue(cats, "the shipped category index failed to load")
        self.assertTrue(divs, "the shipped divergence index failed to load")
        stats = G.index_stats(cats, divs)
        r = G.assess_index_refresh(stats, stats)
        self.assertTrue(r["accept"], r["reasons"])

    def test_the_gate_is_not_inert_against_the_shipped_index(self):
        # Proves the gate BITES on the real artifact: drop 43% of the shipped baselines
        # and it must reject.
        #
        # This deliberately tests the GATE, not the DATA. The first draft asserted
        # `len(cats) >= 5` on the shipped file, which is a second data-freshness tripwire:
        # a legitimate thinning to four categories would have turned CI red for every
        # open PR, which is precisely what the payTo reachability tripwire did to this
        # repo on 2026-09-18. Refresh-time rejection is the guard's job; the suite's job
        # is to prove the guard works.
        cats = G._load("data/category_index.json")
        self.assertTrue(cats, "the shipped category index failed to load")
        full = G.index_stats(cats, G._load("data/divergence_index.json"))
        keep = sorted(cats)[:max(1, int(len(cats) * 0.57))]
        gutted = G.index_stats({k: cats[k] for k in keep},
                               G._load("data/divergence_index.json"))
        r = G.assess_index_refresh(full, gutted)
        self.assertFalse(r["accept"], "the gate did not bite on a 43%% cut")


class TestTheScriptRunsBothGuards(unittest.TestCase):
    """refresh_seed.sh must actually CALL this guard, and must not promote without it."""

    def _script(self):
        with open("scripts/refresh_seed.sh") as fh:
            return fh.read()

    def test_the_script_invokes_the_index_guard(self):
        # kills: shipping the module without wiring it -- the wired-and-inert pattern
        # this repo has been bitten by repeatedly.
        self.assertIn("index_guard.py", self._script())

    def test_promotion_requires_both_verdicts(self):
        # kills: promoting on the store verdict alone, which is the status quo this
        # change exists to end.
        text = self._script()
        self.assertIn('[ "$STORE_OK" = "1" ] && [ "$INDEX_OK" = "1" ]', text)

    def test_both_guards_run_before_the_decision(self):
        # kills: short-circuiting, which would hide a second reject and cost another
        # 25-minute crawl to discover.
        text = self._script()
        self.assertLess(text.index("index_guard.py"),
                        text.index('[ "$STORE_OK" = "1" ]'))


if __name__ == "__main__":
    unittest.main()
