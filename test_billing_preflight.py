"""Tests for billing_preflight -- "if I flip billing on, what happens?".

Each test names the MUTATION it kills, per the repo convention. The checks here
are not verdict logic, but they gate a config change that decides whether money
arrives, and every one of them was written because the corresponding failure is
SILENT in production.
"""

import json
import unittest

import billing_preflight as bp

GOOD = "0x480cd46e6fade651a0437deadda53d5c8e7d846a"
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
SOL_ON_BASE = "0x311935cd80b76769bf2ecc9d8ab7635b2139cf82"


def corpus(*pairs):
    """A minimal directory.json-shaped corpus: (min_price, max_price) rows."""
    return [{"payee": "0x%040d" % i, "min_price": str(lo), "max_price": str(hi)}
            for i, (lo, hi) in enumerate(pairs)]


class TestStatusLattice(unittest.TestCase):
    def test_worst_is_a_max_over_severity(self):
        # Mutation: ordering WARN above FAIL, or worst() returning the FIRST
        # status, makes a report with one FAIL exit 0 -- a broken config that
        # reports success, which is the exact failure mode this tool exists for.
        self.assertEqual(bp.worst([bp.OK, bp.FAIL, bp.WARN]), bp.FAIL)
        self.assertEqual(bp.worst([bp.OK, bp.NOTE]), bp.NOTE)
        self.assertEqual(bp.worst([]), bp.OK)

    def test_exit_codes_separate_degraded_from_broken(self):
        # Mutation: mapping WARN to 0 silences the revenue and proportionality
        # findings in a scheduled run, which are the two the tool adds.
        self.assertEqual(bp.EXIT_FOR[bp.OK], 0)
        self.assertEqual(bp.EXIT_FOR[bp.NOTE], 0)
        self.assertEqual(bp.EXIT_FOR[bp.WARN], 1)
        self.assertEqual(bp.EXIT_FOR[bp.FAIL], 2)


class TestPayee(unittest.TestCase):
    def test_clean_address_passes(self):
        self.assertEqual(bp.check_payee(GOOD)["status"], bp.OK)

    def test_glued_env_var_fails(self):
        # Mutation: dropping the payee_syntax layer and keeping only
        # is_evm_address. This is the defect observed IN THE WILD on a live
        # seller -- a .env missing a newline -- and our own deploy reads a .env.
        row = bp.check_payee(GOOD + "FACILITATOR_URL=https://x")
        self.assertEqual(row["status"], bp.FAIL)
        self.assertIn("no address can hold", row["detail"])

    def test_wrong_length_hex_fails(self):
        # Mutation: dropping the is_evm_address layer. payee_syntax deliberately
        # does NOT gate invalid_hex (0 of 292 corpus payees exhibit it), so
        # without this layer a truncated address would pass preflight and then
        # raise inside BillingConfig at container boot.
        self.assertEqual(bp.check_payee("0xdeadbeef")["status"], bp.FAIL)

    def test_missing_pay_to_fails(self):
        # Mutation: returning OK for None -- "billing is off" reported as ready.
        self.assertEqual(bp.check_payee(None)["status"], bp.FAIL)
        self.assertEqual(bp.check_payee("")["status"], bp.FAIL)


class TestAsset(unittest.TestCase):
    def test_base_usdc_is_ok(self):
        self.assertEqual(bp.check_asset(BASE_USDC, "base")["status"], bp.OK)

    def test_unknown_asset_fails(self):
        # Mutation: defaulting unknown decimals to 6. The price would be quoted
        # in units nobody agrees on, and the challenge would still look valid.
        row = bp.check_asset("0x" + "ab" * 20, "base")
        self.assertEqual(row["status"], bp.FAIL)

    def test_non_usd_asset_fails(self):
        # Mutation: checking only decimals and not currency. Every pricing knob
        # (free_below, min_fee, max_fee) is a DOLLAR figure; billing in SOL means
        # "$1.00" is ~$100. Same class as the GUSD/SOL bypass on the buy side.
        row = bp.check_asset(SOL_ON_BASE, "base")
        self.assertEqual(row["status"], bp.FAIL)
        self.assertIn("NOT a US dollar", row["detail"])

    def test_currency_check_precedes_decimals(self):
        # Mutation: ordering the decimals branch first. SOL-on-Base HAS known
        # decimals (9), so a decimals-first check reports only "9 != 6" and the
        # operator fixes the scale and ships a config billing in the wrong money.
        self.assertIn("NOT a US dollar", bp.check_asset(SOL_ON_BASE, "base")["detail"])


