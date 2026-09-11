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
    """With CDP creds the server does NOT use the mock -- nor the URL you set --
    and the authenticated endpoint IS probed.

    A previous version of this class asserted the opposite: `check_facilitator`
    returned NOTE with "/supported is authenticated, so it was NOT probed here",
    and a test named test_cdp_does_not_probe_the_network PINNED that. Measured
    2026-09-07 against a real payout config: invalid CDP credentials PASSED the
    preflight (overall NOTE, exit 1) -- so the single most likely way a mainnet
    deploy fails silently was the one thing the check whose whole job is "what
    happens if I flip billing on?" declined to look at. Authenticated is a
    reason to mint a token, not a reason to skip. The three tests below replace
    the three that encoded the old behaviour.
    """

    KINDS = {"kinds": [{"scheme": "exact", "network": "eip155:8453"}]}

    def _authed(self, result):
        """An injected authenticated fetch; raising is how a caller signals a
        rejected credential or an unreachable endpoint."""
        def fetch(url, key_id, key_secret, **kw):
            if isinstance(result, Exception):
                raise result
            return result
        return fetch

    def test_cdp_creds_are_not_reported_as_the_mock(self):
        # Mutation: keeping the no-URL branch first. An operator with a CORRECT
        # mainnet config (CDP creds, no BLACKWALL_FACILITATOR) would be told
        # their service runs on the mock facilitator, which is a false finding
        # on the one configuration that actually works on Base mainnet.
        row = bp.check_facilitator(None, "exact", "base", cdp_id="id",
                                   cdp_secret="secret",
                                   authed_fetch=self._authed(self.KINDS))
        self.assertNotIn("MOCK", row["detail"])
        self.assertEqual(row["status"], bp.OK)

    def test_rejected_credentials_fail(self):
        # THE POINT OF THIS CLASS. Mutation: grading a rejected credential as
        # WARN, or not probing at all. A 401/403 is not transient -- no amount
        # of waiting fixes a wrong key -- and it presents in production as every
        # settlement failing while the service reports healthy. Verified live
        # against api.cdp.coinbase.com, which 401s a bad token and accepts GET
        # (no header and a garbage bearer both 401, so a 401 with a properly
        # minted JWT really does mean the credential was refused; a POST-only
        # endpoint would answer 405, which routes to the WARN branch below).
        row = bp.check_facilitator(
            None, "exact", "base", cdp_id="id", cdp_secret="secret",
            authed_fetch=self._authed(bp._CredentialsRejected("HTTP 401")))
        self.assertEqual(row["status"], bp.FAIL)
        self.assertIn("rejected the credentials", row["detail"])

    def test_an_unreachable_cdp_endpoint_only_warns(self):
        # Mutation: grading every failure FAIL. A blip at deploy time must not
        # block a correct config -- the same grading the keyless path uses.
        row = bp.check_facilitator(
            None, "exact", "base", cdp_id="id", cdp_secret="secret",
            authed_fetch=self._authed(OSError("timed out")))
        self.assertEqual(row["status"], bp.WARN)
        self.assertIn("NOT confirmed", row["detail"])

    def test_cdp_is_graded_by_the_same_rule_as_the_keyless_path(self):
        # Mutation: a CDP path that reports OK whatever /supported lists. The
        # network check is the reason the keyless facilitator FAILS on mainnet;
        # CDP must not be exempt from it just because it is authenticated.
        row = bp.check_facilitator(
            None, "exact", "base", cdp_id="id", cdp_secret="secret",
            authed_fetch=self._authed(
                {"kinds": [{"scheme": "exact", "network": "base-sepolia"}]}))
        self.assertEqual(row["status"], bp.FAIL)
        self.assertIn("does NOT support", row["detail"])

    def test_a_community_url_alongside_cdp_creds_warns(self):
        # Mutation: reporting OK unconditionally. choose_facilitator silently
        # IGNORES a non-CDP URL when CDP creds are present -- correct (it would
        # leak a Bearer JWT) and surprising, so the operator whose stale
        # BLACKWALL_FACILITATOR is doing nothing should be told, even when the
        # credentials themselves are fine.
        row = bp.check_facilitator("https://facilitator.x402.rs", "exact", "base",
                                   cdp_id="id", cdp_secret="secret",
                                   authed_fetch=self._authed(self.KINDS))
        self.assertEqual(row["status"], bp.WARN)
        self.assertIn("IGNORING", row["detail"])

    def test_the_keyless_fetch_is_never_used_on_the_cdp_path(self):
        # Mutation: probing CDP with the unauthenticated fetch. That returns 401
        # and would misreport a CORRECT config as unreachable -- the original
        # reason the probe was skipped in the first place.
        def boom(url, **kw):
            raise AssertionError("unauthenticated fetch used on the CDP path")
        bp.check_facilitator(None, "exact", "base", fetch=boom, cdp_id="id",
                             cdp_secret="secret",
                             authed_fetch=self._authed(self.KINDS))

    def test_partial_creds_fall_back_to_the_url_path(self):
        # Mutation: `cdp_id or cdp_secret`. choose_facilitator requires BOTH;
        # with one set it uses the URL, and the preflight must model the same.
        def fetch(url, **kw):
            return {"kinds": [{"scheme": "exact", "network": "base"}]}
        row = bp.check_facilitator("https://f.example", "exact", "base",
                                   fetch=fetch, cdp_id="id")
        self.assertEqual(row["status"], bp.OK)


