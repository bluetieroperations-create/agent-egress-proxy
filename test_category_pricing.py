"""
Tests for category_pricing.py -- per-category on-chain price baselines. Each test
states the mutation it kills.
"""
import unittest

import category_pricing as CP


class _Store:
    def __init__(self, recs):
        self.recs = recs

    def lookup(self, cp):
        return self.recs.get(cp.lower(), {})


class TestBuildIndex(unittest.TestCase):
    """
    Mutation notes:
      - count rows instead of distinct payees -> test_min_payees FAILS (one payee
        with many settlements would fake a market).
      - index 'other' -> test_other_excluded FAILS.
      - mean instead of median-of-medians -> test_wash_resistant FAILS.
    """
    def _obs(self, cat, payee, *amounts):
        return [{"category": cat, "payee": payee, "amount": a} for a in amounts]

    def test_indexes_with_enough_payees(self):
        obs = []
        for i in range(5):
            obs += self._obs("finance", "0x%02d" % i, "0.005")
        self.assertEqual(CP.build_category_index(obs), {"finance": "0.005"})

    def test_min_payees_gate(self):
        # 2 distinct payees < MIN(5) -> omitted (not enough of a market)
        obs = self._obs("finance", "0xa", "0.005") + self._obs("finance", "0xb", "0.005")
        self.assertEqual(CP.build_category_index(obs), {})

    def test_one_payee_many_rows_is_not_a_market(self):
        obs = self._obs("finance", "0xa", *["0.005"] * 50)   # one payee, 50 rows
        self.assertEqual(CP.build_category_index(obs), {})    # still < min DISTINCT payees

    def test_other_excluded(self):
        obs = []
        for i in range(6):
            obs += self._obs("other", "0x%02d" % i, "1.0")
        self.assertNotIn("other", CP.build_category_index(obs))

    def test_wash_resistant_median_of_medians(self):
        # 4 payees at 0.005 + one wash payee spamming 100.0 -> median-of-medians ~0.005,
        # not dragged up by the wash volume.
        obs = []
        for i in range(4):
            obs += self._obs("finance", "0x%02d" % i, "0.005")
        obs += self._obs("finance", "0xwash", *["100.0"] * 100)
        idx = CP.build_category_index(obs)
        self.assertIn("finance", idx)
        self.assertLess(float(idx["finance"]), 1.0)


class TestLoadCategoryIndex(unittest.TestCase):
    """Shared loader for BLACKWALL_CATEGORY_INDEX (HTTP + MCP use it -- must agree).

    Mutation notes:
      - return an error for a falsy path -> test_no_path FAILS (would spam a warning).
      - swallow a bad file silently -> test_bad_file FAILS (caller couldn't warn).
      - not stringify values -> test_normalizes FAILS.
    """
    def _write(self, text):
        import os
        import tempfile
        p = os.path.join(tempfile.mkdtemp(), "idx.json")
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_no_path_silent(self):
        self.assertEqual(CP.load_category_index(None), (None, None))
        self.assertEqual(CP.load_category_index(""), (None, None))

    def test_loads_and_normalizes(self):
        idx, err = CP.load_category_index(self._write('{"finance": 0.005}'))
        self.assertIsNone(err)
        self.assertEqual(idx, {"finance": "0.005"})   # value stringified

    def test_missing_file_reports_error(self):
        idx, err = CP.load_category_index("/no/such/index.json")
        self.assertIsNone(idx)
        self.assertIsNotNone(err)                      # caller can warn

    def test_bad_json_reports_error(self):
        idx, err = CP.load_category_index(self._write("{not json"))
        self.assertIsNone(idx)
        self.assertIsNotNone(err)

    def test_empty_object_reports_error(self):
        idx, err = CP.load_category_index(self._write("{}"))
        self.assertIsNone(idx)
        self.assertIsNotNone(err)