class TestPricing(unittest.TestCase):
    def test_flat_zero_price_fails(self):
        # Mutation: allowing price 0. BillingConfig raises "invalid price" at
        # boot -- inside a container, into a log nobody reads.
        self.assertEqual(bp.check_pricing("0")["status"], bp.FAIL)

    def test_flat_overprecise_price_fails(self):
        # Mutation: rounding instead of rejecting. to_atomic refuses more
        # precision than the asset supports; silently rounding would quote a
        # price the operator did not choose.
        self.assertEqual(bp.check_pricing("0.0000001")["status"], bp.FAIL)

    def test_value_pricing_rejects_negative_knob(self):
        # Mutation: swallowing the ValueError. PricingPolicy fails LOUD on a
        # negative knob by design; a preflight that hides it defeats that.
        row = bp.check_pricing("0.001", value_pricing=True,
                               knobs={"bps": "-1"})
        self.assertEqual(row["status"], bp.FAIL)

    def test_modes_are_reported_distinctly(self):
        self.assertEqual(bp.check_pricing("0.001")["mode"], "flat")
        self.assertEqual(
            bp.check_pricing("0.001", value_pricing=True)["mode"], "value")


class TestSelfReportedAmount(unittest.TestCase):
    def test_value_pricing_is_flagged(self):
        # Mutation: dropping this check. The fee under value pricing is derived
        # from payload["amount"], which the CALLER writes and nothing verifies.
        row = bp.check_self_reported_amount("value")
        self.assertEqual(row["status"], bp.NOTE)
        self.assertIn("unverified", row["detail"])

    def test_flat_pricing_is_not_flagged(self):
        # Mutation: flagging unconditionally -- a false finding on the mode that
        # genuinely does not read the field.
        self.assertEqual(bp.check_self_reported_amount("flat")["status"], bp.OK)


class TestCompareAccept(unittest.TestCase):
    EXPECTED = {"scheme": "exact", "network": "eip155:8453", "amount": "1000",
                "asset": BASE_USDC, "payTo": GOOD}

    def test_agreement_has_no_problems(self):
        self.assertEqual(bp.compare_accept(dict(self.EXPECTED), self.EXPECTED), [])

    def test_wrong_payee_is_caught(self):
        # Mutation: comparing only the amount. A challenge advertising a
        # different payTo sends the agent's money to someone else.
        accept = dict(self.EXPECTED, payTo="0x" + "11" * 20)
        self.assertTrue(any("payTo" in p for p in
                            bp.compare_accept(accept, self.EXPECTED)))

    def test_absent_field_is_reported_as_absent(self):
        # Mutation: dropping the `got is None` branch. The comparison then falls
        # through to str(None) != want, which still reports a problem -- so the
        # weaker assertion "some problem mentions asset" does NOT kill it. The
        # distinction matters to whoever reads the report: "asset: absent from
        # the parsed challenge" says a field is MISSING (a protocol-version
        # rename, exactly as maxAmountRequired -> amount was), while
        # "configured X, a client reads 'None'" reads like a value mismatch and
        # sends the operator looking in the wrong place.
        accept = dict(self.EXPECTED)
        accept.pop("asset")
        problems = bp.compare_accept(accept, self.EXPECTED)
        self.assertEqual(problems, ["asset: absent from the parsed challenge"])

    def test_address_case_is_not_a_mismatch(self):
        # Mutation: exact string compare. A live 402 returns EIP-55 checksummed
        # addresses while config carries lowercase -- the same join that missed
        # 64 of 69 endpoints in advertised_prices. A false FAIL here blocks a
        # correct deploy.
        accept = dict(self.EXPECTED, payTo=GOOD.upper().replace("0X", "0x"))
        self.assertEqual(bp.compare_accept(accept, self.EXPECTED), [])

    def test_numeric_amount_equals_string_amount(self):
        # Mutation: comparing types. JSON round-trips ints as ints; an amount of
        # 1000 and "1000" are the same value and must not read as a mismatch.
        accept = dict(self.EXPECTED, amount=1000)
        self.assertEqual(bp.compare_accept(accept, self.EXPECTED), [])


