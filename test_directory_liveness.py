"""Tests for directory_liveness.py.

Each test states the MUTATION it kills, per repo convention.

The two bugs these are written against are both real errors made while running
this survey by hand, and both UNDERCOUNTED the live ecosystem: parsing the
challenge from the body only (which hid every v2 header-style endpoint), and
probing with GET only (which classified POST endpoints as unreachable).
"""

import json
import unittest

import directory_liveness as dl


def b64(doc):
    import base64
    return base64.b64encode(json.dumps(doc).encode()).decode().rstrip("=")


ACCEPTS = [{"scheme": "exact", "network": "eip155:8453", "maxAmountRequired": "8500",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}]


class ParseChallenge(unittest.TestCase):
    def test_reads_accepts_from_body(self):
        # MUTATION: dropping the body branch -> the v1 style stops being parsed,
        # which is the majority of the live ecosystem.
        accepts, carrier = dl.parse_challenge(
            json.dumps({"x402Version": 2, "accepts": ACCEPTS}), {})
        self.assertEqual(carrier, dl.BODY_ACCEPTS)
        self.assertEqual(accepts, ACCEPTS)

    def test_reads_accepts_from_www_authenticate_header(self):
        # MUTATION: dropping the header branch -> blockrun.ai (61 distinct payers)
        # reads as an opaque 402 and silently drops out of every count. This is
        # the exact bug the first survey pass had.
        headers = {"WWW-Authenticate": f'X402 requirements="{b64({"accepts": ACCEPTS})}"'}
        accepts, carrier = dl.parse_challenge("", headers)
        self.assertEqual(carrier, dl.HDR_ACCEPTS)
        self.assertEqual(accepts, ACCEPTS)

    def test_header_scheme_match_is_case_insensitive(self):
        # MUTATION: comparing the scheme case-sensitively -> observed servers send
        # both "X402" and "x402"; one of them would be missed.
        headers = {"www-authenticate": f'x402 requirements="{b64({"accepts": ACCEPTS})}"'}
        self.assertEqual(dl.parse_challenge("", headers)[1], dl.HDR_ACCEPTS)

    def test_body_wins_over_header(self):
        # MUTATION: preferring the header -> the survey would report endpoints as
        # needing a parser change when today's body parser already handles them,
        # overstating the gap.
        body_accepts = [{"network": "base", "maxAmountRequired": "1"}]
        headers = {"WWW-Authenticate": f'X402 requirements="{b64({"accepts": ACCEPTS})}"'}
        accepts, carrier = dl.parse_challenge(json.dumps({"accepts": body_accepts}), headers)
        self.assertEqual(carrier, dl.BODY_ACCEPTS)
        self.assertEqual(accepts, body_accepts)

    def test_malformed_header_does_not_mask_a_good_body(self):
        # MUTATION: letting a header decode error propagate -> one bad server
        # kills the whole survey run.
        headers = {"WWW-Authenticate": 'X402 requirements="!!!not-base64!!!"'}
        accepts, carrier = dl.parse_challenge(json.dumps({"accepts": ACCEPTS}), headers)
        self.assertEqual(carrier, dl.BODY_ACCEPTS)

    def test_empty_accepts_list_is_not_a_challenge(self):
        # MUTATION: truthiness on the key rather than the list -> `{"accepts": []}`
        # would count as scoreable when there is nothing to score.
        self.assertEqual(dl.parse_challenge(json.dumps({"accepts": []}), {}), (None, None))

    def test_junk_inputs_never_raise(self):
        # MUTATION: removing the tolerant guards -> arbitrary third-party bytes
        # crash the probe.
        for body in ("", "{", b"\xff\xfe", "null", "[]", json.dumps({"accepts": "nope"})):
            self.assertEqual(dl.parse_challenge(body, {}), (None, None))
        self.assertEqual(dl.parse_challenge("", {"WWW-Authenticate": "Bearer x"}), (None, None))
        self.assertEqual(dl.parse_challenge("", {"WWW-Authenticate": "X402 realm=\"x\""}), (None, None))


class Classify(unittest.TestCase):
    def test_opaque_402_when_no_requirements_anywhere(self):
        # MUTATION: defaulting a bare 402 to scoreable -> ~91 hosts that publish
        # nothing machine-readable get counted as addressable market.
        cls, _ = dl.classify(402, "{}", {})
        self.assertEqual(cls, dl.OPAQUE_402)

    def test_wellknown_only_applies_to_a_402(self):
        # MUTATION: checking well-known regardless of status -> a 404 route on a
        # host with a catalog would be reported as a payment-required endpoint.
        self.assertEqual(dl.classify(404, "", {}, wellknown_ok=True)[0], dl.OTHER)
        self.assertEqual(dl.classify(402, "{}", {}, wellknown_ok=True)[0], dl.WELLKNOWN)

    def test_non_402_is_other_not_dead(self):
        # MUTATION: treating 405 as dead -> POST-only endpoints get written off;
        # retrying those with POST recovered 14 live hosts.
        self.assertEqual(dl.classify(405, "", {})[0], dl.OTHER)


class ManagedHost(unittest.TestCase):
    def test_flags_free_hosting_subdomains(self):
        # MUTATION: inverting the check -> the BD lead list becomes all hobby demos
        # and excludes every real company.
        for host in ("foo.workers.dev", "bar.vercel.app", "x402-seller.onrender.com"):
            self.assertTrue(dl.is_managed_host(host), host)

    def test_does_not_flag_own_domains(self):
        for host in ("api.bitrefill.com", "blockrun.ai", "api.nansen.ai"):
            self.assertFalse(dl.is_managed_host(host), host)

    def test_tolerates_missing_host(self):
        self.assertFalse(dl.is_managed_host(None))


