"""
Tests for payto_baseline.py -- is this the recipient this endpoint has always used?

Each test states the mutation it kills. Pure and network-free: the index is built
from literals or from the COMMITTED corpus, never from a live fetch.
"""
import json
import os
import tempfile
import unittest
from urllib.parse import urlsplit

import payto_baseline as PB

PAYEE_A = "0x480CD46E6faDe651a0437DeaddA53D5c8e7D846A"
PAYEE_B = "0x8AC76a51cc950d9822D68b83fE43AD4843bA77E4"
HOST = "api.example.com"

SINGLE = [{"payee": PAYEE_A.lower(),
           "resources": ["https://api.example.com/v1/quote",
                         "https://api.example.com/v1/other"]}]
MULTI = SINGLE + [{"payee": PAYEE_B.lower(),
                   "resources": ["https://api.example.com/v1/third"]}]


def _index(records):
    return PB.build_payto_index(records)


class TestTheAttackItExistsFor(unittest.TestCase):
    """x402 v2 made payTo dynamic: the server may name a different recipient on
    every request. The price is right; the party is not."""

    def test_the_advertised_payee_is_ok(self):
        # Kills: the whole module inverted.
        got = PB.assess_payto("https://api.example.com/v1/quote", PAYEE_A,
                              _index(SINGLE))
        self.assertEqual(got["grade"], PB.OK)

    def test_a_swapped_payee_on_a_single_payee_host_is_unexpected(self):
        # Kills: the whole module. This IS the v2 recipient-manipulation attack:
        # a host we have only ever seen advertise ONE recipient now names another.
        got = PB.assess_payto("https://api.example.com/v1/quote", PAYEE_B,
                              _index(SINGLE))
        self.assertEqual(got["grade"], PB.UNEXPECTED)

    def test_unexpected_escalates_go_to_hold_when_the_lock_is_on(self):
        # Kills: recording the signal and never acting on it -- the
        # wired-and-inert pattern this repo has hit six times.
        got = PB.apply_payto_baseline(
            {"verdict": "GO", "reasons": [], "signals": {}},
            PB.assess_payto("https://api.example.com/v1/quote", PAYEE_B,
                            _index(SINGLE)),
            gate=True)
        self.assertEqual(got["verdict"], "HOLD")

    def test_the_reason_names_the_endpoint_not_just_the_payee(self):
        # Kills: a bare "unexpected payee". The actionable fact is that THIS HOST
        # has always used a different recipient -- the payee alone does not say that.
        got = PB.assess_payto("https://api.example.com/v1/quote", PAYEE_B,
                              _index(SINGLE))
        self.assertIn(HOST, got["reasons"][0])


class TestTheDefaultIsOff(unittest.TestCase):
    """The reversibility lock, exactly as SYBIL_RING_GATES / EXCESSIVE_GATES /
    ISSUER_TRUST_GATES graduated: measured advisory first, gate second."""

    def test_the_lock_defaults_to_off(self):
        # Kills: shipping it gating. 1.6% is the HOST-level false-flag ceiling;
        # the REQUEST-level rate cannot be derived from the corpus, so the gate
        # has to earn it on real traffic first.
        self.assertFalse(PB.PAYTO_BASELINE_GATES)

    def test_with_the_lock_off_it_records_but_does_not_escalate(self):
        # Kills: hardcoding gate=True in the fold, which would ship an
        # uncalibrated gate to every deploy.
        signal = PB.assess_payto("https://api.example.com/v1/quote", PAYEE_B,
                                 _index(SINGLE))
        got = PB.apply_payto_baseline(
            {"verdict": "GO", "reasons": [], "signals": {}}, signal)
        self.assertEqual(got["verdict"], "GO")
        self.assertEqual(got["signals"]["payto_baseline"]["grade"], PB.UNEXPECTED)

    def test_it_is_still_reported_when_it_does_not_gate(self):
        # Kills: swallowing the finding when the lock is off. Not gating is not
        # the same as not saying -- that is how it gets calibrated.
        signal = PB.assess_payto("https://api.example.com/v1/quote", PAYEE_B,
                                 _index(SINGLE))
        got = PB.apply_payto_baseline(
            {"verdict": "GO", "reasons": [], "signals": {}}, signal)
        self.assertTrue(any("has only ever advertised" in r for r in got["reasons"]))


