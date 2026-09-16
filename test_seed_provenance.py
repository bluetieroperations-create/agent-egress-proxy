"""
Tests for seed_provenance.py.

The corpus is a bounded sample. These pin the two things that make the record
worth having: that it never reports an unmeasured quantity as zero, and that the
floor warning cannot be quietly dropped.

Each test names the mutation it kills.
"""
import unittest

import seed_provenance as S

STORE = {"payees": 281, "edges": 46031, "payers": 2511,
         "gating_capable": 237, "age_days": 3}
CRAWL = {"payees": 270, "errors": 11, "truncated": 70}


class TestBuildProvenance(unittest.TestCase):
    def test_records_the_cap_and_who_hit_it(self):
        r = S.build_provenance(STORE, CRAWL, max_pages=4, built_at="T")
        self.assertEqual(r["max_pages_per_payee"], 4)
        self.assertEqual(r["crawl_payees_at_page_cap"], 70)
        self.assertEqual(r["crawl_payees_failed"], 11)

    def test_attempted_is_successes_plus_failures(self):
        # `payees` in a backfill summary counts only the ones that SUCCEEDED.
        # Mutation: report it as the attempted total -> the record understates
        # the crawl's size and the failure rate cannot be recomputed from it.
        r = S.build_provenance(STORE, CRAWL, max_pages=4, built_at="T")
        self.assertEqual(r["crawl_payees_attempted"], 281)

    def test_an_absent_crawl_yields_NULL_not_zero(self):
        # THE ONE THAT MATTERS. Mutation: default the counts to 0 -> a record
        # built without a crawl summary claims "0 payees hit the cap, 0 failed",
        # which is the strongest possible completeness claim and is unmeasured.
        # Same error as a refresh guard reading an absent summary as healthy.
        r = S.build_provenance(STORE, None, max_pages=4, built_at="T")
        self.assertIsNone(r["crawl_payees_at_page_cap"])
        self.assertIsNone(r["crawl_payees_failed"])
        self.assertIsNone(r["crawl_payees_attempted"])

    def test_completeness_is_stated_not_implied(self):
        # Mutation: drop the field -> a consumer must infer boundedness from a
        # cap number it may not think to read.
        self.assertEqual(S.build_provenance(STORE, CRAWL, max_pages=4,
                                            built_at="T")["completeness"], "BOUNDED")

    def test_the_floor_warning_is_always_present(self):
        # Mutation: make the warning conditional on truncated > 0 -> a crawl that
        # happened to cap nothing ships a record with no caveat, and the next
        # reader takes its totals as measurements.
        for crawl in (CRAWL, {"payees": 281, "errors": 0, "truncated": 0}, None):
            r = S.build_provenance(STORE, crawl, max_pages=4, built_at="T")
            self.assertIn("FLOOR", r["warning"])
            self.assertIn("newest-first", r["warning"])

    def test_the_measured_cost_is_carried_with_the_claim(self):
        # An abstract caveat gets skimmed; a number does not. Mutation: drop it
        # -> the record says "totals are floors" with nothing to show the scale.
        r = S.build_provenance(STORE, CRAWL, max_pages=4, built_at="T")
        self.assertIn("27,264,465", r["measured_cost_of_the_bound"])
        self.assertIn("250", r["measured_cost_of_the_bound"])

    def test_an_absent_crawl_explains_its_own_nulls(self):
        # Three nulls are honest but ambiguous -- a reader can take them for a
        # crawl that reported nothing, i.e. a clean one. Mutation: drop the note
        # -> the most complete-looking record is the one with no measurement.
        r = S.build_provenance(STORE, None, max_pages=4, built_at="T")
        self.assertIn("NOT RECORDED", r["crawl_summary"])
        self.assertIn("not zero", r["crawl_summary"])

    def test_a_present_crawl_carries_no_such_note(self):
        # Mutation: always attach it -> a record with real counts also says they
        # were not recorded.
        self.assertNotIn("crawl_summary",
                         S.build_provenance(STORE, CRAWL, max_pages=4, built_at="T"))

    def test_it_is_json_serializable(self):
        import json
        json.dumps(S.build_provenance(STORE, CRAWL, max_pages=4, built_at="T"))

    def test_optional_asset_is_omitted_when_absent(self):
        self.assertNotIn("asset", S.build_provenance(STORE, CRAWL, max_pages=4, built_at="T"))
        self.assertEqual(S.build_provenance(STORE, CRAWL, max_pages=4, built_at="T",
                                            asset="0xabc")["asset"], "0xabc")


if __name__ == "__main__":
    unittest.main()
