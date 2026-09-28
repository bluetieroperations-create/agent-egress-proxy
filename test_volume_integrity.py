"""
Tests for volume_integrity.py -- the synthetic-volume screen.

Two of these matter more than the rest. `test_single_buyer_is_never_synthetic`
guards the fan-out gate: without it, every ordinary company funding its own
agent wallet scores 3-of-3 and the module starts accusing real businesses.
`test_x402_shape_is_clean` is the control -- the same code returned 0 of 92
payees on the real x402 corpus while flagging ~18% of MPP, and a detector that
fires everywhere is worthless.

Each test names the mutation it kills.
"""
import unittest

import volume_integrity as V


def mpp_cluster(n_buyers=1281, per_buyer=3, price=0.01):
    """The observed MPP shape: one funder -> many wallets -> one sink, one price."""
    buyers = ["0xb%040x" % i for i in range(n_buyers)]
    payee = "0xsink"
    return {
        "payee": payee,
        "amounts": [price] * (n_buyers * per_buyer),
        "buyers": buyers,
        "pays_to": {b: {payee} for b in buyers},
        "funders_of": {b: {"0xfunder": 1} for b in buyers},
    }


def organic(n_buyers=40, payee="0xreal"):
    """Real usage: varied prices, buyers who shop around, mixed funding."""
    buyers = ["0xc%040x" % i for i in range(n_buyers)]
    return {
        "payee": payee,
        "amounts": [0.001 * (1 + i % 37) for i in range(n_buyers * 4)],
        "buyers": buyers,
        "pays_to": {b: {payee, "0xother%d" % (i % 7)} for i, b in enumerate(buyers)},
        "funders_of": {b: {"0xf%d" % (i % 11): 3} for i, b in enumerate(buyers)},
    }


class TestSignals(unittest.TestCase):
    def test_price_uniformity_counts_distinct(self):
        self.assertEqual(V.price_uniformity([0.01] * 50), 1)
        self.assertEqual(V.price_uniformity([0.01, 0.02, 0.01]), 2)

    def test_price_uniformity_ignores_float_noise(self):
        # Mutation: compare raw floats -> a decimal-converted integer amount
        # reads as price variety and the strongest signal never fires.
        self.assertEqual(V.price_uniformity([10000 / 1e6, 0.01, 0.0100000001]), 1)

    def test_exclusivity_is_none_for_no_buyers(self):
        # Mutation: return 0.0 -> "no buyers" reads as "perfectly diverse",
        # which is a clean bill of health for something never examined.
        self.assertIsNone(V.buyer_exclusivity("p", set(), {}))

    def test_exclusivity_fraction(self):
        pays_to = {"a": {"p"}, "b": {"p"}, "c": {"p", "q"}}
        self.assertAlmostEqual(V.buyer_exclusivity("p", {"a", "b", "c"}, pays_to), 2 / 3.0)

    def test_funder_concentration_is_none_without_data(self):
        # Mutation: return 0.0 -> absent funding data counts as evidence of
        # diversity, and `signals_checked` silently lies about what was tested.
        self.assertIsNone(V.funder_concentration({"a", "b"}, {}))

    def test_funder_concentration_excludes_the_payee_itself(self):
        # A payee refunding its own buyers is a different pattern (rebates);
        # counting it as their funder would manufacture the signal.
        f = {"a": {"0xsink": 9, "0xreal": 1}}
        self.assertEqual(V.funder_concentration({"a"}, f, exclude=("0xsink",)), 1.0)

    def test_system_addresses_recognised(self):
        self.assertTrue(V.is_system_address(V.NULL_ADDRESS))
        self.assertTrue(V.is_system_address("0xfeec000000000000000000000000000000000000"))
        self.assertTrue(V.is_system_address("0xdec00000000000000000000000000000000000ff"))
        self.assertFalse(V.is_system_address("0x66fa4d79ca84016b42352be33c908dd812952ec8"))