class TestChallengeRoundTrip(unittest.TestCase):
    def _gate(self):
        from x402 import BillingConfig, BillingGate
        return BillingGate(BillingConfig(price="0.001", pay_to=GOOD,
                                         network="base", asset=BASE_USDC))

    def _expected(self, gate):
        from x402 import DEFAULT_SCHEME, to_caip2
        return {"scheme": DEFAULT_SCHEME, "network": to_caip2("base"),
                "asset": BASE_USDC, "payTo": GOOD,
                "amount": str(gate.cfg.price_atomic)}

    def test_our_own_402_round_trips_through_both_carriers(self):
        gate = self._gate()
        row = bp.check_challenge(gate, self._expected(gate))
        self.assertEqual(row["status"], bp.OK, row["detail"])
        # Both carriers must actually have resolved -- not one twice.
        self.assertEqual(len(set(row["carriers"])), 2)

    def test_both_carriers_are_exercised_independently(self):
        # Mutation: parsing the header carrier with the body still present. The
        # body would satisfy accepts_of first and the header path would never
        # run, so a broken header carrier would report OK. 86 of 195 live hosts
        # serve requirements ONLY in a header; that path is not optional.
        gate = self._gate()
        result = gate.check(bp.PREFLIGHT_RESOURCE)
        import base64
        b64 = base64.b64encode(json.dumps(result.body).encode()).decode()
        carriers = bp.roundtrip_carriers(result.body, b64)
        self.assertIsNotNone(carriers["header"][0])
        self.assertNotEqual(carriers["body"][1], carriers["header"][1])

    def test_a_challenge_the_parser_cannot_read_fails(self):
        # Mutation: reporting OK when parse_challenge returns nothing. That is
        # precisely the state this check exists to catch -- well-formed to us,
        # unpayable by a stranger.
        class Broken:
            def check(self, resource, **kw):
                from x402 import BillingResult
                return BillingResult(False, status=402, body={"accepts": []})
        row = bp.check_challenge(Broken(), {})
        self.assertEqual(row["status"], bp.FAIL)

    def test_mismatched_expectation_fails(self):
        # Mutation: building `expected` from the emitted challenge rather than
        # from the config -- the comparison would be a tautology and could never
        # fail.
        gate = self._gate()
        wrong = dict(self._expected(gate), payTo="0x" + "22" * 20)
        self.assertEqual(bp.check_challenge(gate, wrong)["status"], bp.FAIL)

    def test_free_serve_is_reported_not_silently_ok(self):
        # Mutation: treating result.paid as success. Under value pricing a
        # sub-threshold amount is served FREE with no 402 at all; calling that
        # "the challenge is fine" hides that no challenge exists.
        from x402 import BillingConfig, BillingGate, PricingPolicy
        gate = BillingGate(BillingConfig(price="0.001", pay_to=GOOD,
                                         network="base", asset=BASE_USDC),
                           pricing=PricingPolicy())
        row = bp.check_challenge(gate, {}, amount_at_risk="0.10")
        self.assertEqual(row["status"], bp.WARN)
        self.assertTrue(row["free"])