class TestCdpAuthenticatedGet(unittest.TestCase):
    """The HTTP-status -> exception mapping, which decides FAIL vs WARN.

    Reached by no other test (they all inject `authed_fetch`), so two mutations
    survived until this class existed: treating 401 as a generic error, and
    swallowing the rejection entirely. Both turn a wrong CDP key back into a
    WARN an operator ships past.
    """

    def _run(self, raiser):
        import urllib.error
        real_open, real_jwt = urllib.request.urlopen, None
        import cdp_auth
        real_jwt = cdp_auth.build_cdp_jwt
        cdp_auth.build_cdp_jwt = lambda *a, **k: "token"
        urllib.request.urlopen = raiser
        try:
            return bp._cdp_get_json("https://cdp.example/supported", "id", "sec")
        finally:
            urllib.request.urlopen = real_open
            cdp_auth.build_cdp_jwt = real_jwt

    def _http_error(self, code):
        import urllib.error

        def raiser(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "no", {}, None)
        return raiser

    def test_401_is_a_rejected_credential(self):
        # Mutation: `if e.code in (403,)`. 401 is the status CDP actually
        # returns -- verified live against api.cdp.coinbase.com.
        with self.assertRaises(bp._CredentialsRejected):
            self._run(self._http_error(401))

    def test_403_is_a_rejected_credential(self):
        # A key that authenticates but is not enabled for x402.
        with self.assertRaises(bp._CredentialsRejected):
            self._run(self._http_error(403))

    def test_other_statuses_are_not_rejections(self):
        # Mutation: raising _CredentialsRejected for everything. A 405 (the
        # shape a POST-only endpoint would take) or a 429 must reach the WARN
        # branch, not fail a correct config.
        import urllib.error
        for code in (404, 405, 429, 500, 503):
            with self.assertRaises(urllib.error.HTTPError):
                self._run(self._http_error(code))

    def test_the_rejection_is_never_swallowed(self):
        # Mutation: `pass` in the 401 branch, which falls through and returns
        # None -- graded as an empty /supported document, i.e. a WARN.
        try:
            result = self._run(self._http_error(401))
        except bp._CredentialsRejected:
            return
        self.fail("a rejected credential returned %r instead of raising" % result)

    def test_the_request_carries_a_bearer_token(self):
        # Mutation: dropping the Authorization header. The endpoint 401s without
        # it, so every CDP preflight would report rejected credentials.
        seen = {}

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner, n):
                return b'{"kinds": []}'

        def capture(req, timeout=None):
            seen.update(req.headers)
            return _Resp()

        self._run(capture)
        auth = [v for k, v in seen.items() if k.lower() == "authorization"]
        self.assertEqual(auth, ["Bearer token"])


