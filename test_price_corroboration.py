"""Tests for the price-STOP corroboration gate (A').

Each test states the MUTATION it kills, per repo convention.

WHY THIS GATE EXISTS: `STOP_ANOMALY_RATIO` compares a quote to a median pooled
across ALL of a payee's routes, because a USDC transfer carries no resource label
and backfilled history can never populate the per-class basis
`price_median_and_basis` prefers. Measured on 69 live Base endpoints, that STOP'd
5 legitimate multi-tier sellers. Corroboration removes those without loosening the
threshold -- and, critically, WITHOUT becoming forgeable: the tier arm is
payer-weighted because counting settlements alone can be manufactured with three
self-payments.
"""

import unittest
from decimal import Decimal

import blackwall as bw


def obs(pairs):
    """[(payer, amount)] -> the store's price_observations shape."""
    return [{"payer": p, "amount": str(a)} for p, a in pairs]


class DiscoverPriceTiers(unittest.TestCase):
    def test_separates_a_cheap_and_a_premium_tier(self):
        # MUTATION: returning a single median instead of clusters -> the premium
        # tier is invisible, which is the whole bug this gate addresses.
        tiers = bw.discover_price_tiers(["0.01"] * 200 + ["0.25"] * 4)
        centres = [str(c.normalize()) for c, _n in tiers]
        self.assertIn("0.01", centres)
        self.assertIn("0.25", centres)

    def test_sorted_by_population_descending(self):
        # MUTATION: sorting ascending -> describe_price_tiers leads with the
        # rarest tier, misrepresenting what the payee mostly charges.
        tiers = bw.discover_price_tiers(["0.01"] * 5 + ["0.50"] * 30)
        self.assertEqual(tiers[0][1], 30)

    def test_drops_clusters_below_the_settlement_floor(self):
        # MUTATION: keeping singletons -> one stray settlement becomes a "tier"
        # and corroborates any price.
        self.assertEqual(bw.discover_price_tiers(["0.01"] * 10 + ["9.99"]),
                         bw.discover_price_tiers(["0.01"] * 10))

    def test_ignores_junk_and_nonpositive_prices(self):
        # MUTATION: removing the finite/positive guard -> a None or "abc" in the
        # history raises inside the verdict path.
        tiers = bw.discover_price_tiers(["0.01"] * 4 + [None, "abc", "0", "-5", float("inf")])
        self.assertEqual(len(tiers), 1)

    def test_empty_history_yields_no_tiers(self):
        self.assertEqual(bw.discover_price_tiers([]), [])
        self.assertEqual(bw.discover_price_tiers(None), [])


class TierEvidence(unittest.TestCase):
    def test_counts_settlements_and_distinct_payers_near_the_amount(self):
        # MUTATION: returning only the count -> the wash attack below goes
        # undetected, because count is the forgeable half.
        settlements, payers = bw.tier_evidence(
            "0.25", obs([("a", "0.25"), ("b", "0.24"), ("c", "0.26"), ("d", "0.01")]))
        self.assertEqual((settlements, payers), (3, 3))

    def test_same_payer_repeated_counts_once_as_a_payer(self):
        # MUTATION: counting payers with a list instead of a set -> three
        # self-payments read as three independent payers and forge a tier.
        settlements, payers = bw.tier_evidence(
            "5.00", obs([("wash", "5.00"), ("wash", "5.00"), ("wash", "5.00")]))
        self.assertEqual((settlements, payers), (3, 1))

    def test_payer_matching_is_case_insensitive(self):
        # MUTATION: comparing raw strings -> the same address in checksummed and
        # lowercase form counts twice, halving the cost of forging a tier.
        _s, payers = bw.tier_evidence(
            "5.00", obs([("0xAbC", "5.00"), ("0xabc", "5.00"), ("0xABC", "5.00")]))
        self.assertEqual(payers, 1)

    def test_observations_without_a_payer_never_satisfy_the_payer_floor(self):
        # MUTATION: defaulting a missing payer to a unique value -> unattributed
        # history alone would corroborate any price.
        settlements, payers = bw.tier_evidence(
            "5.00", [{"amount": "5.00"}, {"amount": "5.00"}, {"amount": "5.00"}])
        self.assertEqual((settlements, payers), (3, 0))

    def test_respects_the_tolerance_band(self):
        # MUTATION: widening the band -> a distant price corroborates, which is
        # how a gouge just above a real tier would sneak through.
        self.assertEqual(bw.tier_evidence("1.00", obs([("a", "1.19")]))[0], 1)
        self.assertEqual(bw.tier_evidence("1.00", obs([("a", "1.21")]))[0], 0)

    def test_junk_inputs_return_no_evidence_rather_than_raising(self):
        # MUTATION: dropping the guards -> a malformed record crashes the verdict.
        for bad in (None, "abc", 0, -1, float("nan")):
            self.assertEqual(bw.tier_evidence(bad, obs([("a", "1")])), (0, 0))
        self.assertEqual(bw.tier_evidence("1", ["not-a-dict", None, {"amount": None}]), (0, 0))