class TestFacilitator(unittest.TestCase):
    def test_no_facilitator_warns_about_the_mock(self):
        # Mutation: reporting OK. BillingGate falls back to MockFacilitator,
        # which reports EVERY payment settled and moves no money -- a public
        # deploy would serve paid verdicts for free while believing it was paid.
        row = bp.check_facilitator(None, "exact", "base")
        self.assertEqual(row["status"], bp.WARN)
        self.assertIn("MOCK", row["detail"])

    def test_unreachable_is_a_warning_not_a_failure(self):
        # Mutation: failing on unreachable. A facilitator can blip; refusing to
        # deploy over a transient network error is its own defect.
        def boom(url, **kw):
            raise OSError("connection reset")
        row = bp.check_facilitator("https://f.example", "exact", "base", fetch=boom)
        self.assertEqual(row["status"], bp.WARN)

    def test_reachable_but_unsupported_is_a_failure(self):
        # Mutation: grading this WARN like the unreachable case. It is a config
        # error that will NEVER work; every payment is rejected in production.
        def fetch(url, **kw):
            return {"kinds": [{"scheme": "exact", "network": "eip155:1"}]}
        row = bp.check_facilitator("https://f.example", "exact", "base", fetch=fetch)
        self.assertEqual(row["status"], bp.FAIL)

    def test_supported_in_caip2_passes(self):
        def fetch(url, **kw):
            return {"kinds": [{"scheme": "exact", "network": "eip155:8453"}]}
        self.assertEqual(
            bp.check_facilitator("https://f.example", "exact", "base",
                                 fetch=fetch)["status"], bp.OK)

    def test_supported_under_the_plain_network_name_passes(self):
        # Mutation: comparing only the CAIP-2 spelling. Real facilitators list
        # "base"/"base-sepolia"; a CAIP-2-only match would FAIL a working config
        # and block a correct deploy.
        def fetch(url, **kw):
            return {"kinds": [{"scheme": "exact", "network": "base"}]}
        self.assertEqual(
            bp.check_facilitator("https://f.example", "exact", "base",
                                 fetch=fetch)["status"], bp.OK)

    def test_probe_url_is_the_supported_route(self):
        # Mutation: probing "/" or "/verify". /verify is side-effecting-adjacent
        # and needs a payload; /supported is the read-only capability document.
        seen = []
        def fetch(url, **kw):
            seen.append(url)
            return {"kinds": []}
        bp.check_facilitator("https://f.example/", "exact", "base", fetch=fetch)
        self.assertEqual(seen, ["https://f.example/supported"])

    def test_junk_supported_document_does_not_raise(self):
        # Mutation: indexing into the response. This reads a THIRD party's
        # document; the shape has already drifted once in this ecosystem.
        for junk in (None, [], {"kinds": "no"}, {"kinds": [1, {"scheme": "x"}]}):
            self.assertEqual(bp.supported_kinds(junk), set())


class TestCdpSelection(unittest.TestCase):
    """With CDP creds the server does NOT use the mock -- nor the URL you set."""

    def test_cdp_creds_are_not_reported_as_the_mock(self):
        # Mutation: keeping the no-URL branch first. An operator with a CORRECT
        # mainnet config (CDP creds, no BLACKWALL_FACILITATOR) would be told
        # their service runs on the mock facilitator, which is a false finding
        # on the one configuration that actually works on Base mainnet.
        row = bp.check_facilitator(None, "exact", "base",
                                   cdp_id="id", cdp_secret="secret")
        self.assertNotIn("MOCK", row["detail"])
        self.assertEqual(row["status"], bp.NOTE)

    def test_a_community_url_alongside_cdp_creds_warns(self):
        # Mutation: reporting NOTE unconditionally. choose_facilitator silently
        # IGNORES a non-CDP URL when CDP creds are present -- correct (it would
        # leak a Bearer JWT) and surprising, so the operator whose stale
        # BLACKWALL_FACILITATOR is doing nothing should be told.
        row = bp.check_facilitator("https://facilitator.x402.rs", "exact", "base",
                                   cdp_id="id", cdp_secret="secret")
        self.assertEqual(row["status"], bp.WARN)
        self.assertIn("IGNORING", row["detail"])

    def test_cdp_does_not_probe_the_network(self):
        # Mutation: probing anyway. /supported on CDP is authenticated; an
        # unauthenticated probe returns an error and would misreport a correct
        # config as unreachable.
        def boom(url, **kw):
            raise AssertionError("network touched on the CDP path")
        bp.check_facilitator(None, "exact", "base", fetch=boom,
                             cdp_id="id", cdp_secret="secret")

    def test_partial_creds_fall_back_to_the_url_path(self):
        # Mutation: `cdp_id or cdp_secret`. choose_facilitator requires BOTH;
        # with one set it uses the URL, and the preflight must model the same.
        def fetch(url, **kw):
            return {"kinds": [{"scheme": "exact", "network": "base"}]}
        row = bp.check_facilitator("https://f.example", "exact", "base",
                                   fetch=fetch, cdp_id="id")
        self.assertEqual(row["status"], bp.OK)