class TestItNeverManufacturesEvidence(unittest.TestCase):
    """The reachability_ledger rule: our own missing data must never become a
    case against a seller."""

    def test_an_unknown_host_is_unknown_not_unexpected(self):
        # Kills: treating "absent from the corpus" as "wrong". 514 hosts are
        # crawled and the live ecosystem is larger, so most real hosts are
        # absent -- gating on absence would HOLD nearly everything.
        got = PB.assess_payto("https://never-crawled.example/x", PAYEE_A,
                              _index(SINGLE))
        self.assertEqual(got["grade"], PB.UNKNOWN)

    def test_a_host_mapped_to_an_empty_set_is_unknown_not_a_crash(self):
        # Kills: `if advertised is None` in place of `if not advertised`. An
        # empty set cannot come from build_payto_index -- but it CAN come from an
        # injected index, which PayToBaselineSource accepts, and the fall-through
        # reaches the single-element unpack `(only,) = tuple(advertised)` and
        # raises ValueError out of a PURE function documented never to raise.
        got = PB.assess_payto("https://h.example/x", PAYEE_A,
                              {"h.example": frozenset()})
        self.assertEqual(got["grade"], PB.UNKNOWN)

    def test_an_empty_index_is_unknown_for_everything(self):
        # Kills: a missing artifact defaulting to "mismatch". Fail-open.
        got = PB.assess_payto("https://api.example.com/v1/quote", PAYEE_A, {})
        self.assertEqual(got["grade"], PB.UNKNOWN)

    def test_a_missing_resource_is_unknown(self):
        # Kills: deriving a host from nothing. Without a resource there is no
        # endpoint to have a baseline for.
        self.assertEqual(
            PB.assess_payto(None, PAYEE_A, _index(SINGLE))["grade"], PB.UNKNOWN)

    def test_a_relative_resource_is_unknown(self):
        # Kills: reading a path as a host. `/v1/quote` has no netloc.
        self.assertEqual(
            PB.assess_payto("/v1/quote", PAYEE_A, _index(SINGLE))["grade"],
            PB.UNKNOWN)

    def test_a_missing_counterparty_is_unknown(self):
        # Kills: comparing None against the set and calling it a mismatch.
        self.assertEqual(
            PB.assess_payto("https://api.example.com/v1/quote", None,
                            _index(SINGLE))["grade"], PB.UNKNOWN)

    def test_unknown_never_escalates(self):
        # Kills: folding unknown into the gating branch.
        got = PB.apply_payto_baseline(
            {"verdict": "GO", "reasons": [], "signals": {}},
            PB.assess_payto("https://never-crawled.example/x", PAYEE_A,
                            _index(SINGLE)),
            gate=True)
        self.assertEqual(got["verdict"], "GO")


class TestAHostThatRotatesHasNoBaseline(unittest.TestCase):
    """MEASURED: 8 of 514 corpus hosts (1.6%) advertise more than one payTo --
    marketplaces and multi-tenant APIs. For those, "not the one we saw" is
    normal operation, so there is no baseline to violate."""

    def test_a_second_advertised_recipient_is_ok_not_an_attack(self):
        # Kills: treating a marketplace's second recipient as an attack. This is
        # the entire false-flag class the corpus measurement identified.
        # TEST DEFECT CAUGHT ON FIRST RUN: this originally asserted MULTI_PAYEE
        # for PAYEE_B, which the MULTI fixture advertises on that very host -- so
        # it demanded the module report "no baseline" about a recipient the
        # corpus explicitly records. `ok` was right and the assertion was aimed
        # one case to the left of the property, the same shape as the
        # domain-separation test in test_seller_audit.
        got = PB.assess_payto("https://api.example.com/v1/quote", PAYEE_B,
                              _index(MULTI))
        self.assertEqual(got["grade"], PB.OK)

    def test_the_multi_payee_reason_states_how_many_recipients_it_saw(self):
        # Kills: a bare "no baseline". The count is what tells an operator this
        # is a multi-tenant endpoint rather than a parsing failure.
        got = PB.assess_payto("https://api.example.com/v1/quote", "0x" + "c" * 40,
                              _index(MULTI))
        self.assertIn("2 different payment recipients", got["reasons"][0])

    def test_multi_payee_does_not_gate_even_with_the_lock_on(self):
        # Kills: gating it. api.aidress.ai advertises SIX payees; every payment
        # to five of them would be flagged.
        got = PB.apply_payto_baseline(
            {"verdict": "GO", "reasons": [], "signals": {}},
            PB.assess_payto("https://api.example.com/v1/quote", PAYEE_B,
                            _index(MULTI)),
            gate=True)
        self.assertEqual(got["verdict"], "GO")

    def test_an_unseen_payee_on_a_rotating_host_is_still_multi_payee(self):
        # Kills: gating the case that LOOKS most like the attack but is
        # indistinguishable from a marketplace onboarding a new tenant. A host
        # that has demonstrated rotation has no stable recipient to compare to,
        # and declining to judge is the honest answer.
        unseen = "0x" + "c" * 40
        got = PB.assess_payto("https://api.example.com/v1/quote", unseen,
                              _index(MULTI))
        self.assertEqual(got["grade"], PB.MULTI_PAYEE)

    def test_a_known_payee_on_a_rotating_host_is_ok_not_multi_payee(self):
        # Kills: collapsing "rotating host" into one grade regardless of the
        # answer. A recipient we HAVE seen is corroborated whatever else the
        # host advertises.
        got = PB.assess_payto("https://api.example.com/v1/quote", PAYEE_A,
                              _index(MULTI))
        self.assertEqual(got["grade"], PB.OK)


