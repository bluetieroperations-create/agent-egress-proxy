"""Tests for advertised_prices.py and its wiring.

Each test states the MUTATION it kills, per repo convention.

This source is MONOTONICALLY PERMISSIVE -- it exists only to WITHHOLD a price
STOP -- so most of these tests are about it staying narrow: never inventing a
range, never widening one, never contributing reputation, and never raising.
"""

import json
import os
import tempfile
import unittest

import advertised_prices as ap
import blackwall as bw
from reputation_store import merge_records, production_source


class BuildIndex(unittest.TestCase):
    def test_keys_are_lowercased(self):
        # MUTATION: keying on the raw address -> a live 402 returns an EIP-55
        # CHECKSUMMED payTo while the crawl stores lowercase, so the join misses.
        # Measured at 64 of 69 live endpoints silently answering "no catalog".
        index = ap.build_advertised_index(
            [{"payee": "0xAbCdEf", "min_price": "1", "max_price": "2"}])
        self.assertIn("0xabcdef", index)

    def test_requires_both_bounds(self):
        # MUTATION: defaulting a missing bound -> invents a limit the payee never
        # advertised, and this signal only ever relaxes a STOP.
        for record in ({"payee": "0xa", "min_price": "1"},
                       {"payee": "0xa", "max_price": "2"},
                       {"payee": "0xa"}):
            self.assertEqual(ap.build_advertised_index([record]), {})

    def test_duplicate_payee_narrows_rather_than_widens(self):
        # MUTATION: taking the widest range -> a duplicate entry becomes a way to
        # corroborate more prices. Where the corpus contradicts itself the
        # conservative reading must win.
        index = ap.build_advertised_index([
            {"payee": "0xa", "min_price": "1", "max_price": "100"},
            {"payee": "0xa", "min_price": "5", "max_price": "50"},
        ])
        self.assertEqual((float(index["0xa"][0]), float(index["0xa"][1])), (5.0, 50.0))

    def test_disjoint_duplicate_ranges_yield_no_claim(self):
        # MUTATION: keeping an inverted range -> within_advertised_range()
        # normalizes it back into a WIDE span covering both, the opposite of
        # conservative.
        index = ap.build_advertised_index([
            {"payee": "0xa", "min_price": "1", "max_price": "2"},
            {"payee": "0xa", "min_price": "90", "max_price": "100"},
        ])
        self.assertNotIn("0xa", index)

    def test_rejects_junk_and_negative_prices(self):
        # MUTATION: dropping the validation -> NaN/inf/negative bounds reach the
        # Decimal conversion inside the verdict path.
        for bad in ("abc", None, float("nan"), float("inf"), -1, True):
            self.assertEqual(
                ap.build_advertised_index(
                    [{"payee": "0xa", "min_price": bad, "max_price": "2"}]), {})

    def test_prices_keep_their_original_form(self):
        # MUTATION: storing float(value) -> binary rounding enters a monetary
        # bound before Decimal ever sees it.
        index = ap.build_advertised_index(
            [{"payee": "0xa", "min_price": "0.1", "max_price": "0.3"}])
        self.assertEqual(index["0xa"], ("0.1", "0.3"))

    def test_survives_malformed_records(self):
        self.assertEqual(ap.build_advertised_index(
            [None, "junk", 42, {}, {"payee": None}]), {})
        self.assertEqual(ap.build_advertised_index(None), {})


class LoadIndex(unittest.TestCase):
    def test_missing_or_corrupt_file_is_empty_not_an_exception(self):
        # MUTATION: letting the error propagate -> a missing artifact takes the
        # whole verdict server down at startup.
        self.assertEqual(ap.load_advertised_index("/nonexistent/nope.json"), {})
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("{not json")
        self.addCleanup(os.unlink, handle.name)
        self.assertEqual(ap.load_advertised_index(handle.name), {})

    def test_reads_the_real_committed_artifact(self):
        # Guards the shipped file's shape, not just a fixture.
        self.assertGreater(len(ap.load_advertised_index("data/directory.json")), 100)


class SourceContract(unittest.TestCase):
    INDEX = {"0xa": ("0.01", "3.00")}

    def test_lookup_is_case_insensitive(self):
        self.assertIsNotNone(ap.AdvertisedPriceSource(self.INDEX).lookup("0xA"))

    def test_unknown_payee_returns_none(self):
        self.assertIsNone(ap.AdvertisedPriceSource(self.INDEX).lookup("0xzz"))
        self.assertIsNone(ap.AdvertisedPriceSource(self.INDEX).lookup(None))

    def test_contributes_no_reputation(self):
        # MUTATION: adding settlement_count/distinct_payers here -> a payee could
        # clear the thin-history or Sybil gate purely by being in our crawl.
        record = ap.AdvertisedPriceSource(self.INDEX).lookup("0xa")
        for field in ("settlement_count", "distinct_payers", "price_history",
                      "price_observations", "dispute_rate", "sanctioned"):
            self.assertNotIn(field, record)
        self.assertFalse(record["_meta"]["known"])