class TestStoreJoin(unittest.TestCase):
    """
    Mutation notes:
      - use advertised price_atomic instead of the store's on-chain price_history ->
        test_uses_onchain_history FAILS.
      - don't classify per payee -> test_end_to_end FAILS.
    """
    def test_observations_from_store(self):
        store = _Store({"0xa": {"price_history": ["0.005", "0.006"]}})
        obs = CP.observations_from_store(store, {"0xa": "finance"})
        self.assertEqual([o["amount"] for o in obs], ["0.005", "0.006"])
        self.assertTrue(all(o["category"] == "finance" for o in obs))

    def test_skips_other_and_missing(self):
        store = _Store({"0xa": {"price_history": ["1"]}})
        self.assertEqual(CP.observations_from_store(store, {"0xa": "other"}), [])
        self.assertEqual(CP.observations_from_store(store, {"0xz": "finance"}), [])

    def test_end_to_end_build_index(self):
        # 5 finance payees each with on-chain history -> a finance baseline
        recs = {"0x%02d" % i: {"price_history": ["0.005"]} for i in range(5)}
        store = _Store(recs)
        resources = [{"payTo": "0x%02d" % i, "resource": "https://x/price/btc"}
                     for i in range(5)]
        idx = CP.build_index(store, resources)
        self.assertEqual(idx.get("finance"), "0.005")


if __name__ == "__main__":
    unittest.main()


class TestPayeeCounts(unittest.TestCase):
    """The number a reader of category_index.json cannot recover and most needs.

    A baseline is a median-of-medians across DISTINCT payees, so one over 7 payees moves
    when a single payee enters or leaves while one over 29 does not -- and the finished
    artifact renders both as one price string. Measured 2026-09-28: `dev-tools` rested on
    7 and moved 2.25x; `ai-agents` rested on 26 and did not move at all.
    """

    OBS = [
        {"category": "dev-tools", "payee": "0xa", "amount": "0.01"},
        {"category": "dev-tools", "payee": "0xa", "amount": "0.02"},   # same payee again
        {"category": "dev-tools", "payee": "0xb", "amount": "0.03"},
        {"category": "finance",   "payee": "0xc", "amount": "0.04"},
    ]

    def test_it_counts_DISTINCT_payees_not_settlements(self):
        # kills: counting observations. 0xa contributes two settlements and one payee;
        # counting rows would report dev-tools as 3 and make a thin baseline look deep --
        # inverting the signal this file exists to carry.
        self.assertEqual(CP.category_payee_counts(self.OBS),
                         {"dev-tools": 2, "finance": 1})

    def test_it_counts_categories_the_index_OMITS(self):
        # kills: filtering to categories that cleared MIN_CATEGORY_PAYEES. The whole
        # point is to answer "why is commerce missing" without two extra crawls -- and
        # the answer is only legible if the under-floor count is recorded. Measured on
        # the shipped corpus: commerce sits at 4, one under the floor of 5.
        counts = CP.category_payee_counts(self.OBS)
        index = CP.build_category_index(self.OBS)   # min_payees=5
        self.assertEqual(index, {})                               # nothing clears it
        self.assertTrue(counts)                                   # yet counts survive

    def test_unclassified_and_malformed_rows_are_skipped_not_crashed_on(self):
        # kills: trusting the observation shape. These rows are assembled from a live
        # third-party crawl joined against the store, so a missing payee or an
        # unclassified category must read as "nothing to count", never as an exception
        # that takes down the refresh.
        obs = self.OBS + [
            {"category": CP.CATEGORY_UNCLASSIFIED, "payee": "0xz"},
            {"category": "dev-tools", "payee": None},
            {"category": None, "payee": "0xy"},
            "not a dict",
        ]
        self.assertEqual(CP.category_payee_counts(obs),
                         {"dev-tools": 2, "finance": 1})
        self.assertEqual(CP.category_payee_counts(None), {})

    def test_the_sidecar_path_is_the_one_payto_baseline_already_defines(self):
        # kills: reintroducing a second sidecar-naming rule here. Two rules that can
        # drift is precisely how a sidecar ends up describing a file it does not sit
        # beside -- the mismatched-pairing defect this repo already shipped once.
        import payto_baseline
        self.assertEqual(payto_baseline.meta_path("data/category_index.json"),
                         "data/category_index.meta.json")
        for p in ("data/category_index.json", "/tmp/x/cat", "a.json.json"):
            self.assertNotEqual(payto_baseline.meta_path(p), p)
        self.assertNotIn("_meta_path", open("category_pricing.py").read())