class TestSettlementAuth(unittest.TestCase):
    """POST /verify -- folded from the parallel session's cdp_preflight.py.

    /supported is a READ; settlement is a WRITE. A CDP Secret API Key can carry
    restrictions (the keys already in the operator's project are scoped
    `Portfolio: Primary / Trade - View`), so a key can pass the capability read
    and be refused on the path that moves money. That failure is invisible until
    production, which is the shape of failure this module exists to catch.
    """

    PAYEE = "0x" + "3e" * 20

    def _post(self, result):
        def post(url, body, key_id, key_secret, **kw):
            self.seen = {"url": url, "body": body}
            if isinstance(result, Exception):
                raise result
            return result
        return post

    def test_no_credentials_is_a_note_not_a_finding(self):
        # Mutation: warning when there is nothing to prove. A keyless
        # facilitator has no authentication; reporting that as a problem would
        # put a permanent WARN on a configuration that is simply different.
        row = bp.check_settlement_auth(None, None, "base", self.PAYEE)
        self.assertEqual(row["status"], bp.NOTE)

    def test_a_refused_credential_fails(self):
        # THE POINT. Mutation: grading it WARN, or not probing at all. A wrong
        # or wrongly-scoped key is never transient -- it presents in production
        # as every settlement failing while the service reports healthy.
        row = bp.check_settlement_auth(
            "id", "sec", "base", self.PAYEE,
            authed_post=self._post(bp._CredentialsRejected("HTTP 401")))
        self.assertEqual(row["status"], bp.FAIL)
        self.assertIn("refused the credentials", row["detail"])

    def test_it_names_SCOPE_as_the_likely_cause(self):
        # The actionable half. A key that passes /supported and fails /verify
        # authenticates fine and lacks the x402 permission -- telling the
        # operator to re-check the key id would send them the wrong way.
        row = bp.check_settlement_auth(
            "id", "sec", "base", self.PAYEE,
            authed_post=self._post(bp._CredentialsRejected("HTTP 403")))
        self.assertIn("SCOPED", row["detail"])

    def test_a_validation_error_is_a_PASS(self):
        # Mutation: treating any 4xx as failure. The payload is a DELIBERATE
        # throwaway with an empty `payload`, so a structured x402 validation
        # error is the EXPECTED answer and proves we got past authentication.
        # Failing on it would reject every correct credential.
        row = bp.check_settlement_auth("id", "sec", "base", self.PAYEE,
                                       authed_post=self._post((400, '{"error":"bad"}')))
        self.assertEqual(row["status"], bp.OK)

    def test_an_unreachable_endpoint_only_warns(self):
        # Mutation: grading a blip FAIL. Same restraint the keyless path uses.
        row = bp.check_settlement_auth("id", "sec", "base", self.PAYEE,
                                       authed_post=self._post(OSError("timeout")))
        self.assertEqual(row["status"], bp.WARN)
        self.assertIn("NOT confirmed", row["detail"])

    def test_the_probe_cannot_move_money(self):
        # Mutation: pointing this at /settle. /verify validates and returns;
        # only /settle transfers. A preflight that could spend money is not a
        # preflight. Also pins the empty payload -- a REAL signed authorization
        # here would be a live payment attempt.
        bp.check_settlement_auth("id", "sec", "base", self.PAYEE,
                                 authed_post=self._post((200, "{}")))
        self.assertTrue(self.seen["url"].endswith("/verify"), self.seen["url"])
        self.assertNotIn("settle", self.seen["url"])
        self.assertEqual(self.seen["body"]["paymentPayload"]["payload"], {})

    def test_the_probe_describes_the_payment_we_would_actually_take(self):
        # Mutation: probing a hardcoded network/payee. Authenticating against a
        # tuple we do not serve proves nothing about the one we do.
        bp.check_settlement_auth("id", "sec", "base", self.PAYEE,
                                 authed_post=self._post((200, "{}")))
        body = self.seen["body"]
        self.assertEqual(body["paymentPayload"]["network"], "eip155:8453")
        # BOTH halves. A first mutation pass changed only the REQUIREMENTS'
        # network to base-sepolia and survived, because this asserted the
        # payload's network alone -- and the requirements are the half that
        # describes what we would actually charge for.
        req = body["paymentRequirements"]
        self.assertEqual(req["network"], "eip155:8453")
        self.assertEqual(req["payTo"], self.PAYEE)
        self.assertEqual(req["scheme"], "exact")