class TestTheJoinKey(unittest.TestCase):
    def test_the_payee_comparison_is_case_insensitive(self):
        # Kills: an exact-match join. A live 402 returns an EIP-55 CHECKSUMMED
        # payTo while the crawl stores lowercase -- the join that silently
        # missed 64 of 69 live endpoints in advertised_prices, and here it would
        # read as an ATTACK rather than as no data.
        got = PB.assess_payto("https://api.example.com/v1/quote",
                              PAYEE_A.upper().replace("0X", "0x"),
                              _index(SINGLE))
        self.assertEqual(got["grade"], PB.OK)

    def test_the_host_comparison_is_case_insensitive(self):
        # Kills: case-sensitive host keys. DNS is case-insensitive.
        got = PB.assess_payto("https://API.EXAMPLE.COM/v1/quote", PAYEE_A,
                              _index(SINGLE))
        self.assertEqual(got["grade"], PB.OK)

    def test_a_lookalike_host_is_not_a_substring_match(self):
        # Kills: substring matching. `seller_report.resources_for_key` learned
        # this exactly -- a lookalike host must not inherit the real one's
        # baseline. Here a substring match would grade the attacker's own
        # domain OK.
        got = PB.assess_payto("https://api.example.com.evil.test/v1/quote",
                              PAYEE_A, _index(SINGLE))
        self.assertEqual(got["grade"], PB.UNKNOWN)

    def test_embedded_credentials_do_not_forge_the_host(self):
        # Kills: reading netloc raw. `https://api.example.com@evil.test/x` reads
        # as the trusted host to a human and resolves to evil.test -- the exact
        # trick seller_portal.safe_probe_url refuses. Taking netloc verbatim
        # would look up the userinfo and hand the attacker's host a baseline.
        got = PB.assess_payto("https://api.example.com@evil.test/x", PAYEE_A,
                              _index(SINGLE))
        self.assertEqual(got["grade"], PB.UNKNOWN)

    def test_a_port_does_not_split_one_hosts_baseline(self):
        # Kills: keying on netloc, which makes `host` and `host:8443` two
        # different hosts and splits one endpoint's baseline in two -- so the
        # same recipient on the same host reads as UNEXPECTED depending on
        # whether the url happened to name a port. A port is a service on the
        # same host, not a different operator.
        records = [{"payee": PAYEE_A.lower(),
                    "resources": ["https://api.example.com:8443/x"]}]
        index = _index(records)
        self.assertEqual(list(index), [HOST])
        self.assertEqual(
            PB.assess_payto("https://api.example.com/x", PAYEE_A, index)["grade"],
            PB.OK)

    def test_a_trailing_dot_is_the_same_host(self):
        # Kills: treating the fully-qualified form as a different host. `a.b.`
        # and `a.b` resolve identically.
        got = PB.assess_payto("https://api.example.com./v1/quote", PAYEE_A,
                              _index(SINGLE))
        self.assertEqual(got["grade"], PB.OK)


