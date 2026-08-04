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