class TestCdpAuthenticatedPost(unittest.TestCase):
    """The POST status mapping, which decides FAIL vs PASS on /verify.

    Reached by no other test -- they all inject `authed_post` -- so a mutation
    treating EVERY 4xx as a refused credential survived until this existed. That
    mutation rejects every correct credential, because the deliberate throwaway
    payload is SUPPOSED to come back as a 400-class x402 validation error.
    """

    def _run(self, raiser):
        import cdp_auth
        import urllib.request
        real_open, real_jwt = urllib.request.urlopen, cdp_auth.build_cdp_jwt
        cdp_auth.build_cdp_jwt = lambda *a, **k: "token"
        urllib.request.urlopen = raiser
        try:
            return bp._cdp_post_json("https://cdp.example/verify", {"a": 1},
                                     "id", "sec")
        finally:
            urllib.request.urlopen = real_open
            cdp_auth.build_cdp_jwt = real_jwt

    def _http_error(self, code):
        import urllib.error

        class _E(urllib.error.HTTPError):  # noqa: N801
            def read(self_inner, *a):
                return b'{"error":"x402 validation"}'

        def raiser(req, timeout=None):
            raise _E(req.full_url, code, "no", {}, None)
        return raiser

    def test_401_and_403_are_refused_credentials(self):
        for code in (401, 403):
            with self.assertRaises(bp._CredentialsRejected):
                self._run(self._http_error(code))

    def test_a_validation_error_is_RETURNED_not_raised(self):
        # THE MUTATION THAT SURVIVED FIRST: raising on every HTTPError. The
        # probe sends an empty payload ON PURPOSE, so a 400-class x402
        # validation error is the expected answer and PROVES auth succeeded.
        # Raising there reports a perfectly good credential as refused.
        status, body = self._run(self._http_error(400))
        self.assertEqual(status, 400)
        self.assertIn("validation", body)

    def test_a_server_error_is_returned_too(self):
        # A 500 is the facilitator's problem, not our credential's.
        status, _ = self._run(self._http_error(503))
        self.assertEqual(status, 503)

    def test_the_request_is_a_POST_carrying_the_bearer(self):
        # Mutation: dropping the Authorization header, or the method. CDP 401s
        # without the header, so every probe would report a refused credential.
        seen = {}

        class _Resp:
            status = 200

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner, n):
                return b"{}"

        def capture(req, timeout=None):
            seen["method"] = req.get_method()
            seen.update(req.headers)
            return _Resp()

        self._run(capture)
        self.assertEqual(seen["method"], "POST")
        auth = [v for k, v in seen.items() if k.lower() == "authorization"]
        self.assertEqual(auth, ["Bearer token"])