class WithinAdvertisedRange(unittest.TestCase):
    def test_true_inside_false_outside(self):
        # MUTATION: inverting the comparison -> the corroboration arm vouches for
        # exactly the prices it should flag.
        self.assertIs(bw.within_advertised_range("0.25", "0.002", "0.65"), True)
        self.assertIs(bw.within_advertised_range("0.09", "0.001", "0.02"), False)

    def test_missing_catalog_is_unknown_not_false(self):
        # MUTATION: returning False when data is absent -> absence of a catalog
        # becomes evidence of wrongdoing, the opposite of fail-open.
        self.assertIsNone(bw.within_advertised_range("1", None, "5"))
        self.assertIsNone(bw.within_advertised_range("1", "0.1", None))

    def test_inverted_bounds_are_tolerated(self):
        # MUTATION: not normalizing -> a source that emits (max, min) makes every
        # quote look out-of-range.
        self.assertIs(bw.within_advertised_range("1", "5", "0.1"), True)

    def test_junk_bounds_are_unknown(self):
        self.assertIsNone(bw.within_advertised_range("1", "abc", "5"))
        self.assertIsNone(bw.within_advertised_range("abc", "1", "5"))


class StopCorroboration(unittest.TestCase):
    HONEST = obs([(f"payer{i}", "0.01") for i in range(200)])

    def test_uncorroborated_gouge_still_stops(self):
        # MUTATION: defaulting to "corroborated" -> the price STOP stops existing.
        stands, _why = bw.price_stop_is_corroborated(
            "5.00", ["0.01"] * 200, self.HONEST, "0.001", "0.02")
        self.assertTrue(stands)

    def test_established_multi_payer_tier_downgrades_the_stop(self):
        # MUTATION: removing the tier arm -> the 5 legitimate multi-tier sellers
        # measured on live Base endpoints go back to STOP.
        real = self.HONEST + obs([("x", "0.25"), ("y", "0.25"), ("z", "0.25")])
        stands, why = bw.price_stop_is_corroborated(
            "0.25", ["0.01"] * 200 + ["0.25"] * 3, real, "0.002", "0.65")
        self.assertFalse(stands)
        self.assertIn("established tier", why)

    def test_wash_forged_tier_does_NOT_downgrade_the_stop(self):
        # THE ATTACK THIS GATE IS BUILT AGAINST. A payee whose real price is $0.01
        # self-pays three times at $5.00 (~$15, recovered on the first victim) to
        # manufacture a tier, then quotes $5.00.
        # MUTATION: dropping MIN_TIER_PAYERS (counting settlements only) -> this
        # returns False and the gouge is waved through.
        washed = self.HONEST + obs([("selfpay", "5.00")] * 3)
        stands, why = bw.price_stop_is_corroborated(
            "5.00", ["0.01"] * 200 + ["5.00"] * 3, washed, "0.001", "0.02")
        self.assertTrue(stands)
        self.assertIn("1 distinct payer", why)

    def test_advertised_range_alone_can_downgrade(self):
        # MUTATION: requiring BOTH arms -> a payee with a legitimate published
        # premium route but little history at it still STOPs.
        stands, why = bw.price_stop_is_corroborated(
            "0.50", ["0.01"] * 200, self.HONEST, "0.01", "3.00")
        self.assertFalse(stands)
        self.assertIn("advertised", why)

    def test_missing_evidence_leaves_the_stop_standing(self):
        # MUTATION: treating absent data as corroboration -> a cold-start payee
        # could gouge freely.
        stands, _why = bw.price_stop_is_corroborated("5.00", [], None, None, None)
        self.assertTrue(stands)

    def test_reason_carries_the_tier_map(self):
        # MUTATION: dropping describe_price_tiers -> the operator sees a bare
        # ratio with no way to tell a premium tier from a gouge.
        _stands, why = bw.price_stop_is_corroborated(
            "5.00", ["0.01"] * 200, self.HONEST, "0.001", "0.02")
        self.assertIn("tiers:", why)


