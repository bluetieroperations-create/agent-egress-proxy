"""Tests for seller_report -- "why agents are not paying you", per seller.

Each test names the MUTATION it kills. Several encode a mistake this module
ACTUALLY made on its first live runs: it mixed two businesses' data into one
report, congratulated a seller on evidence it had just said was absent, and
accused a gift-card merchant of gouging. A diagnostic that is mailed to a third
party has a failure mode ordinary code does not -- being confidently wrong about
someone else -- so the restraint tests here matter as much as the detection ones.
"""

import json
import os
import tempfile
import unittest

import seller_report as sr


# `build_report` records every probe in the reachability ledger, and the ledger's
# DEFAULT_PATH is the OPERATOR's real file. Left alone, running this suite writes
# fixture hosts ("tools.example") into the memory the module exists to be, where
# they then show up in `python reachability_ledger.py`. MEASURED before this
# guard: 5 fixture rows per suite run. Redirect the whole module at a temp file.
_LEDGER_TMP = None


def setUpModule():
    global _LEDGER_TMP
    import reachability_ledger as RL
    _LEDGER_TMP = (RL.DEFAULT_PATH, tempfile.mkdtemp())
    RL.DEFAULT_PATH = os.path.join(_LEDGER_TMP[1], "reach.jsonl")


def tearDownModule():
    import reachability_ledger as RL
    import shutil
    RL.DEFAULT_PATH = _LEDGER_TMP[0]
    shutil.rmtree(_LEDGER_TMP[1], ignore_errors=True)


PAYEE = "0x480cd46e6fade651a0437deadda53d5c8e7d846a"
OTHER = "0x" + "11" * 20


def row(payee=PAYEE, host="api.example.com", settlements=100, payers=10,
        lo="0.001", hi="0.01", category="commerce", **kw):
    r = {"payee": payee, "resources": ["https://%s/a" % host, "https://%s/b" % host],
         "settlement_count": settlements, "distinct_payers": payers,
         "min_price": lo, "max_price": hi, "category": category}
    r.update(kw)
    return r


class TestStaleArtifactRule(unittest.TestCase):
    """Rule 1: never report our own stale artifact as the seller's bug."""

    def test_the_module_never_reads_the_stale_liveness_class(self):
        # Mutation: sourcing parseability from data/liveness.json's `class`
        # field. That survey predates the `payment-required` carrier and still
        # says 86 of 195 hosts serve an unreadable 402; the real figure after
        # that carrier landed is 18 non-answering. Reading it would tell ~68
        # sellers their challenge is broken because OUR parser was incomplete.
        # Checked on CODE, not prose: the module's own docstring explains at
        # length why that field is off-limits, so a naive text search for the
        # word finds the warning rather than the defect.
        with open("seller_report.py", encoding="utf-8") as fh:
            source = fh.read()
        for reading in ('get("class")', "get('class')", '["class"]',
                        'LIVENESS', 'liveness.json"', "liveness.json'"):
            self.assertNotIn(reading, source, reading)
        self.assertFalse([n for n in dir(sr) if "LIVENESS" in n.upper()])

    def test_parseability_requires_a_live_probe(self):
        # Mutation: defaulting to a stored classification when no probe ran.
        row_ = sr.assess_parseability(None)
        self.assertEqual(row_["severity"], sr.UNKNOWN)


class TestReportIsNeverAnInput(unittest.TestCase):
    """Rule 3: a report must never feed the gate that scores the same seller."""

    def test_the_engine_does_not_import_this_module(self):
        # Mutation: importing seller_report from blackwall. A seller could then
        # influence their own verdict through their own report, which turns a
        # diagnostic into a laundering step.
        for path in ("blackwall.py", "x402.py", "ledger.py"):
            with open(path, encoding="utf-8") as fh:
                self.assertNotIn("seller_report", fh.read())