class TestTheIndexIsBuiltFromOurOwnCorpus(unittest.TestCase):
    def test_a_record_without_a_payee_is_skipped(self):
        # Kills: indexing None as a payee, which would make every host
        # multi_payee and disable the gate corpus-wide.
        index = _index([{"resources": ["https://api.example.com/x"]}] + SINGLE)
        self.assertEqual(index.get(HOST), frozenset({PAYEE_A.lower()}))

    def test_a_record_without_resources_contributes_no_host(self):
        # Kills: inventing a host key from a payee with no advertised resources.
        self.assertEqual(_index([{"payee": PAYEE_A.lower()}]), {})

    def test_non_dict_records_are_skipped(self):
        # Kills: assuming the artifact's shape. It is refreshed by crawling
        # third parties.
        self.assertEqual(_index(["nonsense", None, 42]), {})

    def test_resources_may_be_dicts_or_strings(self):
        # Kills: handling only one shape. discovery_crawl has written both.
        index = _index([{"payee": PAYEE_A.lower(),
                         "resources": [{"url": "https://api.example.com/x"}]}])
        self.assertEqual(index.get(HOST), frozenset({PAYEE_A.lower()}))

    def test_the_same_payee_on_two_hosts_gives_each_host_a_baseline(self):
        # Kills: keying by payee instead of by host. 58 of 266 corpus payees are
        # multi-host; keying the wrong way round loses the per-endpoint claim
        # that is the whole signal.
        index = _index([{"payee": PAYEE_A.lower(),
                         "resources": ["https://one.example/x",
                                       "https://two.example/y"]}])
        self.assertEqual(index["one.example"], frozenset({PAYEE_A.lower()}))
        self.assertEqual(index["two.example"], frozenset({PAYEE_A.lower()}))

    def test_load_is_fail_open_on_a_missing_file(self):
        # Kills: raising on a missing artifact. A container without the corpus
        # must still serve verdicts.
        self.assertEqual(PB.load_payto_index("/nonexistent/directory.json"), {})

    def test_load_is_fail_open_on_malformed_json(self):
        # Kills: raising on a corrupt artifact.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not json")
            path = fh.name
        try:
            self.assertEqual(PB.load_payto_index(path), {})
        finally:
            os.unlink(path)

    def test_load_reads_a_real_directory_shaped_file(self):
        # Kills: a loader that only works on the in-memory shape.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(SINGLE, fh)
            path = fh.name
        try:
            self.assertEqual(PB.load_payto_index(path).get(HOST),
                             frozenset({PAYEE_A.lower()}))
        finally:
            os.unlink(path)


class TestTheFold(unittest.TestCase):
    def test_it_never_produces_a_stop(self):
        # Kills: promoting this to STOP. It is inference from our own crawl, not
        # proof; sanctions and payload-mismatch keep the STOP authority.
        for grade_signal in (
                PB.assess_payto("https://api.example.com/v1/quote", PAYEE_B,
                                _index(SINGLE)),
                PB.assess_payto("https://api.example.com/v1/quote", PAYEE_B,
                                _index(MULTI))):
            got = PB.apply_payto_baseline(
                {"verdict": "GO", "reasons": [], "signals": {}},
                grade_signal, gate=True)
            self.assertNotEqual(got["verdict"], "STOP")
            self.assertFalse(got.get("hard_stop"))

    def test_it_never_upgrades_a_verdict(self):
        # Kills: letting OK clear an existing HOLD. Monotonically conservative:
        # the recipient being familiar is not evidence the payment is safe.
        got = PB.apply_payto_baseline(
            {"verdict": "HOLD", "reasons": ["prior"], "signals": {}},
            PB.assess_payto("https://api.example.com/v1/quote", PAYEE_A,
                            _index(SINGLE)),
            gate=True)
        self.assertEqual(got["verdict"], "HOLD")

    def test_it_does_not_downgrade_a_stop_to_hold(self):
        # Kills: unconditionally writing HOLD on escalation.
        got = PB.apply_payto_baseline(
            {"verdict": "STOP", "reasons": [], "signals": {}},
            PB.assess_payto("https://api.example.com/v1/quote", PAYEE_B,
                            _index(SINGLE)),
            gate=True)
        self.assertEqual(got["verdict"], "STOP")

    def test_the_fold_is_non_mutating(self):
        # Kills: mutating the caller's verdict in place, which leaks a gate
        # decision into an earlier fold's view of the verdict.
        original = {"verdict": "GO", "reasons": [], "signals": {}}
        PB.apply_payto_baseline(
            original,
            PB.assess_payto("https://api.example.com/v1/quote", PAYEE_B,
                            _index(SINGLE)),
            gate=True)
        self.assertEqual(original["verdict"], "GO")
        self.assertEqual(original["signals"], {})

    def test_a_junk_signal_is_ignored(self):
        # Kills: trusting the signal's shape. This fold is exported.
        for junk in (None, "nonsense", 42, {}, {"grade": "invented"}):
            got = PB.apply_payto_baseline(
                {"verdict": "GO", "reasons": [], "signals": {}}, junk)
            self.assertEqual(got["verdict"], "GO")

    def test_a_junk_verdict_is_returned_unchanged(self):
        # Kills: assuming a dict.
        self.assertEqual(PB.apply_payto_baseline("nonsense", None), "nonsense")

    def test_a_string_reasons_field_is_not_splayed_into_characters(self):
        # Kills: list() on a str, the defect apply_payee_syntax guards against.
        got = PB.apply_payto_baseline(
            {"verdict": "GO", "reasons": "prior", "signals": {}},
            PB.assess_payto("https://api.example.com/v1/quote", PAYEE_B,
                            _index(SINGLE)),
            gate=True)
        self.assertNotIn("p", got["reasons"])