class EndToEndVerdict(unittest.TestCase):
    """The gate must change decide_payment, not just its helpers."""

    def record(self, observations, advertised=None):
        rec = {"settlement_count": 200, "dispute_rate": 0.0, "distinct_payers": 50,
               "price_observations": observations}
        if advertised:
            rec["advertised_min_price"], rec["advertised_max_price"] = advertised
        return rec

    def test_legit_premium_tier_is_HOLD_not_STOP(self):
        # MUTATION: reverting the STOP branch -> a real multi-tier seller is told
        # "do not sign" for charging its own published premium price.
        observations = ([{"payer": f"p{i}", "amount": "0.01"} for i in range(200)]
                        + [{"payer": x, "amount": "0.25"} for x in ("x", "y", "z")])
        out = bw.decide_payment("0.25", self.record(observations, ("0.002", "0.65")),
                                ["0.01"] * 200 + ["0.25"] * 3)
        self.assertEqual(out["verdict"], "HOLD")
        self.assertFalse(out.get("hard_stop"))

    def test_uncorroborated_gouge_is_still_STOP(self):
        # MUTATION: downgrading unconditionally -> the price gate is gone.
        observations = [{"payer": f"p{i}", "amount": "0.01"} for i in range(200)]
        out = bw.decide_payment("5.00", self.record(observations, ("0.001", "0.02")),
                                ["0.01"] * 200)
        self.assertEqual(out["verdict"], "STOP")

    def test_wash_forged_tier_is_still_STOP_end_to_end(self):
        # The regression that matters most: the bypass must not work through the
        # real entry point either.
        observations = ([{"payer": f"p{i}", "amount": "0.01"} for i in range(200)]
                        + [{"payer": "selfpay", "amount": "5.00"}] * 3)
        out = bw.decide_payment("5.00", self.record(observations, ("0.001", "0.02")),
                                ["0.01"] * 200 + ["5.00"] * 3)
        self.assertEqual(out["verdict"], "STOP")

    def test_downgrade_never_reaches_GO(self):
        # MUTATION: downgrading STOP straight to GO -> an 8x+ overcharge would be
        # auto-signed. A corroborated anomaly is still an anomaly.
        observations = ([{"payer": f"p{i}", "amount": "0.01"} for i in range(200)]
                        + [{"payer": x, "amount": "0.25"} for x in ("x", "y", "z")])
        out = bw.decide_payment("0.25", self.record(observations, ("0.002", "0.65")),
                                ["0.01"] * 200 + ["0.25"] * 3)
        self.assertNotEqual(out["verdict"], "GO")

    def test_sanctions_stop_is_untouched_by_corroboration(self):
        # MUTATION: routing every STOP through the price gate -> a sanctioned
        # counterparty with a well-evidenced price would be downgraded. Sanctions
        # are a hard_stop and must be immune.
        observations = [{"payer": f"p{i}", "amount": "0.25"} for i in range(50)]
        rec = self.record(observations, ("0.002", "0.65"))
        rec["sanctioned"] = True
        out = bw.decide_payment("0.25", rec, ["0.25"] * 50)
        self.assertEqual(out["verdict"], "STOP")
        self.assertTrue(out["hard_stop"])


if __name__ == "__main__":
    unittest.main()