class TestFindRows(unittest.TestCase):
    def test_address_match_ignores_case_on_BOTH_sides(self):
        # Mutation: lowercasing only the query. A live 402 returns EIP-55
        # CHECKSUMMED while our crawl stores lowercase -- the join that silently
        # missed 64 of 69 endpoints in advertised_prices -- and a seller pastes
        # whichever their dashboard shows. So the row side is fixtured
        # checksummed here: with both spellings lowercase, normalizing only the
        # query is indistinguishable from correct.
        checksummed = "0x480CD46E6faDE651a0437DEadDa53d5C8e7D846A"
        rows = [row(payee=checksummed)]
        self.assertEqual(len(sr.find_rows(rows, PAYEE)), 1)
        self.assertEqual(len(sr.find_rows(rows, checksummed)), 1)

    def test_host_match_is_exact_not_substring(self):
        # Mutation: `needle in url`. A substring test makes "api.foo.com" match
        # "api.foo.com.evil.net", putting one seller's findings in another's
        # report -- the same suffix-host bypass x402._is_cdp_host guards against.
        rows = [row(host="api.foo.com.evil.net")]
        self.assertEqual(sr.find_rows(rows, "api.foo.com"), [])
        self.assertEqual(len(sr.find_rows(rows, "api.foo.com.evil.net")), 1)

    def test_no_key_matches_nothing(self):
        self.assertEqual(sr.find_rows([row()], ""), [])
        self.assertEqual(sr.find_rows([row()], None), [])


class TestSubjectSelection(unittest.TestCase):
    """The bug that would have mailed one business another's numbers."""

    # Distinct resource paths per payee: with identical lists, resolving the
    # probe off matches[0] instead of the selected row is indistinguishable from
    # correct, and that is the bug under test.
    MULTI = [dict(row(payee=PAYEE, host="shared.example", settlements=10,
                      payers=99), resources=["https://shared.example/first"]),
             dict(row(payee=OTHER, host="shared.example", settlements=500,
                      payers=1), resources=["https://shared.example/busiest"])]

    def test_the_busiest_payee_is_the_subject(self):
        self.assertEqual(sr.select_subject(self.MULTI)["payee"], OTHER)

    def test_every_resolved_input_uses_the_SELECTED_payee(self):
        # Mutation: resolving the probe / payer graph / category from
        # matches[0] while reporting on max(settlement_count) -- which is
        # exactly what happened. Measured live on blockrun.ai, which carries
        # three payees: one payee's graph ("26 payers corroborated") landed in
        # another's report ("1 distinct payer, possible wash-trading"), two
        # businesses in one document with numbers contradicting each other on
        # the page. Callers now pass FUNCTIONS, so they cannot resolve anything
        # against a different row.
        seen = {}

        def cross_fn(payee):
            seen["cross"] = payee
            return {"distinct_payers": 1, "established_payers": 3}, None

        def probe_fn(resources):
            seen["probe"] = resources
            return None

        report = sr.build_report("shared.example", self.MULTI,
                                 probe_fn=probe_fn, cross_fn=cross_fn,
                                 decide=lambda *a, **k: {"verdict": "GO",
                                                         "reasons": []})
        self.assertEqual(report["payee"], OTHER)
        self.assertEqual(seen["cross"], OTHER)
        self.assertEqual(seen["probe"], self.MULTI[1]["resources"])

    def test_a_shared_host_is_disclosed(self):
        # Mutation: dropping the disclosure. A seller on a shared host would
        # read another payee's numbers as their own with nothing to warn them.
        report = sr.build_report("shared.example", self.MULTI,
                                 decide=lambda *a, **k: {"verdict": "GO",
                                                         "reasons": []})
        codes = [f["code"] for f in report["findings"]]
        self.assertIn("shared_host", codes)

    def test_a_single_payee_host_says_nothing_about_sharing(self):
        report = sr.build_report("api.example.com", [row()],
                                 decide=lambda *a, **k: {"verdict": "GO",
                                                         "reasons": []})
        self.assertNotIn("shared_host", [f["code"] for f in report["findings"]])


class TestSilentIsNotBroken(unittest.TestCase):
    """Rule 2: an absence of evidence is not evidence of a defect."""

    def test_an_unreachable_host_is_unknown_not_a_defect(self):
        # Mutation: grading an unreachable probe BLOCKER. A silent host and a
        # healthy one produce the same absence of findings -- payee_syntax
        # learned this over three probes of the same seller. Calling a quiet
        # host broken is the mistake in the other direction.
        f = sr.assess_reach({"url": "https://x", "error": "timeout"})
        self.assertEqual(f["severity"], sr.UNKNOWN)
        self.assertIn("not a defect", f["detail"])

    def test_an_unknown_seller_is_not_condemned(self):
        # Mutation: reporting "you are undiscoverable" as a blocker. We may
        # simply never have crawled them.
        report = sr.build_report("nobody.example", [])
        self.assertEqual(report["severity"], sr.UNKNOWN)
        self.assertFalse(report["found"])

    def test_unknown_is_not_a_severity_that_sets_the_report(self):
        # Mutation: ranking UNKNOWN above INFO. A report full of "not checked"
        # would then outrank a real clean finding and exit non-zero.
        self.assertLess(sr._RANK[sr.UNKNOWN], sr._RANK[sr.INFO])