class MergeCarriesTheRange(unittest.TestCase):
    def test_merge_preserves_the_pair(self):
        # MUTATION: not adding the fields to merge_records -> CombinedReputationSource
        # silently DROPS them (it builds a fixed dict), and the arm stays inert.
        merged = merge_records([{"settlement_count": 5},
                                {"advertised_min_price": "0.01",
                                 "advertised_max_price": "3"}])
        self.assertEqual(merged["advertised_max_price"], "3")

    def test_never_mixes_bounds_across_sources(self):
        # MUTATION: taking min and max independently -> synthesizes a range NO
        # catalog advertises, in the permissive direction.
        merged = merge_records([{"advertised_min_price": "0.01"},
                                {"advertised_max_price": "9999"}])
        self.assertIsNone(merged["advertised_min_price"])
        self.assertIsNone(merged["advertised_max_price"])

    def test_absent_is_none_not_missing(self):
        self.assertIsNone(merge_records([{"settlement_count": 1}])["advertised_min_price"])


class EndToEnd(unittest.TestCase):
    """The wiring must change a verdict, which is the whole point."""

    OBSERVATIONS = ([{"payer": "0x%040x" % i, "amount": "0.01"} for i in range(50)]
                    + [{"payer": "0xrepeat", "amount": "0.25"}] * 4)
    HISTORY = ["0.01"] * 50 + ["0.25"] * 4

    def record(self, advertised=None):
        rec = {"settlement_count": 54, "dispute_rate": 0.0, "distinct_payers": 50,
               "price_observations": self.OBSERVATIONS}
        if advertised:
            rec["advertised_min_price"], rec["advertised_max_price"] = advertised
        return rec

    def test_single_payer_tier_stops_without_a_catalog(self):
        # The state before this wiring: 4 settlements at $0.25 but all from ONE
        # payer, so the tier arm cannot vouch and the STOP stands.
        out = bw.decide_payment("0.25", self.record(), self.HISTORY)
        self.assertEqual(out["verdict"], "STOP")

    def test_advertised_range_clears_it_to_HOLD(self):
        # MUTATION: not wiring the pair through -> this stays STOP, which is the
        # exact false positive on netintel.dev / agent.kihustle.tech measured on
        # live endpoints.
        out = bw.decide_payment("0.25", self.record(("0.002", "0.65")), self.HISTORY)
        self.assertEqual(out["verdict"], "HOLD")

    def test_cleared_price_still_never_reaches_GO(self):
        # MUTATION: downgrading to GO -> a 25x overcharge is auto-signed because
        # the payee listed the price. Corroboration lowers severity, not the gate.
        out = bw.decide_payment("0.25", self.record(("0.002", "0.65")), self.HISTORY)
        self.assertNotEqual(out["verdict"], "GO")

    def test_quote_outside_the_advertised_range_still_stops(self):
        # MUTATION: treating any catalog as clearance -> a payee escapes a STOP by
        # publishing any range at all.
        out = bw.decide_payment("0.25", self.record(("0.001", "0.02")), self.HISTORY)
        self.assertEqual(out["verdict"], "STOP")

    def test_wash_forged_tier_is_not_rescued_by_a_catalog_it_exceeds(self):
        # The two arms must stay independent: a self-dealt tier plus an
        # out-of-range quote is still a STOP.
        observations = ([{"payer": "0x%040x" % i, "amount": "0.01"} for i in range(50)]
                        + [{"payer": "0xself", "amount": "5.00"}] * 3)
        rec = {"settlement_count": 53, "dispute_rate": 0.0, "distinct_payers": 50,
               "price_observations": observations,
               "advertised_min_price": "0.001", "advertised_max_price": "0.02"}
        out = bw.decide_payment("5.00", rec, ["0.01"] * 50 + ["5.00"] * 3)
        self.assertEqual(out["verdict"], "STOP")


class ProductionWiring(unittest.TestCase):
    def test_production_source_composes_the_advertised_index(self):
        # MUTATION: not appending the source -> the server runs with the arm inert,
        # which is the bug this change fixes.
        with tempfile.TemporaryDirectory() as directory:
            index_path = os.path.join(directory, "dir.json")
            with open(index_path, "w") as handle:
                json.dump([{"payee": "0x" + "a" * 40,
                            "min_price": "0.01", "max_price": "3"}], handle)
            store_path = os.path.join(directory, "s.db")
            source = production_source(store_path, advertised_index=index_path)
            record = source.lookup("0x" + "A" * 40)
            self.assertEqual(record["advertised_max_price"], "3")

    def test_empty_index_is_a_noop_not_a_dead_source(self):
        # MUTATION: appending regardless -> a missing artifact silently changes the
        # source topology (single store -> Combined) for no benefit.
        with tempfile.TemporaryDirectory() as directory:
            store_path = os.path.join(directory, "s.db")
            plain = production_source(store_path)
            composed = production_source(store_path,
                                         advertised_index="/nonexistent/x.json")
            self.assertIs(type(plain), type(composed))


if __name__ == "__main__":
    unittest.main()
