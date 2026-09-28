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


class TestPromotionIsAllOrNothing(unittest.TestCase):
    """refresh_seed.sh promotes with a sequence of `mv` under `set -eu`."""

    @staticmethod
    def _precondition_block():
        """The REAL block from the shipped script, not a copy of it."""
        src = open("scripts/refresh_seed.sh").read()
        start = src.index('MISSING=""')
        end = src.index("fi", src.index("exit 1", start)) + len("fi")
        return src[start:end]

    def _run(self, present):
        """Execute the extracted block with a temp dir where `present` files exist."""
        import subprocess, tempfile, os
        with tempfile.TemporaryDirectory() as d:
            names = {"TMP_GZ": "s.gz", "TMP_CAT": "c.json",
                     "TMP_CAT_META": "c.meta.json", "TMP_DIV": "d.json"}
            env = dict(os.environ)
            for var, fn in names.items():
                path = os.path.join(d, fn)
                env[var] = path
                if var in present:
                    open(path, "w").write("x")
            return subprocess.run(["sh", "-eu", "-c", self._precondition_block()],
                                  env=env, capture_output=True, text=True)

    ALL = ("TMP_GZ", "TMP_CAT", "TMP_CAT_META", "TMP_DIV")

    def test_a_missing_candidate_refuses_before_anything_moves(self):
        # AUDIT FIX. Adding a fourth artifact added a fourth way to abort PART WAY THROUGH
        # promotion: mv store, mv index, then mv sidecar fails -> data/ holds a new store
        # and a new index beside a sidecar describing the previous one, and `set -e` stops
        # there. A partially promoted corpus is what the temp-candidate design prevents.
        #
        # EXECUTES the shipped block rather than grepping for it: a first version of this
        # test searched for the string "REFUSING to promote" and a mutation to `if false`
        # left the string in place and walked past.
        for missing in self.ALL:
            r = self._run([v for v in self.ALL if v != missing])
            self.assertNotEqual(r.returncode, 0,
                                "a missing %s must refuse promotion" % missing)
            self.assertIn("REFUSING", r.stderr)

    def test_a_complete_candidate_set_promotes(self):
        # kills: a precondition so strict nothing ever ships -- the failure mode that
        # gets a guard deleted rather than fixed.
        self.assertEqual(self._run(self.ALL).returncode, 0)

    def test_the_precondition_runs_before_the_first_move(self):
        # kills: relocating the check after promotion has begun, which would make it
        # decorative -- the store and index would already be moved.
        src = open("scripts/refresh_seed.sh").read()
        self.assertLess(src.index("REFUSING to promote"), src.index('mv "$TMP_GZ"'))

    def test_the_sidecar_is_promoted_with_the_index_it_describes(self):
        # kills: promoting category_index.json without its sidecar, which would leave the
        # counts describing the previous index -- the mismatched pairing this repo already
        # shipped once as a provenance record claiming 46,031 beside a store holding 67,972.
        src = open("scripts/refresh_seed.sh").read()
        self.assertIn('mv "$TMP_CAT" data/category_index.json', src)
        self.assertIn('mv "$TMP_CAT_META" data/category_index.meta.json', src)


class TestSidecarContentPin(unittest.TestCase):
    """The pin has to be tested where it is WRITTEN, not only where it is shipped.

    A first version of this asserted the sha256 on data/category_index.meta.json, which
    kept passing when the writer stopped emitting one -- the committed file still had the
    key. An untested pin is the pin that silently stops pinning.
    """

    INDEX_TEXT = '{\n  "ai-agents": "0.01",\n  "dev-tools": "0.002"\n}\n'
    COUNTS = {"ai-agents": 26, "dev-tools": 7, "commerce": 4}

    def _meta(self, text=None):
        return CP.sidecar_meta(text if text is not None else self.INDEX_TEXT,
                               self.COUNTS, 5, "2026-09-28T00:00:00Z")

    def test_the_hash_is_over_the_index_bytes(self):
        # kills: dropping the pin, or hashing anything but the index -- an empty digest,
        # a constant, or the counts instead of the file.
        import hashlib
        self.assertEqual(self._meta()["sha256"],
                         hashlib.sha256(self.INDEX_TEXT.encode()).hexdigest())

    def test_a_changed_PRICE_changes_the_pin_though_the_KEYS_are_identical(self):
        # THE point of the whole change, asserted on the sidecar itself. Same two
        # category names, one different price: a key-pinned sidecar cannot tell these
        # apart and would keep describing the wrong index.
        moved = self.INDEX_TEXT.replace('"0.002"', '"0.009"')
        a, b = self._meta(), self._meta(moved)
        self.assertEqual(a["indexed"], b["indexed"])      # keys identical ...
        self.assertNotEqual(a["sha256"], b["sha256"])     # ... pin is not

    def test_the_shipped_sidecar_pins_the_shipped_index(self):
        # kills: promoting a refreshed index without regenerating its sidecar.
        import hashlib, json as _json
        meta = _json.load(open("data/category_index.meta.json"))
        with open("data/category_index.json", "rb") as fh:
            self.assertEqual(meta["sha256"], hashlib.sha256(fh.read()).hexdigest())