class TestParseability(unittest.TestCase):
    def test_an_unreadable_challenge_blocks(self):
        # Mutation: grading it a warning. An agent prices and signs from
        # accepts[]; with none there is literally nothing to pay.
        f = sr.assess_parseability({"url": "https://x", "status": 402,
                                    "body": "{}", "headers": {}})
        self.assertEqual(f["severity"], sr.BLOCKER)

    def test_a_header_only_challenge_is_readable(self):
        # Mutation: parsing the body only. 80 of 195 live hosts serve their
        # requirements ONLY in a `payment-required` header; a body-only reader
        # would tell every one of them their challenge is unreadable.
        import base64
        doc = {"accepts": [{"scheme": "exact", "network": "eip155:8453",
                            "amount": "1000", "asset": "0x" + "22" * 20,
                            "payTo": PAYEE}]}
        b64 = base64.b64encode(json.dumps(doc).encode()).decode()
        f = sr.assess_parseability({"url": "https://x", "status": 402,
                                    "body": "{}",
                                    "headers": {"payment-required": b64}})
        self.assertEqual(f["severity"], sr.INFO)
        self.assertEqual(f["options"], 1)


class TestIdentifierAttribution(unittest.TestCase):
    COVERAGE = {"generated_at": "2026-09-05T00:00:00Z",
                "malformed": [{"asset": "0xdeadbeef", "hosts": ["mine.example"]}],
                "unresolved": [{"asset": "0xfeed", "hosts": ["theirs.example"]}]}

    def test_another_hosts_defect_is_not_reported_as_yours(self):
        # Mutation: reporting every coverage row regardless of host. That puts
        # another seller's broken identifier in this seller's report -- the same
        # false-attribution failure the subject-selection bug caused.
        out = sr.assess_identifiers(self.COVERAGE, ["mine.example"])
        codes = {f["code"] for f in out}
        self.assertIn("asset_id", codes)
        self.assertNotIn("asset_scale", codes)

    def test_an_unattributable_row_is_dropped_not_assigned(self):
        # Mutation: defaulting to "attributable" when no host matches.
        # The census is DATED here on purpose: "we read the census and nothing in
        # it is yours" is INFO, while an absent census is UNKNOWN, and the two
        # must not be confused -- that confusion is the bug the test below pins.
        out = sr.assess_identifiers(
            {"generated_at": "2026-09-05T00:00:00Z",
             "malformed": [{"asset": "0xbad"}]}, ["mine.example"])
        self.assertEqual([f["severity"] for f in out], [sr.INFO])

    def test_a_seller_with_no_known_host_is_attributed_nothing(self):
        # Mutation: `if not hostset: return True`. A corpus row with no
        # resources yields an empty hostset, and attributing on empty would hand
        # that seller EVERY defect in the ecosystem census -- the most extreme
        # form of the false-attribution failure.
        self.assertFalse(sr._mentions_host({"hosts": ["anything.example"]}, []))
        out = sr.assess_identifiers(self.COVERAGE, [])
        self.assertEqual([f["severity"] for f in out], [sr.INFO])

    def test_a_missing_census_is_not_a_clean_bill_of_health(self):
        # Mutation: the shipped-and-fixed bug. `load_json` fails soft to {}, so a
        # missing census reported "your asset identifiers resolve" -- and the
        # DEPLOY IMAGE DID NOT SHIP THE FILE, so the host carrying the one
        # genuinely broken identifier in the corpus would have been told it was
        # fine, on the first page a seller ever sees.
        out = sr.assess_identifiers({}, ["mine.example"])
        self.assertEqual([f["severity"] for f in out], [sr.UNKNOWN])
        self.assertIn("not available", out[0]["detail"])

    def test_the_deploy_image_ships_the_census(self):
        # Mutation: dropping it from the Dockerfile again. The finding above is
        # honest but useless if the artifact never reaches production, and
        # nothing else would notice -- it fails soft by design.
        with open("Dockerfile", encoding="utf-8") as fh:
            dockerfile = fh.read()
        self.assertIn("data/asset_coverage.json", dockerfile)

    def test_the_evidence_carries_the_artifact_date(self):
        # Mutation: dropping generated_at. This report tells a business their
        # endpoint is broken from a dated snapshot; without the date a stale
        # artifact passes as a current fact.
        out = sr.assess_identifiers(self.COVERAGE, ["mine.example"])
        self.assertIn("2026-09-05", out[0]["evidence"])

    def test_an_undated_census_is_labelled_undated_when_it_does_report(self):
        # Mutation: dropping the "undated" label. A census that HAS findings but
        # no date still gets read; the reader must be able to see that its age is
        # unknown rather than assume it is current.
        out = sr.assess_identifiers(
            {"malformed": [{"asset": "0xbad", "hosts": ["mine.example"]}]},
            ["mine.example"])
        self.assertEqual(out[0]["severity"], sr.BLOCKER)
        self.assertIn("undated", out[0]["evidence"])