class TestSupportedVersions(unittest.TestCase):
    """x402Version discrimination -- the catch `supported_kinds` cannot make.

    `supported_kinds` reduces a /supported doc to (scheme, network) and DROPS
    `x402Version`. A facilitator settling Base-mainnet `exact` at v1 only, while
    our 402 advertises v2, matches on that pair and would be graded OK -- and the
    real paid call is rejected as an unsupported kind. Measured live 2026-09-11
    on facilitator.x402.rs: all 31 entries state a version (5 v1, 26 v2), so the
    field is populated in the wild.
    """

    def _doc(self, *entries):
        return {"kinds": list(entries)}

    def test_a_version_mismatch_fails(self):
        # Mutation: ignoring x402Version, which is what the code did before this
        # was folded in from the parallel session's cdp_preflight.py.
        doc = self._doc({"scheme": "exact", "network": "eip155:8453",
                         "x402Version": 1})
        row = bp._grade_kinds("https://f.example", doc, "exact", "base",
                              want_version=2)
        self.assertEqual(row["status"], bp.FAIL)
        self.assertIn("x402 v1", row["detail"])

    def test_a_matching_version_passes(self):
        doc = self._doc({"scheme": "exact", "network": "eip155:8453",
                         "x402Version": 2})
        row = bp._grade_kinds("https://f.example", doc, "exact", "base",
                              want_version=2)
        self.assertEqual(row["status"], bp.OK)

    def test_one_matching_version_among_several_passes(self):
        # Mutation: requiring the ONLY version to be ours. A facilitator listing
        # both v1 and v2 for our tuple settles ours fine.
        doc = self._doc({"scheme": "exact", "network": "eip155:8453",
                         "x402Version": 1},
                        {"scheme": "exact", "network": "eip155:8453",
                         "x402Version": 2})
        row = bp._grade_kinds("https://f.example", doc, "exact", "base",
                              want_version=2)
        self.assertEqual(row["status"], bp.OK)

    def test_an_unstated_version_is_no_opinion_not_a_mismatch(self):
        # Mutation: treating absent as wrong. Many facilitators omit the field
        # entirely; failing them for it would reject correct configurations --
        # the fail-open discipline every gate in this repo follows.
        doc = self._doc({"scheme": "exact", "network": "eip155:8453"})
        row = bp._grade_kinds("https://f.example", doc, "exact", "base",
                              want_version=2)
        self.assertEqual(row["status"], bp.OK)
        self.assertEqual(row["versions"], [])

    def test_versions_of_a_DIFFERENT_kind_are_not_consulted(self):
        # Mutation: collecting every version in the document. A v1 entry for a
        # different network -- or, the case that survived a first mutation pass,
        # a different SCHEME on the SAME network -- says nothing about our
        # tuple, and counting it would fail a facilitator that supports us
        # perfectly. Both filters are exercised, because dropping either one
        # alone must be caught.
        doc = self._doc({"scheme": "exact", "network": "eip155:8453",
                         "x402Version": 2},
                        {"scheme": "exact", "network": "solana-devnet",
                         "x402Version": 1},
                        {"scheme": "upto", "network": "eip155:8453",
                         "x402Version": 1})
        self.assertEqual(bp.kind_versions(doc, "exact", "eip155:8453"), {2})
        row = bp._grade_kinds("https://f.example", doc, "exact", "base",
                              want_version=2)
        self.assertEqual(row["status"], bp.OK)

    def test_the_caip2_spelling_is_consulted_before_the_legacy_one(self):
        # MEASURED LIVE 2026-09-11 and nearly a false finding: facilitator.x402.rs
        # lists the SAME chain under BOTH spellings at DIFFERENT versions --
        # `exact/base-sepolia` at v1 and `exact/eip155:84532` at v2. We advertise
        # the CAIP-2 id, so the versions stated for THAT spelling are the ones
        # that bind; reading the legacy row would fail a facilitator that
        # genuinely settles what we quote.
        #
        # Mutation: iterating (network, caip2) instead of (caip2, network), or
        # merging versions across both spellings -- which is what the parallel
        # session's `_is_base_mainnet` does, and why this fold is not a copy.
        doc = self._doc({"scheme": "exact", "network": "base-sepolia",
                         "x402Version": 1},
                        {"scheme": "exact", "network": "eip155:84532",
                         "x402Version": 2})
        row = bp._grade_kinds("https://f.example", doc, "exact", "base-sepolia",
                              want_version=2)
        self.assertEqual(row["status"], bp.OK)
        self.assertEqual(row["versions"], [2])

    def test_a_legacy_only_listing_at_the_wrong_version_still_fails(self):
        # The other half: no CAIP-2 twin to rescue it. Verified live against
        # facilitator.x402.rs on `solana-devnet` (exact/v1 only), which the
        # shipped check FAILs.
        doc = self._doc({"scheme": "exact", "network": "solana-devnet",
                         "x402Version": 1})
        row = bp._grade_kinds("https://f.example", doc, "exact", "solana-devnet",
                              want_version=2)
        self.assertEqual(row["status"], bp.FAIL)

    def test_a_non_integer_version_is_ignored(self):
        # Junk from a third party must not become a mismatch -- the same
        # tolerance supported_kinds documents.
        doc = self._doc({"scheme": "exact", "network": "eip155:8453",
                         "x402Version": "two"})
        self.assertEqual(bp.kind_versions(doc, "exact", "eip155:8453"), set())
        self.assertEqual(bp._grade_kinds("https://f.example", doc, "exact",
                                         "base", want_version=2)["status"], bp.OK)