class TestTheEchoIsLogSafe(unittest.TestCase):
    """SEVENTH instance of the untrusted-echo class here. The resource URL is
    harvested from a stranger's own x402 advertisement, and the counterparty is
    merchant-controlled; both land in reasons[], which reaches plain-text logs,
    CLI reports and the seller portal's HTML."""

    def test_an_escape_character_in_the_host_cannot_reach_a_report(self):
        # Kills: echoing the host raw. CORRECTS A WRONG CLAIM IN THIS TEST'S
        # FIRST VERSION, found by mutation testing: it asserted a control
        # character "cannot survive into a host key at all" and only exercised
        # `_safe_text` directly, so deleting the sanitizer from `assess_payto`
        # left every test green. `urlsplit` strips ONLY CR, LF and TAB -- NUL,
        # ESC and DEL pass straight into `.hostname`, so the host sanitizer is
        # LOAD-BEARING rather than defense-in-depth.
        dirty = "ab\x1bm.example"
        self.assertEqual(PB.host_key("https://ab\x1bm.example/x"), dirty)
        index = {dirty: frozenset({PAYEE_A.lower()})}
        got = PB.assess_payto("https://ab\x1bm.example/x", PAYEE_B, index)
        self.assertEqual(got["grade"], PB.UNEXPECTED)   # the join really matches
        self.assertNotIn("\x1b", got["host"])
        for reason in got["reasons"]:
            self.assertNotIn("\x1b", reason)

    def test_a_bracketed_ansi_sequence_is_refused_by_the_parser_not_by_us(self):
        # Kills: crediting this module for a guard it does not own. A COMPLETE
        # ANSI sequence (`\x1b[31m`) contains `[`, which makes urlsplit raise
        # "Invalid IPv6 URL" -- so it is refused by ACCIDENT, one layer down.
        # Stated explicitly because relying on it would be relying on a bracket:
        # a bare ESC has no bracket, still reaches the hostname, and is still
        # enough for some terminal sequences.
        self.assertIsNone(PB.host_key("https://ab\x1b[31m.example/x"))

    def test_a_nul_in_the_host_cannot_reach_a_report(self):
        # Kills: sanitizing only the escape characters a grep would think of.
        dirty = "ab\x00.example"
        index = {dirty: frozenset({PAYEE_A.lower()})}
        got = PB.assess_payto("https://ab\x00.example/x", PAYEE_B, index)
        self.assertEqual(got["grade"], PB.UNEXPECTED)
        self.assertNotIn("\x00", got["host"])

    def test_only_cr_lf_and_tab_are_stripped_by_the_url_parser(self):
        # Kills: the assumption above silently reversing. If a future parser
        # started rejecting or stripping these, the tests above would pass for
        # the wrong reason again.
        self.assertEqual(PB.host_key("https://a\nb.example/x"), "ab.example")
        self.assertEqual(PB.host_key("https://a\rb.example/x"), "ab.example")
        self.assertEqual(PB.host_key("https://a\tb.example/x"), "ab.example")
        self.assertIn("\x1b", PB.host_key("https://a\x1bb.example/x"))

    def test_a_newline_in_the_payee_cannot_forge_a_log_line(self):
        # Kills: echoing the counterparty raw.
        got = PB.assess_payto("https://api.example.com/x",
                              "0xdead\napproved-by: security-team",
                              _index(SINGLE))
        for reason in got["reasons"]:
            self.assertNotIn("\n", reason)

    def test_an_ordinary_host_and_payee_stay_readable(self):
        # Kills: over-escaping into unreadability. The operator has to be able
        # to act on this.
        got = PB.assess_payto("https://api.example.com/v1/quote", PAYEE_B,
                              _index(SINGLE))
        self.assertIn(HOST, got["reasons"][0])
        self.assertIn(PAYEE_B.lower()[:10], got["reasons"][0].lower())