class TestVerdictFinding(unittest.TestCase):
    def test_the_real_engine_is_used(self):
        # Mutation: reimplementing the gate rules here. The value of this line is
        # that it is the verdict a buyer ACTUALLY gets; a reimplementation would
        # drift from the engine and quietly start lying.
        calls = []

        def decide(amount, record, history, **kw):
            calls.append((amount, record, kw))
            return {"verdict": "HOLD", "reasons": ["thin"]}

        f = sr.assess_verdict(row(lo="0.05"), None, decide=decide)
        self.assertEqual(calls[0][0], "0.05")
        self.assertEqual(calls[0][2]["counterparty"], PAYEE)
        self.assertEqual(f["severity"], sr.WARNING)

    def test_the_scope_of_the_verdict_is_stated(self):
        # Mutation: dropping the scope note. The first live run printed "you
        # advertise an unusable asset identifier" and "a buyer's agent gets GO"
        # side by side; both were true, and together they read as an all-clear.
        # This verdict sees history and price only -- not the identifier, not
        # reachability -- and saying so is what makes the pair coherent.
        f = sr.assess_verdict(row(), None,
                              decide=lambda *a, **k: {"verdict": "GO", "reasons": []})
        self.assertIn("history and price only", f["evidence"])
        self.assertEqual(f["scope"], "reputation_and_price")

    def test_the_cheapest_option_is_scored(self):
        # Mutation: scoring max_price. The hull's top is routinely a legitimate
        # large product, so scoring it makes every catalogue look over-budget and
        # turns the reputation finding into a finding about payment size.
        self.assertEqual(sr.representative_amount(row(lo="0.02", hi="900")),
                         __import__("decimal").Decimal("0.02"))


class TestDemandAuthenticity(unittest.TestCase):
    """The finding a seller cannot get anywhere else -- and the easiest to
    get wrong in a way that insults a legitimate business."""

    def test_zero_corroboration_is_not_reported_as_good_news(self):
        # Mutation: the shipped-and-fixed bug. The engine's sybil flags need a
        # minimum payer count to fire, so a payee with ONE payer tripped neither
        # and fell into the positive branch -- the report congratulated a seller
        # on evidence it had just said was absent ("0 of your payers also pay
        # other known endpoints, which is the hard-to-fake half of a
        # reputation", marked ok).
        f = sr.assess_demand_authenticity(
            row(), {"distinct_payers": 1, "established_payers": 0})
        self.assertEqual(f["severity"], sr.WARNING)
        self.assertIn("not one", f["detail"])

    def test_real_corroboration_is_reported_positively(self):
        f = sr.assess_demand_authenticity(
            row(), {"distinct_payers": 40, "established_payers": 12})
        self.assertEqual(f["severity"], sr.INFO)

    def test_the_engine_flags_get_the_strongest_wording(self):
        f = sr.assess_demand_authenticity(
            row(), {"distinct_payers": 40, "established_payers": 0,
                    "sybil_ring": True})
        self.assertEqual(f["severity"], sr.WARNING)
        self.assertIn("sybil_ring", f["flags"])

    def test_every_tier_quotes_the_market_median_for_context(self):
        # Mutation: dropping the context sentence. "You have 0" is an accusation;
        # "you have 0, the median endpoint has 10, and 11 of 266 are in that
        # position" is a measurement a seller can act on and check.
        for signal in ({"distinct_payers": 1, "established_payers": 0},
                       {"distinct_payers": 40, "established_payers": 12},
                       {"distinct_payers": 40, "established_payers": 0,
                        "sybil_ring": True}):
            f = sr.assess_demand_authenticity(row(), signal)
            self.assertIn(str(sr.CORPUS_MEDIAN_ESTABLISHED), f["detail"])

    def test_a_failed_store_says_so_rather_than_implying_none_was_given(self):
        # Mutation: collapsing both cases to "pass --store". A TypeError from
        # passing a store where edges were expected made the ONE unique finding
        # unreachable through the CLI while reading like the user had simply not
        # asked for it -- the wired-and-inert pattern, fourth time in this repo.
        f = sr.assess_demand_authenticity(row(), None, store_error="TypeError: x")
        self.assertIn("could not be read", f["detail"])