class TestPricePoints(unittest.TestCase):
    def test_inverted_bounds_are_normalized(self):
        # Mutation: trusting the order. A row whose max < min would make the
        # "low" projection the expensive end and invert the whole interval.
        points = bp.price_points([{"payee": "p", "min_price": "5",
                                   "max_price": "1"}])
        self.assertEqual((points[0][1], points[0][2]), (1, 5))

    def test_unparseable_rows_are_dropped_not_zeroed(self):
        # Mutation: coercing bad input to 0. A zero amount is a FREE call under
        # value pricing, so junk rows would silently deflate the projection.
        self.assertEqual(bp.price_points([
            {"min_price": "abc", "max_price": "1"},
            {"min_price": None, "max_price": "1"},
            {"max_price": "1"},
            "not a dict",
        ]), [])

    def test_negative_prices_are_dropped(self):
        self.assertEqual(bp.price_points([{"min_price": "-1", "max_price": "2"}]), [])


class TestProjectRevenue(unittest.TestCase):
    def test_interval_spans_both_ends_of_the_hull(self):
        # Mutation: projecting from min only (or max only). The corpus stores a
        # HULL, not a price list; a point estimate invents a distribution we
        # never measured -- the exact error advertised_prices.py documents.
        from x402 import PricingPolicy
        policy = PricingPolicy()
        proj = bp.project_revenue(policy.fee_atomic, bp.price_points(
            corpus(("0.01", "100"))))
        self.assertEqual(proj["billable_low"], 0)
        self.assertEqual(proj["billable_high"], 1)

    def test_totals_are_human_units_not_atomic(self):
        # Mutation: returning atomic units. A projection is read by a person;
        # "265000" and "0.265" differ by a factor of a million.
        proj = bp.project_revenue(lambda a: 1000, bp.price_points(corpus(("1", "1"))))
        self.assertEqual(proj["low"], "0.001")

    def test_empty_corpus_projects_nothing(self):
        proj = bp.project_revenue(lambda a: 1000, [])
        self.assertEqual((proj["total"], proj["billable_high"]), (0, 0))


class TestRevenueGrading(unittest.TestCase):
    def test_zero_collection_fails(self):
        # Mutation: warning instead of failing. A policy that cannot bill ANY of
        # its addressable market is not a pricing choice; it is an outage that
        # reports success, and it is the finding this whole tool was built for.
        row = bp.check_revenue({"total": 100, "billable_low": 0,
                                "billable_high": 0, "low": "0", "high": "0"})
        self.assertEqual(row["status"], bp.FAIL)

    def test_small_minority_warns(self):
        row = bp.check_revenue({"total": 100, "billable_low": 1,
                                "billable_high": 7, "low": "0", "high": "1"})
        self.assertEqual(row["status"], bp.WARN)

    def test_broad_collection_is_ok(self):
        row = bp.check_revenue({"total": 100, "billable_low": 40,
                                "billable_high": 90, "low": "1", "high": "9"})
        self.assertEqual(row["status"], bp.OK)

    def test_absent_corpus_warns_rather_than_claiming_zero(self):
        # Mutation: treating an unreadable corpus as "bills nobody". Missing
        # evidence is not evidence of nothing -- the same distinction
        # payee_syntax draws between a silent host and a healthy one.
        row = bp.check_revenue({"total": 0})
        self.assertEqual(row["status"], bp.WARN)
        self.assertIn("no corpus", row["detail"])