class RankLeads(unittest.TestCase):
    def rec(self, host, payers, cls=dl.BODY_ACCEPTS):
        return {"host": host, "class": cls, "distinct_payers": payers}

    def test_requires_all_three_criteria(self):
        # MUTATION: dropping any one filter -> unscoreable, low-traffic, or
        # throwaway-hosted endpoints leak into a list meant for outreach.
        results = [
            self.rec("api.good.com", 20),
            self.rec("api.opaque.com", 99, cls=dl.OPAQUE_402),   # not scoreable
            self.rec("api.thin.com", 2),                          # too few payers
            self.rec("demo.vercel.app", 50),                      # managed host
        ]
        self.assertEqual([r["host"] for r in dl.rank_leads(results)], ["api.good.com"])

    def test_header_style_endpoints_are_eligible_leads(self):
        # MUTATION: restricting SCOREABLE to the body carrier -> blockrun.ai, the
        # 2nd-largest lead by distinct payers, disappears from the list.
        results = [self.rec("blockrun.ai", 61, cls=dl.HDR_ACCEPTS)]
        self.assertEqual([r["host"] for r in dl.rank_leads(results)], ["blockrun.ai"])

    def test_sorted_by_distinct_payers_descending(self):
        # MUTATION: sorting ascending -> the outreach list is led by its weakest rows.
        results = [self.rec("a.com", 10), self.rec("b.com", 50), self.rec("c.com", 30)]
        self.assertEqual([r["host"] for r in dl.rank_leads(results)],
                         ["b.com", "c.com", "a.com"])


class Targets(unittest.TestCase):
    def test_one_target_per_host(self):
        # MUTATION: probing every resource -> 2500+ requests instead of 195, and
        # the per-host counts are inflated by however many routes each advertises.
        directory = [
            {"resources": ["https://a.com/one", "https://a.com/two"]},
            {"resources": ["https://a.com/three"]},
            {"resources": ["https://b.com/x"]},
        ]
        self.assertEqual([h for _, _, h in dl.targets_from_directory(directory)],
                         ["a.com", "b.com"])

    def test_skips_payees_with_no_resources(self):
        # MUTATION: not guarding -> IndexError kills the run on a resourceless payee.
        self.assertEqual(dl.targets_from_directory(
            [{"resources": []}, {}, {"resources": ["not-a-url"]}]), [])


class ProbeFallback(unittest.TestCase):
    """probe_host must retry with POST -- the GET-only artifact."""

    def test_falls_back_to_post_when_get_is_405(self):
        # MUTATION: removing the POST retry -> 14 live, scoreable hosts (incl. two
        # with 50+ distinct payers) are reported unreachable.
        calls = []

        def fake_open(url, data=None, timeout=None):
            calls.append("POST" if data is not None else "GET")
            if data is None:
                raise urllib_error(405, b"", {})
            raise urllib_error(402, json.dumps({"accepts": ACCEPTS}).encode(), {})

        with patched(fake_open):
            cls, note = dl.probe_host("https://x.com/p", "x.com")
        self.assertEqual(calls, ["GET", "POST"])
        self.assertEqual(cls, dl.BODY_ACCEPTS)

    def test_does_not_post_when_get_already_succeeded(self):
        # MUTATION: always retrying -> the survey doubles its request volume
        # against every third-party host for no new information.
        calls = []

        def fake_open(url, data=None, timeout=None):
            calls.append("POST" if data is not None else "GET")
            raise urllib_error(402, json.dumps({"accepts": ACCEPTS}).encode(), {})

        with patched(fake_open):
            dl.probe_host("https://x.com/p", "x.com")
        self.assertEqual(calls, ["GET"])

    def test_egress_denial_is_not_reported_as_a_dead_endpoint(self):
        # MUTATION: mapping 403 to DEAD -> our own network policy gets recorded as
        # the third party being down, which is a false statement about them.
        def fake_open(url, data=None, timeout=None):
            raise urllib_error(403, b"__agentproxy denied", {})

        with patched(fake_open):
            cls, _ = dl.probe_host("https://x.com/p", "x.com")
        self.assertEqual(cls, dl.BLOCKED)

    def test_transport_failure_is_dead_not_an_exception(self):
        # MUTATION: letting the error propagate -> one bad TLS handshake aborts
        # the whole survey.
        import socket as _socket

        def fake_open(url, data=None, timeout=None):
            raise _socket.timeout("timed out")

        with patched(fake_open):
            cls, _ = dl.probe_host("https://x.com/p", "x.com")
        self.assertEqual(cls, dl.DEAD)


# --- small helpers so the tests stay stdlib-only -------------------------------

import contextlib
import urllib.error


def urllib_error(code, body, headers):
    import io
    return urllib.error.HTTPError("https://x.com/p", code, "err", headers, io.BytesIO(body))


@contextlib.contextmanager
def patched(fake_open):
    original_open, original_wk = dl._open, dl._has_wellknown
    dl._open, dl._has_wellknown = fake_open, lambda host, timeout=None: False
    try:
        yield
    finally:
        dl._open, dl._has_wellknown = original_open, original_wk


if __name__ == "__main__":
    unittest.main()