class TestSettlementCost(unittest.TestCase):
    """Does the fee cover what it COSTS to collect the fee?

    Every other money check here asks what we charge; none asked what charging
    costs. Collecting an x402 payment means the facilitator broadcasts an
    onchain settlement, and past the free tier that settlement has a price.
    """

    # 10 bps of the amount, floored at $0.0001, free at or below $0.01 --
    # the shipped value-pricing config, in atomic units like the real one.
    @staticmethod
    def _fee(amount):
        amt = bp.Decimal(str(amount))
        if amt <= bp.Decimal("0.01"):
            return 0
        fee = max(bp.Decimal("0.0001"), amt * bp.Decimal(10) / bp.Decimal(10000))
        # ROUNDS, like the real BillingGate._price_for -- which is exactly why
        # break-even lands at $0.9995 rather than the $1.00 the bps arithmetic
        # gives, and why _breakeven_amount bisects the function instead of
        # inverting the formula. A truncating fixture here would hide that.
        return int((fee * 1000000).quantize(bp.Decimal("1")))

    def _points(self, *amounts):
        return [("p%d" % i, bp.Decimal(str(a)), bp.Decimal(str(a)))
                for i, a in enumerate(amounts)]

    def test_a_fee_below_the_settlement_cost_warns(self):
        # Mutation: comparing fee > settle, or skipping the comparison. A $0.10
        # payment bills the $0.0001 floor and costs $0.001 to settle -- we lose
        # ten times what we collect, and it looks like revenue in every report.
        row = bp.check_settlement_cost(self._fee, self._points("0.10"))
        self.assertEqual(row["status"], bp.WARN)
        self.assertEqual(row["below_settlement"], 1)

    def test_it_never_fails_only_warns(self):
        # Mutation: grading this FAIL. FAIL means "this would not work" and
        # exits 2. Billing WORKS -- the 402 is valid, the payer pays, the money
        # arrives. Selling below cost is a decision an operator may make
        # deliberately, so it must not block a deploy.
        row = bp.check_settlement_cost(self._fee, self._points(*["0.02"] * 50))
        self.assertNotEqual(row["status"], bp.FAIL)
        self.assertEqual(row["status"], bp.WARN)

    def test_a_fee_above_the_settlement_cost_is_ok(self):
        # Mutation: warning unconditionally. 10 bps of $50 is $0.05, fifty
        # times the settlement cost.
        row = bp.check_settlement_cost(self._fee, self._points("50.00"))
        self.assertEqual(row["status"], bp.OK)
        self.assertEqual(row["below_settlement"], 0)

    def test_the_free_path_is_not_counted_as_a_loss(self):
        # Mutation: counting a zero fee as "below settlement". A payment we do
        # not bill is never settled either, so it costs nothing -- counting it
        # would invent a loss on the entire free tier and make the healthiest
        # possible config look the worst.
        row = bp.check_settlement_cost(self._fee, self._points("0.005", "0.001"))
        self.assertEqual(row["status"], bp.OK)
        self.assertEqual(row["billable"], 0)

    def test_the_boundary_is_inclusive(self):
        # Mutation: `fee <= settle`. A fee EXACTLY equal to the settlement cost
        # breaks even; it is not a loss, and calling it one would flag a config
        # that is precisely at the line the check exists to find.
        fee_exact = lambda amount: 1000          # $0.001, == settle
        row = bp.check_settlement_cost(fee_exact, self._points("1.00"))
        self.assertEqual(row["status"], bp.OK)

    def test_the_settlement_cost_is_overridable(self):
        # Mutation: hardcoding CDP's price. It is a THIRD PARTY'S number, dated
        # 2026-09-08; when it moves, a hardcoded check mis-measures silently.
        pts = self._points("50.00")
        self.assertEqual(bp.check_settlement_cost(self._fee, pts)["status"], bp.OK)
        expensive = bp.check_settlement_cost(self._fee, pts, settle="1.00")
        self.assertEqual(expensive["status"], bp.WARN)

    def test_a_missing_corpus_is_not_a_finding_about_the_config(self):
        # Mutation: reporting OK on no data, which claims a measurement that
        # never happened -- the failure mode seller_report's rule 1 exists for.
        row = bp.check_settlement_cost(self._fee, [])
        self.assertEqual(row["status"], bp.WARN)
        self.assertIn("NOT measured", row["detail"])

    def test_the_detail_names_which_end_of_the_hull_was_measured(self):
        # The corpus stores a price HULL, not a list, and `check_revenue`
        # reports an interval over both ends. A bare "41 of 46" next to
        # "46-164" reads as a contradiction unless it says which end it is.
        row = bp.check_settlement_cost(self._fee, self._points("0.10"))
        self.assertIn("CHEAPEST", row["detail"])

    def test_a_fee_just_under_the_cost_is_still_a_loss(self):
        # Mutation: `fee < settle / 2`, or any threshold softer than the real
        # cost. 10 bps of $0.60 is $0.0006 -- comfortably above half the
        # settlement cost and still below it, so it loses money. A softened
        # threshold under-reports exactly the band nearest break-even, which is
        # where most of the corpus actually sits.
        row = bp.check_settlement_cost(self._fee, self._points("0.60"))
        self.assertEqual(row["status"], bp.WARN)
        self.assertEqual(row["below_settlement"], 1)
        self.assertEqual(bp.Decimal(row["avg_shortfall"]),
                         bp.Decimal("0.000400"))

    def test_the_mean_shortfall_is_actually_computed(self):
        # Mutation: reporting a constant "0". The shortfall is the number an
        # operator reads to decide whether this matters at all -- a hardcoded
        # zero would make every below-cost config look free.
        row = bp.check_settlement_cost(self._fee, self._points("0.10"))
        # $0.10 bills the $0.0001 floor against a $0.001 settlement.
        self.assertEqual(bp.Decimal(row["avg_shortfall"]),
                         bp.Decimal("0.000900"))

    def test_preflight_passes_the_configured_cost_through(self):
        # Mutation: dropping `settle=settlement_cost` from the assembly, which
        # leaves --settlement-cost parsed, documented and INERT while the check
        # silently uses CDP's default. The wired-and-inert pattern -- the exact
        # defect class this repo has now hit five times, and the reason the
        # check exists at all.
        rows = corpus(("0.50", "0.50"))
        cheap = bp.preflight(GOOD, corpus=rows, offline=True,
                             settlement_cost="0.0000001")
        dear = bp.preflight(GOOD, corpus=rows, offline=True,
                            settlement_cost="100")

        def row_of(report):
            return [c for c in report["checks"]
                    if c["name"] == "settlement_cost"][0]

        self.assertEqual(row_of(cheap)["status"], bp.OK)
        self.assertEqual(row_of(dear)["status"], bp.WARN)

    def test_break_even_is_found_from_the_real_fee_function(self):
        # Mutation: inverting the bps arithmetic instead of bisecting. The real
        # function ROUNDS, so break-even is 0.9995 rather than the 1.00 the
        # arithmetic gives -- and an inversion would be a SECOND implementation
        # of pricing, free to drift from the one that actually quotes.
        row = bp.check_settlement_cost(self._fee, self._points("0.10"))
        self.assertEqual(bp.Decimal(row["breakeven"]), bp.Decimal("0.9995"))

    def test_break_even_is_none_when_no_payment_could_ever_cover_it(self):
        # Mutation: returning the ceiling, which would assert a break-even that
        # does not exist and read as "just charge more".
        row = bp.check_settlement_cost(self._fee, self._points("50.00"),
                                       settle="10000")
        self.assertIsNone(row["breakeven"])
        self.assertNotIn("break-even is", row["detail"])


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
                                      "settlement_cost", "facilitator",
                                      "settlement_auth", "self_reported_amount"})

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