class TestProportionality(unittest.TestCase):
    def test_flat_pricing_on_a_cheap_market_warns(self):
        # Mutation: dropping this check entirely. THIS IS THE FINDING: the
        # proportionality invariant lives in PricingPolicy, and _price_for
        # consults a policy only when one is configured -- so the SHIPPED
        # DEFAULT (flat) never applies the bound its own module documents as
        # necessary. Measured on the committed corpus: median 20% of the payment.
        points = bp.price_points(corpus(("0.005", "0.01")) * 5)
        row = bp.check_proportionality(lambda a: 1000, points)
        self.assertEqual(row["status"], bp.WARN)
        self.assertIn("flat pricing does not consult it", row["detail"])

    def test_value_pricing_stays_within_the_bound(self):
        # Mutation: measuring the fee before PricingPolicy's ratio cap. The cap
        # returns 0 (free) rather than an over-bound fee, so a correct
        # implementation reports OK here and a broken one warns.
        from x402 import PricingPolicy
        points = bp.price_points(corpus(("0.005", "0.01")) * 5)
        row = bp.check_proportionality(PricingPolicy().fee_atomic, points)
        self.assertEqual(row["status"], bp.OK)

    def test_fee_above_the_whole_payment_fails(self):
        # Mutation: grading this WARN. A fee at or above the payment has no
        # rational buyer; that is broken, not aggressive.
        points = bp.price_points(corpus(("0.0001", "0.0002")) * 3)
        row = bp.check_proportionality(lambda a: 1000, points)
        self.assertEqual(row["status"], bp.FAIL)

    def test_unpayable_is_measured_at_the_dearest_end(self):
        # Mutation: measuring "unpayable" at the CHEAPEST end. Every payee has a
        # cheap option, so that would fail almost any flat config -- an alarm
        # that always fires is not a check. Here the dear end clears the fee, so
        # the verdict is WARN (disproportionate) and not FAIL (unpayable).
        points = bp.price_points(corpus(("0.0001", "1.00")) * 3)
        row = bp.check_proportionality(lambda a: 1000, points)
        self.assertEqual(row["status"], bp.WARN)

    def test_no_corpus_warns_rather_than_passing(self):
        self.assertEqual(bp.check_proportionality(lambda a: 1000, [])["status"],
                         bp.WARN)


class TestPreflight(unittest.TestCase):
    def test_bad_payee_short_circuits_without_a_cascade(self):
        # Mutation: continuing past a failed payee. Every downstream check
        # constructs a BillingConfig, which RAISES on a bad payee -- so either
        # the tool crashes or it buries the one real finding under noise.
        report = bp.preflight("nonsense", offline=True)
        self.assertEqual(report["status"], bp.FAIL)
        self.assertEqual(len(report["checks"]), 1)

    def test_offline_does_not_touch_the_network(self):
        # Mutation: probing anyway. A preflight must be runnable in CI and in a
        # sandbox with no egress.
        def boom(url, **kw):
            raise AssertionError("network touched under --offline")
        report = bp.preflight(GOOD, corpus=corpus(("1", "2")), offline=True,
                              fetch=boom)
        self.assertTrue(any(c["name"] == "facilitator" for c in report["checks"]))

    def test_every_check_appears_once(self):
        # Mutation: appending a check twice, or dropping one from the assembly.
        # A check that is implemented and never called is the wired-and-inert
        # pattern this repo has now hit three times.
        report = bp.preflight(GOOD, corpus=corpus(("1", "2")), offline=True)
        names = [c["name"] for c in report["checks"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), {"payee", "network", "asset", "pricing",
                                      "challenge", "revenue", "proportionality",
                                      "facilitator", "self_reported_amount"})

    def test_status_is_the_worst_check(self):
        def fetch(url, **kw):
            return {"kinds": [{"scheme": "exact", "network": "eip155:1"}]}
        report = bp.preflight(GOOD, facilitator="https://f.example",
                              corpus=corpus(("1", "2")), fetch=fetch)
        self.assertEqual(report["status"], bp.FAIL)

    def test_value_pricing_challenge_quotes_the_value_derived_fee(self):
        # Mutation: probing with amount_at_risk=None. That does NOT hit the free
        # path -- fee_atomic(None) falls back to min_fee -- so the challenge is
        # still emitted and a status-only assertion passes. What changes is WHICH
        # price is round-tripped: the $0.001 floor instead of the $0.10 the
        # policy actually charges on a real payment. Asserting the quoted amount
        # is what makes the value path genuinely exercised.
        from x402 import PricingPolicy
        policy = PricingPolicy()
        report = bp.preflight(GOOD, value_pricing=True,
                              corpus=corpus(("1", "2")), offline=True)
        row = [c for c in report["checks"] if c["name"] == "challenge"][0]
        self.assertEqual(row["status"], bp.OK, row["detail"])
        quoted = row["quoted_amount"]
        self.assertEqual(quoted, str(policy.fee_atomic(str(policy.free_below * 1000))))
        self.assertNotEqual(quoted, str(policy.fee_atomic(None)))