class TestTheSource(unittest.TestCase):
    def test_check_answers_from_the_prebuilt_index(self):
        # Kills: rebuilding or re-fetching on the hot path. The issuer_trust_gate
        # pattern: precompute at boot, O(1) per request.
        source = PB.PayToBaselineSource(index=_index(SINGLE))
        self.assertEqual(
            source.check("https://api.example.com/v1/quote", PAYEE_B)["grade"],
            PB.UNEXPECTED)

    def test_the_source_never_raises(self):
        # Kills: letting a malformed input reach the caller as an exception.
        # This runs on every request.
        source = PB.PayToBaselineSource(index=_index(SINGLE))
        for resource, payee in ((None, None), (42, 42), ({}, []),
                                ("://", "\x00")):
            self.assertIn(source.check(resource, payee)["grade"],
                          (PB.OK, PB.UNEXPECTED, PB.MULTI_PAYEE, PB.UNKNOWN))

    def test_len_reports_the_indexed_host_count(self):
        # Kills: a boot banner that cannot say whether the corpus loaded.
        self.assertEqual(len(PB.PayToBaselineSource(index=_index(SINGLE))), 1)

    def test_the_index_is_never_updated_from_a_request(self):
        # Kills: learning the baseline from traffic, which would let an attacker
        # teach us their own address and then pay it. The index comes from OUR
        # committed crawl only -- the advertised_prices rule.
        source = PB.PayToBaselineSource(index=_index(SINGLE))
        before = dict(source.index)
        source.check("https://brand-new.example/x", PAYEE_B)
        source.check("https://api.example.com/v1/quote", PAYEE_B)
        self.assertEqual(source.index, before)


class TestTheCorpusCalibration(unittest.TestCase):
    """The prevalence claim is COMPUTED from the committed artifact, not
    restated -- payee_syntax's "0 of 558" correction is why."""

    @classmethod
    def setUpClass(cls):
        with open("data/directory.json") as handle:
            cls.records = json.load(handle)
        cls.index = PB.build_payto_index(cls.records)

    def test_the_overwhelming_majority_of_hosts_have_a_single_recipient(self):
        # Kills: the premise. If most hosts rotated recipients there would be no
        # baseline to hold anyone to and the gate would be noise.
        multi = [h for h, s in self.index.items() if len(s) > 1]
        share = len(multi) / len(self.index)
        self.assertLess(share, 0.05, "multi-payee hosts: %.1f%%" % (100 * share))
        self.assertGreater(len(self.index), 400)

    def test_every_advertised_pair_in_the_corpus_grades_ok(self):
        # Kills: a join-key bug. Every (host, payee) the crawl itself recorded
        # must grade OK -- anything else is a false flag on a real seller, and
        # this is the measurement the default-off lock exists to graduate.
        false_flags = []
        for record in self.records:
            payee = (record.get("payee") or "")
            for resource in (record.get("resources") or []):
                url = resource if isinstance(resource, str) else (
                    resource.get("url") or "")
                if not urlsplit(url).netloc:
                    continue
                grade = PB.assess_payto(url, payee, self.index)["grade"]
                if grade not in (PB.OK, PB.MULTI_PAYEE):
                    false_flags.append((url, payee, grade))
        self.assertEqual(false_flags[:5], [], "%d false flags" % len(false_flags))

    def test_a_checksummed_form_of_every_real_payee_still_grades_ok(self):
        # Kills: the EIP-55 join bug at corpus scale. A live 402 returns the
        # checksummed form; if that failed to join, the gate would flag the
        # entire ecosystem as an attack.
        bad = []
        for record in self.records:
            payee = (record.get("payee") or "")
            if not payee.startswith("0x"):
                continue
            for resource in (record.get("resources") or [])[:1]:
                url = resource if isinstance(resource, str) else (
                    resource.get("url") or "")
                if not urlsplit(url).netloc:
                    continue
                grade = PB.assess_payto(url, payee.upper().replace("0X", "0x"),
                                        self.index)["grade"]
                if grade not in (PB.OK, PB.MULTI_PAYEE):
                    bad.append((url, payee, grade))
        self.assertEqual(bad[:5], [])


if __name__ == "__main__":
    unittest.main()