class TestDeployBlueprints(unittest.TestCase):
    """The three blueprints must agree on the pricing knobs.

    They did not. render.yaml carried free_below $0.01 with a comment measuring
    it at 46 of 265 payees billable, while fly.toml and render-free.yaml carried
    $10.00 -- which that same comment calls UNREACHABLE. Measured with this
    module: $10.00 bills 0-6 of 265, $0.01 bills 46-164. Two deploy targets shipped
    the config a third documented as broken, and nothing compared them.
    """

    KEYS = ("BLACKWALL_VALUE_PRICING", "BLACKWALL_FREE_BELOW",
            "BLACKWALL_MIN_FEE", "BLACKWALL_MAX_FEE_RATIO_BPS")

    def _knobs(self, path):
        """Pull key/value pairs without a YAML or TOML parser (stdlib-only)."""
        import re
        text = open(path, encoding="utf-8").read()
        found = {}
        for key in self.KEYS:
            # render: `- key: NAME` then `value: "x"`. fly: `NAME = "x"`.
            m = (re.search(r'key:\s*%s\s*\n\s*value:\s*"([^"]*)"' % key, text)
                 or re.search(r'^\s*%s\s*=\s*"([^"]*)"' % key, text, re.M))
            if m:
                found[key] = m.group(1)
        return found

    def test_all_blueprints_agree_on_the_pricing_knobs(self):
        # Mutation: reverting either blueprint to free_below 10.00. That config
        # deploys a paid tier no live payee can reach, and the service looks
        # healthy while collecting nothing -- the exact failure this whole module
        # exists to surface.
        paths = ["render.yaml", "render-free.yaml", "fly.toml"]
        knobs = {p: self._knobs(p) for p in paths}
        for key in self.KEYS:
            values = {p: k.get(key) for p, k in knobs.items()}
            self.assertEqual(len(set(values.values())), 1,
                             "%s disagrees across blueprints: %s" % (key, values))

    def test_the_shipped_pricing_actually_collects(self):
        # Mutation: agreeing on a value that bills nobody. Agreement is not
        # enough -- three blueprints can be consistently wrong.
        from x402 import PricingPolicy
        knobs = self._knobs("render.yaml")
        policy = PricingPolicy(free_below=knobs["BLACKWALL_FREE_BELOW"],
                               min_fee=knobs["BLACKWALL_MIN_FEE"],
                               max_fee_ratio_bps=knobs["BLACKWALL_MAX_FEE_RATIO_BPS"])
        points = bp.price_points(bp.load_corpus())
        proj = bp.project_revenue(policy.fee_atomic, points)
        self.assertEqual(bp.check_revenue(proj)["status"], bp.OK)
        self.assertEqual(
            bp.check_proportionality(policy.fee_atomic, points,
                                     bound_bps=int(knobs["BLACKWALL_MAX_FEE_RATIO_BPS"])
                                     )["status"], bp.OK)