class TestCorpusResolution(unittest.TestCase):
    def test_the_corpus_is_found_from_any_working_directory(self):
        # Mutation: making CORPUS_PATH cwd-relative. The whole test suite runs
        # from the repo root, so both spellings pass there -- and an operator
        # preflighting a deploy runs the script BY PATH from wherever they are.
        # With the relative path that run silently reported "no corpus
        # available" for revenue and proportionality: a missing FILE presenting
        # as a missing FINDING, on the two checks the tool exists for.
        import os
        import tempfile
        here = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                report = bp.preflight(GOOD, offline=True)
        finally:
            os.chdir(here)
        row = [c for c in report["checks"] if c["name"] == "revenue"][0]
        self.assertNotIn("no corpus", row["detail"])
        self.assertGreater(row["total"], 200)

    def test_a_missing_corpus_says_it_is_a_missing_file(self):
        # Mutation: keeping the bare "no corpus available". An operator reading
        # a WARN on the revenue line must not mistake "I could not find the
        # artifact" for "your config collects nothing" -- opposite conclusions.
        row = bp.check_revenue({"total": 0}, source="/nope/directory.json")
        self.assertIn("not a finding about your config", row["detail"])
        self.assertIn("/nope/directory.json", row["detail"])


class TestCommittedCorpus(unittest.TestCase):
    """Tripwire: the shipped numbers must stay reproducible from the artifact.

    A prevalence claim another session cannot reproduce is worth nothing -- the
    same principle test_payee_syntax applies to the 292-payee union and
    test_agentcore_guard applies to the census. If the corpus is refreshed and
    these move, that is a REAL change in the market and a person should look.
    """

    @classmethod
    def setUpClass(cls):
        cls.points = bp.price_points(bp.load_corpus())

    def test_corpus_is_present_and_priced(self):
        self.assertGreater(len(self.points), 200)

    def test_value_pricing_bills_a_small_minority(self):
        from x402 import PricingPolicy
        proj = bp.project_revenue(PricingPolicy().fee_atomic, self.points)
        share = 100.0 * proj["billable_high"] / proj["total"]
        self.assertLess(share, 25.0)
        self.assertEqual(bp.check_revenue(proj)["status"], bp.WARN)

    def test_flat_default_price_is_disproportionate_on_this_market(self):
        from x402 import to_atomic
        flat = to_atomic("0.001", 6)
        row = bp.check_proportionality(lambda a: flat, self.points)
        self.assertEqual(row["status"], bp.WARN)
        # The median fee share at each payee's cheapest advertised option.
        self.assertGreater(float(row["median_ratio_cheapest"]), 0.01)