class TestItRefusesToGateOnABaselineItCannotDate(unittest.TestCase):
    """AUDIT FINDING, found by measuring the artifact rather than reading the
    code: `data/directory.json` carries NO timestamp and was last touched 18 days
    before this was written. A seller may legitimately rotate its payout wallet,
    and against a stale baseline that ordinary event is indistinguishable from an
    attacker's swapped recipient -- so the gate would manufacture evidence
    against a seller who did nothing wrong."""

    def test_a_list_shaped_artifact_has_no_knowable_age(self):
        # Kills: inventing an age for the shape the corpus actually has.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(SINGLE, fh)
            path = fh.name
        try:
            self.assertIsNone(PB.index_age_days(path))
        finally:
            os.unlink(path)

    def test_a_dated_artifact_reports_its_age(self):
        # Kills: ignoring generated_at when it IS present, which would leave the
        # gate permanently unreachable.
        import datetime
        stamp = (datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
                 .isoformat().replace("+00:00", "Z"))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"generated_at": stamp, "payees": SINGLE}, fh)
            path = fh.name
        try:
            age = PB.index_age_days(
                path, now=datetime.datetime(2026, 9, 11,
                                            tzinfo=datetime.timezone.utc))
            self.assertAlmostEqual(age, 10.0, places=3)
        finally:
            os.unlink(path)

    def test_the_age_is_not_taken_from_the_file_mtime(self):
        # Kills: `os.path.getmtime`, which is the obvious implementation and is
        # WRONG HERE. A container clones the repo at build time, so every
        # committed artifact's mtime is the BUILD date -- an arbitrarily old
        # corpus would read as minutes old and the staleness guard would confirm
        # freshness it has no evidence for. The `chain_backfill` age_days
        # inversion, exactly.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(SINGLE, fh)          # written NOW, so mtime is fresh
            path = fh.name
        try:
            self.assertIsNone(PB.index_age_days(path))
        finally:
            os.unlink(path)

    def test_an_unknown_age_counts_as_stale(self):
        # Kills: treating unknown as fresh, which gates on a corpus that could be
        # any age -- the safety feature becoming the bug.
        source = PB.PayToBaselineSource(index=_index(SINGLE), age_days=None)
        self.assertTrue(source.stale)

    def test_an_old_baseline_is_stale_and_a_fresh_one_is_not(self):
        # Kills: an inverted or absent comparison.
        self.assertTrue(PB.PayToBaselineSource(
            index=_index(SINGLE),
            age_days=PB.MAX_INDEX_AGE_DAYS + 1).stale)
        self.assertFalse(PB.PayToBaselineSource(
            index=_index(SINGLE), age_days=1.0).stale)

    def test_the_boundary_is_strict(self):
        # Kills: > vs >=. Exactly MAX_INDEX_AGE_DAYS old is still allowed.
        self.assertFalse(PB.PayToBaselineSource(
            index=_index(SINGLE),
            age_days=float(PB.MAX_INDEX_AGE_DAYS)).stale)

    def test_a_stale_baseline_does_not_gate_even_with_the_lock_on(self):
        # Kills: the whole guard. This is the false-HOLD-on-an-innocent-seller
        # case the audit finding is about.
        source = PB.PayToBaselineSource(index=_index(SINGLE), age_days=None)
        got = PB.apply_payto_baseline(
            {"verdict": "GO", "reasons": [], "signals": {}},
            source.check("https://api.example.com/v1/quote", PAYEE_B),
            gate=True)
        self.assertEqual(got["verdict"], "GO")
        self.assertFalse(got["signals"]["payto_baseline"]["gated"])
        self.assertTrue(got["signals"]["payto_baseline"]["stale_baseline"])

    def test_a_fresh_baseline_does_gate_with_the_lock_on(self):
        # Kills: a guard that refuses ALWAYS, which is the same as not shipping
        # the gate -- inert, with a constant that looks like it does something.
        source = PB.PayToBaselineSource(index=_index(SINGLE), age_days=1.0)
        got = PB.apply_payto_baseline(
            {"verdict": "GO", "reasons": [], "signals": {}},
            source.check("https://api.example.com/v1/quote", PAYEE_B),
            gate=True)
        self.assertEqual(got["verdict"], "HOLD")
        self.assertTrue(got["signals"]["payto_baseline"]["gated"])

    def test_a_stale_mismatch_is_still_recorded_and_says_why(self):
        # Kills: swallowing the finding when stale. The mismatches ARE the
        # traffic the lock gets calibrated on, and the operator needs to know the
        # reason it was not acted on is OUR corpus, not the seller.
        source = PB.PayToBaselineSource(index=_index(SINGLE), age_days=None)
        got = source.check("https://api.example.com/v1/quote", PAYEE_B)
        self.assertEqual(got["grade"], PB.UNEXPECTED)
        self.assertTrue(any("undated" in r for r in got["reasons"]))

    def test_the_stale_reason_names_a_known_age_when_there_is_one(self):
        # Kills: reporting "undated" for a corpus whose age we DO know, which
        # sends the operator to fix the wrong thing.
        source = PB.PayToBaselineSource(index=_index(SINGLE), age_days=40.0)
        got = source.check("https://api.example.com/v1/quote", PAYEE_B)
        self.assertTrue(any("40 days old" in r for r in got["reasons"]))

    def test_the_shipped_corpus_is_currently_undated_so_the_gate_is_inert(self):
        # Kills: a future change that dates the artifact without anyone noticing
        # this precondition. When this fails, `ecosystem_scan` has started
        # writing `generated_at` and the lock becomes genuinely reachable --
        # which is the point at which the false-HOLD rate must be measured.
        self.assertIsNone(PB.index_age_days("data/directory.json"))
        self.assertTrue(PB.PayToBaselineSource.from_path("data/directory.json").stale)