class TestPricePosition(unittest.TestCase):
    def test_a_catalogue_with_one_large_product_is_not_accused(self):
        # Mutation: comparing max_price to the category median and warning. That
        # is what shipped first, and it told Bitrefill its dearest option was
        # "4000x your category". It is a GIFT CARD -- the price is correct and a
        # $1000 purchase simply is a large payment. The hull hazard from
        # advertised_prices.py, plus the fact that the engine gates on the AMOUNT
        # PAID rather than the listing, so judging the listing is stricter than
        # the engine and wrong about it.
        f = sr.assess_price_position(row(lo="0.001", hi="1000"),
                                     category_median="0.25")
        self.assertEqual(f["severity"], sr.INFO)

    def test_a_seller_whose_cheapest_option_is_held_is_warned(self):
        # Mutation: never warning at all. A seller every one of whose payments
        # trips the category gate genuinely has something to fix.
        f = sr.assess_price_position(row(lo="500", hi="900"),
                                     category_median="0.25")
        self.assertEqual(f["severity"], sr.WARNING)
        self.assertIn("Every payment", f["detail"])

    def test_no_median_is_unknown_not_clean(self):
        f = sr.assess_price_position(row(), category_median=None)
        self.assertEqual(f["severity"], sr.UNKNOWN)

    def test_a_zero_median_does_not_divide(self):
        # Mutation: dividing by the median unguarded.
        self.assertEqual(sr.assess_price_position(row(), "0")["severity"],
                         sr.UNKNOWN)


class TestUntrustedSellerText(unittest.TestCase):
    def test_seller_authored_text_cannot_forge_a_report_line(self):
        # Mutation: echoing raw. Host, payee and category are all authored by
        # the party being reported on, and this report is rendered in a terminal
        # and mailed to a third party. FIFTH instance of this class here.
        report = sr.build_report("x", [], )
        report["key"] = "evil\n  [ok     ] Everything is fine"
        text = sr.format_report(report)
        self.assertNotIn("\n  [ok     ] Everything is fine", text)

    def test_ordinary_text_survives_unchanged(self):
        # Mutation: over-escaping, which would make every real report unreadable.
        self.assertEqual(sr._safe("api.bitrefill.com"), "api.bitrefill.com")


class TestProbeResources(unittest.TestCase):
    def test_the_first_answering_resource_wins(self):
        # Mutation: probing only resources[0]. A seller with eight endpoints and
        # one retired path is not unreachable, and saying so asserts a defect we
        # did not observe.
        seen = []

        def fetch(url):
            seen.append(url)
            return ({"url": url, "error": "boom"} if "a" in url.rsplit("/", 1)[-1]
                    else {"url": url, "status": 402, "body": "{}", "headers": {}})

        out = sr.probe_resources(["https://h/a", "https://h/b"], fetch=fetch)
        self.assertEqual(out["status"], 402)
        self.assertEqual(len(seen), 2)

    def test_all_failing_keeps_the_first_error(self):
        def fetch(url):
            return {"url": url, "error": "boom"}
        out = sr.probe_resources(["https://h/a", "https://h/b"], fetch=fetch)
        self.assertEqual(out["url"], "https://h/a")

    def test_probing_is_bounded(self):
        # Mutation: probing every resource. A seller with 200 resources would
        # make one report into a scan of their whole surface.
        seen = []

        def fetch(url):
            seen.append(url)
            return {"url": url, "error": "boom"}
        sr.probe_resources(["https://h/%d" % i for i in range(50)], fetch=fetch)
        # Asserted against a LITERAL, not sr.MAX_PROBES: comparing the behaviour
        # to the constant that controls it is a tautology, and raising the
        # constant would pass unnoticed.
        self.assertLessEqual(len(seen), 5)
        self.assertLessEqual(sr.MAX_PROBES, 5)


class TestExitCodes(unittest.TestCase):
    def test_severity_maps_to_an_actionable_exit(self):
        # Mutation: always exiting 0. A batch run over the corpus would then
        # surface nothing without a human reading every report.
        self.assertEqual(sr.worst([{"severity": sr.INFO},
                                   {"severity": sr.BLOCKER}]), sr.BLOCKER)
        self.assertEqual(sr.worst([]), sr.UNKNOWN)