class TestUntrustedFacilitatorContent(unittest.TestCase):
    """A /supported document is written by a third party we have not paid yet."""

    def test_a_hostile_scheme_cannot_forge_a_report_line(self):
        # Mutation: dropping _safe_text from supported_kinds. A facilitator that
        # returns a scheme containing a newline writes its own line into the
        # report an operator reads before deciding to send it money. FOURTH
        # instance of this class here (payee_syntax hint, approvals decided_by,
        # secret_scan's whole reason for existing).
        def fetch(url, **kw):
            return {"kinds": [{"scheme": "exact\n  overall: OK",
                               "network": "base"}]}
        row = bp.check_facilitator("https://f.example", "exact", "base",
                                   fetch=fetch)
        rendered = bp.format_report({"pay_to": "x", "network": "base",
                                     "checks": [row], "status": row["status"]})
        self.assertNotIn("\n  overall: OK", rendered)
        self.assertIn("\\n", row["detail"])

    def test_a_hostile_scheme_does_not_falsely_match(self):
        # Mutation: sanitizing only at the format site. A scheme crafted to
        # contain our own would then still compare equal somewhere upstream.
        def fetch(url, **kw):
            return {"kinds": [{"scheme": "exact\n", "network": "eip155:8453"}]}
        self.assertEqual(
            bp.check_facilitator("https://f.example", "exact", "base",
                                 fetch=fetch)["status"], bp.FAIL)

    def test_a_legitimate_document_survives_sanitizing(self):
        # Mutation: over-escaping (e.g. quoting everything). A correct
        # facilitator must still match, or the tool blocks working deploys.
        def fetch(url, **kw):
            return {"kinds": [{"scheme": "exact", "network": "base"}]}
        self.assertEqual(
            bp.check_facilitator("https://f.example", "exact", "base",
                                 fetch=fetch)["status"], bp.OK)

    def test_an_enormous_document_is_bounded(self):
        # Mutation: echoing every kind. /supported is a capability list, not a
        # data feed; an unbounded echo turns a report into a denial of reading.
        def fetch(url, **kw):
            return {"kinds": [{"scheme": "s%d" % i, "network": "n%d" % i}
                              for i in range(5000)]}
        row = bp.check_facilitator("https://f.example", "exact", "base",
                                   fetch=fetch)
        self.assertLess(len(row["detail"]), 1200)
        self.assertLessEqual(len(row["kinds"]), 200)

    def test_an_unreachable_error_string_is_escaped(self):
        # Mutation: interpolating the exception raw. urllib puts the SERVER's
        # reason phrase into HTTPError, so that string is untrusted too.
        def boom(url, **kw):
            raise OSError("reset\n  overall: OK")
        row = bp.check_facilitator("https://f.example", "exact", "base",
                                   fetch=boom)
        self.assertNotIn("\n", row["detail"])


class TestNetworkMisconfiguration(unittest.TestCase):
    def test_an_unknown_network_does_not_silently_bill_on_base_mainnet(self):
        # Mutation: trusting default_billing_asset's fallback. It returns Base
        # MAINNET USDC for any unrecognized network -- blackwall.py warns about
        # exactly this at boot. The preflight must not report that config clean.
        report = bp.preflight(GOOD, network="solana", corpus=corpus(("1", "2")),
                              offline=True)
        row = [c for c in report["checks"] if c["name"] == "network"][0]
        self.assertEqual(row["status"], bp.FAIL)
        self.assertEqual(report["status"], bp.FAIL)
        # And the decimals check CANNOT catch it: known_decimals falls back to an
        # address-only table that answers 6 whatever the chain says. This is why
        # coherence is its own check rather than a clause inside check_asset.
        asset_row = [c for c in report["checks"] if c["name"] == "asset"][0]
        self.assertEqual(asset_row["status"], bp.OK)

    def test_evm_asset_on_a_non_evm_caip2_network_fails(self):
        # Mutation: dropping the non-EVM branch of check_network. The `solana`
        # bare-name case is caught by the CAIP-2 resolution branch instead, so it
        # does NOT exercise this one -- an operator who pins a real CAIP-2
        # non-EVM network gets Base mainnet USDC advertised on it.
        row = bp.check_network("solana:mainnet", BASE_USDC)
        self.assertEqual(row["status"], bp.FAIL)
        self.assertIn("not EVM but the billing asset is an EVM", row["detail"])

    def test_non_evm_asset_on_an_evm_network_fails(self):
        # Mutation: dropping the EVM branch. An operator pinning a base58 asset
        # with --asset on Base would quote something no EVM facilitator can move.
        row = bp.check_network("base", "So11111111111111111111111111111111111111112")
        self.assertEqual(row["status"], bp.FAIL)

    def test_a_coherent_pair_passes(self):
        # Restraint control: the correct config must not be condemned.
        self.assertEqual(bp.check_network("base", BASE_USDC)["status"], bp.OK)

    def test_base_sepolia_is_a_supported_testnet_config(self):
        # Mutation: hardcoding mainnet USDC. TESTNET_DRYRUN.md tells the operator
        # to start on Base-Sepolia; a preflight that fails it blocks the
        # documented first step.
        report = bp.preflight(GOOD, network="base-sepolia",
                              corpus=corpus(("1", "2")), offline=True)
        for name in ("network", "asset"):
            row = [c for c in report["checks"] if c["name"] == name][0]
            self.assertNotEqual(row["status"], bp.FAIL, row["detail"])


if __name__ == "__main__":
    unittest.main()