class TestTheLiveWire(unittest.TestCase):
    """A REAL server. Everything above is reachable only if it is WIRED.

    Adding a source to this engine takes TEN edits, and the one that matters is
    the `_BoundHandler` DICT LITERAL in `serve_forever`: omit it and the handler
    keeps its `None` class default, the gate never runs, every unit test passes,
    and the boot banner still announces the feature. `test_honeypot`'s parity
    test now asserts every `*_source` default is bound, which covers that edit
    structurally -- but SIX instances of the wired-and-inert pattern in this repo
    were each invisible to unit tests, so the coverage here is a running process.

    It also caught a real defect on the first run: the `configured` dict in the
    handler is `discovery.build_descriptor`'s KWARGS, not a free-form capability
    report, so adding a key there broke `/.well-known/x402` with a TypeError that
    surfaced as a 503.
    """

    @classmethod
    def setUpClass(cls):
        import threading
        import time
        import blackwall
        cls.server = blackwall.BlackwallServer(
            host="127.0.0.1", port=0,
            reputation_source=blackwall.MockReputationSource(),
            payto_source=PB.PayToBaselineSource(index=_index(SINGLE)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        for _ in range(200):
            if getattr(cls.server, "_httpd", None):
                break
            time.sleep(0.02)
        cls.base = "http://127.0.0.1:%d" % cls.server._httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server._httpd.shutdown()
        except Exception:
            pass

    def _forecast(self, counterparty, resource):
        import json as _json
        import urllib.error
        import urllib.request
        body = {"counterparty": counterparty, "amount": "0.01",
                "asset": "USDC", "chain": "base", "resource": resource}
        req = urllib.request.Request(
            self.base + "/v1/forecast-payment", data=_json.dumps(body).encode(),
            headers={"content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, _json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, _json.loads(e.read() or b"{}")

    def test_the_signal_reaches_a_real_verdict(self):
        # Kills: omitting ANY of the ten wiring edits, in particular the
        # _BoundHandler dict literal -- which raises nothing and leaves the gate
        # silently inert while the banner reports it loaded.
        status, body = self._forecast(PAYEE_B, "https://api.example.com/v1/quote")
        self.assertEqual(status, 200)
        self.assertIn("payto_baseline", body.get("signals", {}))
        self.assertEqual(body["signals"]["payto_baseline"]["grade"], PB.UNEXPECTED)

    def test_the_advertised_recipient_grades_ok_over_the_wire(self):
        # Kills: a wiring that always reports a mismatch -- which would be worse
        # than inert, since it would flag every legitimate payment.
        status, body = self._forecast(PAYEE_A, "https://api.example.com/v1/quote")
        self.assertEqual(status, 200)
        self.assertEqual(body["signals"]["payto_baseline"]["grade"], PB.OK)

    def test_it_does_not_gate_over_the_wire_while_the_lock_is_off(self):
        # Kills: shipping the gate on. The signal must be RECORDED and the
        # verdict unchanged until the false-HOLD rate is measured on real traffic.
        _, body = self._forecast(PAYEE_B, "https://api.example.com/v1/quote")
        self.assertFalse(body["signals"]["payto_baseline"]["gated"])
        self.assertTrue(any("has only ever advertised" in r
                            for r in body.get("reasons", [])))

    def test_a_payment_without_a_resource_is_unaffected(self):
        # Kills: requiring a resource, or synthesizing one. Most requests carry
        # none, and those must reach an identical verdict to today's.
        import json as _json
        import urllib.request
        body = {"counterparty": PAYEE_B, "amount": "0.01",
                "asset": "USDC", "chain": "base"}
        req = urllib.request.Request(
            self.base + "/v1/forecast-payment", data=_json.dumps(body).encode(),
            headers={"content-type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            got = _json.loads(r.read())
        self.assertNotIn("payto_baseline", got.get("signals", {}))

    def test_an_uncrawled_host_adds_no_signal_over_the_wire(self):
        # Kills: treating absence as a mismatch on the real path. Most live hosts
        # are absent from a 514-host corpus.
        _, body = self._forecast(PAYEE_B, "https://never-crawled.example/x")
        signal = body.get("signals", {}).get("payto_baseline")
        self.assertTrue(signal is None or signal["grade"] == PB.UNKNOWN)

    def test_the_descriptor_still_serves(self):
        # Kills: breaking /.well-known/x402 by adding a key to the handler's
        # `configured` dict, which is build_descriptor's kwargs. This is how that
        # defect was found -- as a 503, not as a TypeError in a unit test.
        import json as _json
        import urllib.request
        with urllib.request.urlopen(self.base + "/.well-known/x402",
                                    timeout=10) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("x402Version", _json.loads(r.read()))