class TestLiveCorpus(unittest.TestCase):
    """Against the committed artifacts, offline -- the shape a real run takes."""

    def test_a_known_seller_produces_a_full_report(self):
        rows = sr.load_json(sr.DIRECTORY_PATH, [])
        coverage = sr.load_json(sr.COVERAGE_PATH, {})
        index = sr.load_json(sr.CATEGORY_INDEX_PATH, {})
        report = sr.build_report(PAYEE, rows, coverage=coverage,
                                 category_index=index)
        self.assertTrue(report["found"])
        codes = {f["code"] for f in report["findings"]}
        for expected in ("reach", "challenge", "payee", "verdict", "demand",
                         "price"):
            self.assertIn(expected, codes)

    def test_the_known_malformed_asset_is_still_reported(self):
        # The seller found in the wild by asset_coverage. Its Solana payTo was
        # repaired; its 39-hex BSC asset was not, and that is what keeps this
        # host as live proof rather than a fixture.
        rows = sr.load_json(sr.DIRECTORY_PATH, [])
        coverage = sr.load_json(sr.COVERAGE_PATH, {})
        report = sr.build_report("apiwitchcraft.duckdns.org", rows,
                                 coverage=coverage)
        blockers = [f for f in report["findings"] if f["severity"] == sr.BLOCKER]
        self.assertTrue(any(f["code"] == "asset_id" for f in blockers), blockers)




class ResourcesForKey(unittest.TestCase):
    """A host-keyed report may only probe that host (cross-attribution, 3rd time)."""

    ROW = {"payee": "0xAB", "resources": ["https://sat.example/a",
                                          "https://tools.example/b",
                                          "https://tools.example/c"]}

    def test_a_host_key_probes_only_that_host(self):
        # Mutation: probing row["resources"] wholesale. MEASURED on the shipped
        # corpus: 58 of 266 payees are multi-host and EVERY one of them has a
        # host key whose probe would start on a different host -- including
        # payanagent.com -> api.anchor-x402.com, two different businesses behind
        # one payment address.
        self.assertEqual(sr.resources_for_key(self.ROW, "tools.example"),
                         ["https://tools.example/b", "https://tools.example/c"])

    def test_the_sibling_host_is_not_a_fallback(self):
        # Mutation: `scoped + rest` instead of `scoped or resources`. A host
        # whose own resources all fail IS unreachable; answering with a
        # neighbour's success is the bug this exists to stop.
        got = sr.resources_for_key(self.ROW, "sat.example")
        self.assertEqual(got, ["https://sat.example/a"])
        self.assertNotIn("https://tools.example/b", got)

    def test_the_host_match_is_exact_not_a_substring(self):
        # Mutation: `needle in host_of(r)`. The same hazard find_rows documents
        # -- "tools.example" must not select "tools.example.evil.net", or a
        # lookalike host a stranger controls becomes the probe target of the
        # report we publish about the real seller.
        row = {"payee": "0xAB", "resources": ["https://tools.example/b",
                                              "https://tools.example.evil.net/x"]}
        self.assertEqual(sr.resources_for_key(row, "tools.example"),
                         ["https://tools.example/b"])

    def test_a_payee_key_still_covers_every_host(self):
        # Mutation: scoping unconditionally would shrink an address-keyed report
        # to nothing, since no resource host equals the address.
        self.assertEqual(sr.resources_for_key(self.ROW, "0xab"),
                         self.ROW["resources"])

    def test_an_unknown_host_falls_back_rather_than_probing_nothing(self):
        # Mutation: returning [] for a key that matched the row some other way.
        self.assertEqual(sr.resources_for_key(self.ROW, "other.example"),
                         self.ROW["resources"])

    def test_build_report_probes_the_asked_about_host(self):
        # The end-to-end binding: the wiring is what the corpus bug was, not the
        # helper. Mutation: reverting build_report to row["resources"].
        seen = []

        def probe_fn(resources):
            seen.append(list(resources))
            return {"url": resources[0], "status": 200, "accepts": [{"payTo": "0xAB"}]}

        sr.build_report("tools.example", [self.ROW], probe_fn=probe_fn)
        self.assertEqual(seen, [["https://tools.example/b", "https://tools.example/c"]])


if __name__ == "__main__":
    unittest.main()