class TestVerdicts(unittest.TestCase):
    def test_mpp_cluster_is_synthetic(self):
        r = V.screen_payee(**mpp_cluster())
        self.assertEqual(r["verdict"], "synthetic")
        self.assertEqual(r["distinct_amounts"], 1)
        self.assertEqual(r["buyer_exclusivity"], 1.0)
        self.assertEqual(r["funder_concentration"], 1.0)

    def test_organic_traffic_is_clean(self):
        self.assertEqual(V.screen_payee(**organic())["verdict"], "clean")

    def test_single_buyer_is_never_synthetic(self):
        # THE GATE. One buyer, one price, one funder trips every signal -- and it
        # is just a company funding its own agent wallet. Mutation: drop
        # min_buyers -> the screen accuses every ordinary single-customer seller.
        c = mpp_cluster(n_buyers=1, per_buyer=300)
        r = V.screen_payee(**c)
        self.assertEqual(r["verdict"], "unscreenable")
        self.assertIn("fan-out", r["reason"])

    def test_small_buyer_set_is_unscreenable_not_clean(self):
        # Mutation: return "clean" -> an unexamined payee gets a pass, and the
        # share statistic silently treats it as verified-real.
        r = V.screen_payee(**mpp_cluster(n_buyers=V.MIN_BUYERS - 1))
        self.assertEqual(r["verdict"], "unscreenable")

    def test_thin_history_is_unscreenable(self):
        c = mpp_cluster(n_buyers=30, per_buyer=0)
        c["amounts"] = [0.01] * 5
        self.assertEqual(V.screen_payee(**c)["verdict"], "unscreenable")

    def test_varied_prices_are_never_synthetic(self):
        # Mutation: let exclusivity + funding alone convict. Price uniformity is
        # the one signal a targeted backfill cannot fake, so it is REQUIRED --
        # this is exactly the x402 case, where exclusivity ran to 100% on real
        # payees purely because their buyers' other sellers were never crawled.
        c = mpp_cluster()
        c["amounts"] = [0.01 + 0.001 * (i % 5) for i in range(3000)]
        r = V.screen_payee(**c)
        self.assertEqual(r["verdict"], "suspect")
        self.assertNotIn("uniform_price", r["signals_fired"])

    def test_missing_funding_data_still_allows_a_verdict_but_records_it(self):
        # Mutation: require all three signals -> the screen can never convict on
        # a settlement-only corpus, which is most of them.
        c = mpp_cluster(); c["funders_of"] = {}
        r = V.screen_payee(**c)
        self.assertEqual(r["verdict"], "synthetic")
        self.assertNotIn("funder_concentration", r["signals_checked"])
        self.assertIsNone(r["funder_concentration"])

    def test_uniform_price_alone_is_only_suspect(self):
        # A fixed-price API with real, shopping-around buyers is a normal
        # business. Mutation: convict on uniform price alone -> every flat-rate
        # seller on the network is called a fraud.
        c = organic()
        c["amounts"] = [0.01] * 200
        r = V.screen_payee(**c)
        self.assertEqual(r["verdict"], "suspect")
        self.assertEqual(r["signals_fired"], ["uniform_price"])


class TestNetworkLevel(unittest.TestCase):
    def _network(self):
        cluster, real = mpp_cluster(n_buyers=40, per_buyer=10), organic(n_buyers=40)
        amounts = {cluster["payee"]: cluster["amounts"], real["payee"]: real["amounts"]}
        buyers = {cluster["payee"]: cluster["buyers"], real["payee"]: real["buyers"]}
        pays_to = dict(cluster["pays_to"]); pays_to.update(real["pays_to"])
        funders = dict(cluster["funders_of"]); funders.update(real["funders_of"])
        return amounts, buyers, pays_to, funders

    def test_screen_sorts_worst_first(self):
        res = V.screen(*self._network())
        self.assertEqual(res[0]["verdict"], "synthetic")
        self.assertEqual(res[-1]["verdict"], "clean")

    def test_synthetic_share_counts_payments_not_value(self):
        # THE HEADLINE NUMBER. The real MPP cluster moved $38.72 in total while
        # generating ~19% of traffic -- a value-weighted share would have
        # reported ~0% and hidden it completely. Mutation: weight by value.
        res = V.screen(*self._network())
        s = V.synthetic_share(res)
        self.assertEqual(s["synthetic_payments"], 400)
        self.assertGreater(s["share"], 0.5)

    def test_share_of_an_all_clean_network_is_zero(self):
        # The x402 control, in miniature.
        a, b, p, f = self._network()
        del a["0xsink"]; del b["0xsink"]
        self.assertEqual(V.synthetic_share(V.screen(a, b, p, f))["share"], 0.0)

    def test_x402_shape_is_clean(self):
        # The real control: varied prices, high exclusivity from crawl bias.
        # 0 of 92 real x402 payees were flagged; this asserts the shape that
        # produced that result stays clean.
        payee = "0xx402"
        buyers = ["0xd%040x" % i for i in range(60)]
        r = V.screen_payee(payee=payee,
                           amounts=[0.001 * (1 + i % 23) for i in range(300)],
                           buyers=buyers,
                           pays_to={b: {payee} for b in buyers},  # 100% exclusive
                           funders_of=None)
        self.assertEqual(r["verdict"], "suspect")
        self.assertNotIn("uniform_price", r["signals_fired"])

    def test_empty_network_does_not_divide_by_zero(self):
        self.assertEqual(V.synthetic_share([])["share"], 0.0)


if __name__ == "__main__":
    unittest.main()