class TestEnvDefaults(unittest.TestCase):
    def test_the_cli_reads_the_deploy_environment(self):
        # Mutation: hardcoding the CLI defaults. A preflight that can only check
        # its own defaults cannot check the config that is actually deployed --
        # which is the only config anyone cares about.
        import io
        import os
        from contextlib import redirect_stdout
        saved = {k: os.environ.get(k) for k in
                 ("BLACKWALL_PAY_TO", "BLACKWALL_VALUE_PRICING",
                  "BLACKWALL_FREE_BELOW", "BLACKWALL_MIN_FEE")}
        os.environ.update({"BLACKWALL_PAY_TO": GOOD,
                           "BLACKWALL_VALUE_PRICING": "1",
                           "BLACKWALL_FREE_BELOW": "0.01",
                           "BLACKWALL_MIN_FEE": "0.0001"})
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                code = bp.main(["--offline"])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        text = out.getvalue()
        self.assertIn("VALUE pricing", text)
        self.assertIn("free at or below 0.01", text)
        # This test is about the CLI READING THE ENVIRONMENT, and it asserted
        # exit 0 -- which conflated "the env was read" with "the config is
        # perfect". Those came apart when the settlement-cost check landed: the
        # env this fixture sets (10 bps, $0.0001 floor) genuinely bills below
        # what CDP charges to settle, so exit 1 is the CORRECT answer and the
        # old assertion was pinning a config opinion it never meant to make.
        # What must hold is that nothing FAILED (exit 2) -- a well-formed config
        # is still deployable -- and that the values came from the environment.
        self.assertIn(code, (0, 1), text)
        self.assertNotEqual(code, 2, text)


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
