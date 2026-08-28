"""Tests for x402_challenge -- the shared 402-challenge parser.

Each test names the mutation it kills, per repo convention.
"""

import base64
import json
import unittest
import urllib.error

import x402_challenge as xc


def _hdr_value(doc):
    blob = base64.b64encode(json.dumps(doc).encode()).decode()
    return 'X402 requirements="%s"' % blob


ACCEPT = {"payTo": "0x" + "aa" * 20, "amount": "10000",
          "asset": "0x" + "bb" * 20, "network": "eip155:8453"}


class AcceptsOf(unittest.TestCase):
    def test_returns_a_non_empty_accepts_list(self):
        # Mutation: returning the raw doc instead of doc["accepts"].
        self.assertEqual(xc.accepts_of({"accepts": [ACCEPT]}), [ACCEPT])

    def test_empty_accepts_is_not_a_challenge(self):
        # Mutation: `isinstance(accepts, list)` without the non-empty check --
        # an empty list would then read as a valid challenge, and the paying
        # client would index accepts[0] and raise IndexError on a live server.
        self.assertIsNone(xc.accepts_of({"accepts": []}))

    def test_non_list_accepts_is_rejected(self):
        # Mutation: dropping the isinstance check.
        self.assertIsNone(xc.accepts_of({"accepts": {"payTo": "0x1"}}))

    def test_non_dict_input_is_rejected(self):
        # Mutation: calling .get on a non-dict would raise, not return None.
        for junk in ([ACCEPT], "accepts", 7, None):
            self.assertIsNone(xc.accepts_of(junk))


class DecodeRequirements(unittest.TestCase):
    def test_decodes_a_base64_requirements_blob(self):
        # Mutation: not base64-decoding -- the core of the v2 carrier.
        self.assertEqual(xc.decode_requirements(_hdr_value({"accepts": [ACCEPT]})),
                         [ACCEPT])

    def test_scheme_match_is_case_insensitive(self):
        # Mutation: comparing against the literal "X402". Servers send x402,
        # X402 and X402 alike; a case-sensitive check drops real endpoints.
        doc = {"accepts": [ACCEPT]}
        blob = base64.b64encode(json.dumps(doc).encode()).decode()
        for scheme in ("X402", "x402", "X402"):
            self.assertEqual(
                xc.decode_requirements('%s requirements="%s"' % (scheme, blob)),
                [ACCEPT], scheme)

    def test_scheme_must_be_a_whole_token(self):
        # Mutation: startswith("x402") instead of a token match. "X402Bearer"
        # is a DIFFERENT auth scheme; reading its parameters as x402 payment
        # requirements would have us sign against a challenge nobody issued.
        blob = base64.b64encode(json.dumps({"accepts": [ACCEPT]}).encode()).decode()
        for scheme in ("X402Bearer", "x402-evil", "X402FAKE", "x402x"):
            self.assertIsNone(
                xc.decode_requirements('%s requirements="%s"' % (scheme, blob)), scheme)
        # ...while the real scheme, with or without leading space, still works.
        for scheme in ("X402", "x402", "  X402"):
            self.assertEqual(
                xc.decode_requirements('%s requirements="%s"' % (scheme, blob)),
                [ACCEPT], scheme)

    def test_ignores_a_non_x402_scheme(self):
        # Mutation: dropping the scheme guard -- a Bearer/Basic challenge that
        # happened to contain requirements="..." would be read as a payment.
        blob = base64.b64encode(json.dumps({"accepts": [ACCEPT]}).encode()).decode()
        self.assertIsNone(xc.decode_requirements('Bearer requirements="%s"' % blob))

    def test_tolerates_stripped_base64_padding(self):
        # Mutation: b64decode(blob) with no padding fix -- servers strip '=',
        # and strict decoding then raises on exactly the real-world case.
        doc = {"accepts": [ACCEPT]}
        blob = base64.b64encode(json.dumps(doc).encode()).decode().rstrip("=")
        self.assertEqual(xc.decode_requirements('X402 requirements="%s"' % blob),
                         [ACCEPT])

    def test_malformed_blob_returns_none_not_raise(self):
        # Mutation: removing the try/except -- one bad third-party header would
        # abort a whole crawl.
        for bad in ('X402 requirements="!!!not-base64!!!"',
                    'X402 requirements="%s"' % base64.b64encode(b"not json").decode(),
                    'X402 realm="x402"',          # no requirements= at all
                    "", None, 42):
            self.assertIsNone(xc.decode_requirements(bad), bad)


def _pr_value(doc):
    """A `payment-required:` header value: bare base64, no requirements="" wrapper."""
    return base64.b64encode(json.dumps(doc).encode()).decode()


class PaymentRequiredHeader(unittest.TestCase):
    """The THIRD carrier: `payment-required: <base64 x402 doc>`.

    Measured 2026-08-28: of the 86 hosts the liveness survey filed as
    `opaque_402` -- a 402 with no readable requirements anywhere -- **80 carry a
    complete x402 v2 challenge in this header**. Their bodies are literally `{}`,
    which is why they looked opaque. api.ipintel.ai (78 distinct payers, 145
    settlements) is one of them: real, paid, and unreadable by us.
    """

    def test_reads_a_bare_base64_payment_required_header(self):
        # Mutation: not reading this header. That is the status quo ante, and it
        # made 80 live paying endpoints invisible.
        accepts, carrier = xc.parse_challenge(
            "{}", {"payment-required": _pr_value({"x402Version": 2, "accepts": [ACCEPT]})})
        self.assertEqual(accepts, [ACCEPT])
        self.assertEqual(carrier, xc.PAYMENT_REQUIRED)

    def test_header_name_is_case_insensitive(self):
        accepts, carrier = xc.parse_challenge(
            "", {"Payment-Required": _pr_value({"accepts": [ACCEPT]})})
        self.assertEqual(carrier, xc.PAYMENT_REQUIRED)
        self.assertEqual(accepts, [ACCEPT])

    def test_body_still_wins(self):
        # Mutation: letting a header outrank the body. The body is what every
        # other x402 client pays; that precedence must not change.
        body_accept = dict(ACCEPT, amount="1")
        accepts, carrier = xc.parse_challenge(
            json.dumps({"accepts": [body_accept]}),
            {"payment-required": _pr_value({"accepts": [dict(ACCEPT, amount="999")]})})
        self.assertEqual(carrier, xc.BODY_ACCEPTS)
        self.assertEqual(accepts[0]["amount"], "1")

    def test_www_authenticate_still_wins_over_payment_required(self):
        # Both are v2-style header carriers. Order must be DETERMINISTIC, and the
        # pre-existing carrier keeps precedence so no shipped behaviour changes.
        accepts, carrier = xc.parse_challenge(
            "", {"www-authenticate": _hdr_value({"accepts": [dict(ACCEPT, amount="7")]}),
                 "payment-required": _pr_value({"accepts": [dict(ACCEPT, amount="9")]})})
        self.assertEqual(carrier, xc.HDR_ACCEPTS)
        self.assertEqual(accepts[0]["amount"], "7")

    def test_tolerates_stripped_padding(self):
        blob = _pr_value({"accepts": [ACCEPT]}).rstrip("=")
        accepts, carrier = xc.parse_challenge("", {"payment-required": blob})
        self.assertEqual(carrier, xc.PAYMENT_REQUIRED)

    def test_junk_header_does_not_mask_a_good_body(self):
        accepts, carrier = xc.parse_challenge(
            json.dumps({"accepts": [ACCEPT]}), {"payment-required": "!!!not-base64!!!"})
        self.assertEqual(carrier, xc.BODY_ACCEPTS)

    def test_junk_header_alone_is_not_a_challenge(self):
        for bad in ("!!!", "", base64.b64encode(b"not json").decode(),
                    _pr_value({"accepts": []}), _pr_value(["not", "a", "dict"])):
            self.assertEqual(xc.parse_challenge("", {"payment-required": bad}),
                             (None, None), bad)

    def test_headers_are_read_only_once(self):
        # Mutation: calling _header_items(headers) separately per carrier. A
        # lazy/generator-backed mapping yields its headers on the FIRST call only,
        # so the second pass sees nothing and the challenge is silently lost --
        # exactly the class of silent miss this module exists to prevent.
        class SingleUse:
            def __init__(self):
                self.calls = 0

            def items(self):
                self.calls += 1
                if self.calls == 1:
                    return iter([("payment-required", _pr_value({"accepts": [ACCEPT]}))])
                return iter([])

        h = SingleUse()
        accepts, carrier = xc.parse_challenge("", h)
        self.assertEqual(carrier, xc.PAYMENT_REQUIRED)
        self.assertEqual(accepts, [ACCEPT])
        self.assertEqual(h.calls, 1, "headers must be materialized exactly once")

    def test_oversize_header_value_is_refused(self):
        # Mutation: no size guard on the header path. The BODY path is capped at
        # MAX_CHALLENGE_BYTES; leaving headers uncapped was an inconsistency.
        # Measured before the fix: a 19.2 MB value decoded to 200,000 accepts at
        # 85 MB peak. urllib caps a header line at 65536 bytes so this cannot
        # arrive over HTTP, but parse_challenge is public and takes any mapping.
        # The guard exists to avoid DOING the work, not to change the result: an
        # oversize value returns None either way, because base64 of "AAAA..."
        # decodes to garbage that json.loads rejects. A result-only assertion
        # therefore cannot fail, so assert the decode is never ATTEMPTED.
        huge = "A" * (xc.MAX_CHALLENGE_BYTES + 1)
        calls = []
        real = xc.base64.b64decode

        def spy(*a, **kw):
            calls.append(len(a[0]) if a else 0)
            return real(*a, **kw)

        xc.base64.b64decode = spy
        try:
            self.assertIsNone(xc.decode_payment_required(huge))
            self.assertEqual(xc.parse_challenge("", {"payment-required": huge}),
                             (None, None))
            self.assertEqual(calls, [], "oversize value must not reach b64decode")
        finally:
            xc.base64.b64decode = real

    def test_oversize_www_authenticate_value_is_refused(self):
        huge = 'X402 requirements="%s"' % ("A" * (xc.MAX_CHALLENGE_BYTES + 1))
        self.assertIsNone(xc.decode_requirements(huge))

    def test_a_normal_sized_header_still_parses(self):
        # Mutation: an off-by-one that rejects legitimate challenges.
        accepts, carrier = xc.parse_challenge(
            "", {"payment-required": _pr_value({"accepts": [ACCEPT]})})
        self.assertEqual(carrier, xc.PAYMENT_REQUIRED)

    def test_an_unrelated_header_is_not_scanned(self):
        # Mutation: scanning EVERY header for anything base64-decodable. That is
        # how the survey probe found this, but it is too permissive to ship --
        # an unrelated header could be mistaken for payment requirements.
        self.assertEqual(
            xc.parse_challenge("", {"x-some-cache": _pr_value({"accepts": [ACCEPT]})}),
            (None, None))


class ParseChallenge(unittest.TestCase):
    def test_reads_accepts_from_the_body(self):
        accepts, carrier = xc.parse_challenge(
            json.dumps({"x402Version": 2, "accepts": [ACCEPT]}), {})
        self.assertEqual(accepts, [ACCEPT])
        self.assertEqual(carrier, xc.BODY_ACCEPTS)

    def test_reads_accepts_from_the_www_authenticate_header(self):
        # Mutation: deleting the header branch -- the entire gap this module
        # exists to close. Verified live against blockrun.ai, which serves a
        # JSON body with no accepts[] and its real requirements in this header.
        accepts, carrier = xc.parse_challenge(
            "", {"WWW-Authenticate": _hdr_value({"accepts": [ACCEPT]})})
        self.assertEqual(accepts, [ACCEPT])
        self.assertEqual(carrier, xc.HDR_ACCEPTS)

    def test_header_name_match_is_case_insensitive(self):
        # Mutation: comparing the header name without .lower().
        accepts, carrier = xc.parse_challenge(
            "", {"www-authenticate": _hdr_value({"accepts": [ACCEPT]})})
        self.assertEqual(carrier, xc.HDR_ACCEPTS)
        self.assertEqual(accepts, [ACCEPT])

    def test_body_wins_over_header(self):
        # Mutation: checking the header first. The body is what every existing
        # x402 client reads, so on disagreement we must pay what they pay.
        body_accept = dict(ACCEPT, amount="1")
        hdr_accept = dict(ACCEPT, amount="999999")
        accepts, carrier = xc.parse_challenge(
            json.dumps({"accepts": [body_accept]}),
            {"WWW-Authenticate": _hdr_value({"accepts": [hdr_accept]})})
        self.assertEqual(carrier, xc.BODY_ACCEPTS)
        self.assertEqual(accepts[0]["amount"], "1")

    def test_malformed_header_does_not_mask_a_good_body(self):
        # Mutation: letting a header parse failure short-circuit the result.
        accepts, carrier = xc.parse_challenge(
            json.dumps({"accepts": [ACCEPT]}),
            {"WWW-Authenticate": 'X402 requirements="@@@"'})
        self.assertEqual(carrier, xc.BODY_ACCEPTS)
        self.assertEqual(accepts, [ACCEPT])

    def test_bad_body_does_not_mask_a_good_header(self):
        # Mutation: raising on unparseable JSON instead of falling through --
        # an HTML error page plus a valid header is a real server shape.
        accepts, carrier = xc.parse_challenge(
            "<html>402 Payment Required</html>",
            {"WWW-Authenticate": _hdr_value({"accepts": [ACCEPT]})})
        self.assertEqual(carrier, xc.HDR_ACCEPTS)
        self.assertEqual(accepts, [ACCEPT])

    def test_accepts_bytes_body(self):
        # Mutation: assuming str. urllib hands back bytes, so a str-only parser
        # silently fails on every real response.
        accepts, carrier = xc.parse_challenge(
            json.dumps({"accepts": [ACCEPT]}).encode(), {})
        self.assertEqual(carrier, xc.BODY_ACCEPTS)
        self.assertEqual(accepts, [ACCEPT])

    def test_considers_every_www_authenticate_header(self):
        # Mutation: reading only the first/last header. A response may carry
        # several challenges (e.g. Bearer then X402); the x402 one must be
        # found wherever it sits.
        class MultiHeaders:
            def items(self):
                return [("WWW-Authenticate", 'Bearer realm="api"'),
                        ("WWW-Authenticate", _hdr_value({"accepts": [ACCEPT]}))]
        accepts, carrier = xc.parse_challenge("", MultiHeaders())
        self.assertEqual(carrier, xc.HDR_ACCEPTS)
        self.assertEqual(accepts, [ACCEPT])

    def test_no_requirements_anywhere_is_not_a_challenge(self):
        self.assertEqual(xc.parse_challenge("{}", {}), (None, None))
        self.assertEqual(xc.parse_challenge("", None), (None, None))

    def test_junk_inputs_never_raise(self):
        # Mutation: removing the defensive conversions. This runs against
        # arbitrary third-party servers; an exception aborts the caller.
        for body in (None, "", b"", "not json", 7, {"a": 1}):
            for headers in (None, {}, {"x": None}, 3, "nope", object()):
                self.assertEqual(xc.parse_challenge(body, headers), (None, None))


class AcceptsFromHttpError(unittest.TestCase):
    @staticmethod
    def _err(code, body=b"", headers=None):
        return urllib.error.HTTPError(
            "http://x/", code, "why", headers or {}, _FakeStream(body))

    def test_extracts_a_header_carried_challenge_from_a_402(self):
        err = self._err(402, b"", {"WWW-Authenticate": _hdr_value({"accepts": [ACCEPT]})})
        self.assertEqual(xc.accepts_from_http_error(err), ([ACCEPT], xc.HDR_ACCEPTS))

    def test_extracts_a_body_carried_challenge_from_a_402(self):
        err = self._err(402, json.dumps({"accepts": [ACCEPT]}).encode())
        self.assertEqual(xc.accepts_from_http_error(err), ([ACCEPT], xc.BODY_ACCEPTS))

    def test_reads_the_body_only_once(self):
        # Mutation: reading err.read() twice (e.g. for logging, then parsing).
        # The second read returns b"" and a body-carried challenge would be
        # silently downgraded to "unreadable" -- the exact class of bug that
        # made these endpoints invisible in the first place.
        stream = _FakeStream(json.dumps({"accepts": [ACCEPT]}).encode())
        err = urllib.error.HTTPError("http://x/", 402, "why", {}, stream)
        self.assertEqual(xc.accepts_from_http_error(err)[0], [ACCEPT])
        self.assertEqual(stream.reads, 1)

    def test_oversize_body_is_dropped_not_parsed(self):
        # Mutation: err.read() with no size argument. The crawler calls this on
        # every fetched source including hostile ones; an uncapped read let a
        # 43MB "402" reach 172MB of peak memory in an audit, defeating the
        # read_capped protection http_util applies to the normal fetch path.
        entry = dict(ACCEPT)
        huge = json.dumps({"accepts": [entry] * 40000}).encode()
        self.assertGreater(len(huge), xc.MAX_CHALLENGE_BYTES)
        err = urllib.error.HTTPError("http://evil/", 402, "why", {}, _SizedStream(huge))
        self.assertEqual(xc.accepts_from_http_error(err), (None, None))

    def test_never_buffers_more_than_the_cap(self):
        # Mutation: err.read() with no size argument. The LENGTH check alone
        # still rejects an oversize body, so a result-only assertion cannot
        # tell the two apart -- but by then the whole thing is already in
        # memory, which is the actual harm (43MB body -> 172MB peak, measured).
        # This asserts the bytes never leave the socket.
        stream = _SizedStream(b"x" * (xc.MAX_CHALLENGE_BYTES * 4))
        err = urllib.error.HTTPError("http://evil/", 402, "why", {}, stream)
        xc.accepts_from_http_error(err)
        self.assertLessEqual(stream.yielded, xc.MAX_CHALLENGE_BYTES + 1,
                             "read the whole hostile body into memory")

    def test_oversize_body_does_not_hide_a_header_challenge(self):
        # Mutation: returning (None, None) early on an oversize body. A seller
        # must not be able to suppress its own advertised requirements -- and a
        # legitimately chatty error page alongside a valid header is realistic.
        huge = b"x" * (xc.MAX_CHALLENGE_BYTES + 10)
        err = urllib.error.HTTPError(
            "http://seller/", 402, "why",
            {"WWW-Authenticate": _hdr_value({"accepts": [ACCEPT]})}, _SizedStream(huge))
        self.assertEqual(xc.accepts_from_http_error(err), ([ACCEPT], xc.HDR_ACCEPTS))

    def test_body_at_the_cap_is_still_parsed(self):
        # Mutation: an off-by-one that rejects a body of exactly the cap size.
        body = json.dumps({"accepts": [ACCEPT]}).encode()
        pad = b" " * (xc.MAX_CHALLENGE_BYTES - len(body))
        err = urllib.error.HTTPError("http://s/", 402, "why", {}, _SizedStream(body + pad))
        self.assertEqual(xc.accepts_from_http_error(err), ([ACCEPT], xc.BODY_ACCEPTS))

    def test_stream_without_a_size_argument_still_works(self):
        # Mutation: dropping the TypeError fallback. urllib's real response
        # objects take a size, but a wrapped/adapted stream may not, and the
        # crawler must not lose a legitimate challenge to that.
        body = json.dumps({"accepts": [ACCEPT]}).encode()
        err = urllib.error.HTTPError("http://s/", 402, "why", {}, _FakeStream(body))
        self.assertEqual(xc.accepts_from_http_error(err), ([ACCEPT], xc.BODY_ACCEPTS))

    def test_a_non_402_is_not_a_challenge(self):
        # Mutation: dropping the status guard -- a 401 or a 500 error page
        # carrying an unrelated WWW-Authenticate would be read as a payment
        # requirement.
        err = self._err(401, b"", {"WWW-Authenticate": _hdr_value({"accepts": [ACCEPT]})})
        self.assertEqual(xc.accepts_from_http_error(err), (None, None))

    def test_402_with_no_requirements_is_none(self):
        self.assertEqual(xc.accepts_from_http_error(self._err(402, b"nope")), (None, None))

    def test_non_http_error_does_not_raise(self):
        # Mutation: assuming .code exists. crawl() catches broad exceptions and
        # may hand us a URLError or a socket error.
        self.assertEqual(xc.accepts_from_http_error(ValueError("boom")), (None, None))

    def test_unreadable_body_does_not_raise(self):
        class Exploding:
            def read(self, *a):
                raise OSError("connection reset")
            def close(self):
                pass
        err = urllib.error.HTTPError(
            "http://x/", 402, "why",
            {"WWW-Authenticate": _hdr_value({"accepts": [ACCEPT]})}, Exploding())
        # The header still carries the challenge even though the body died.
        self.assertEqual(xc.accepts_from_http_error(err), ([ACCEPT], xc.HDR_ACCEPTS))


class _SizedStream:
    """A stream that honours read(n), the way a real urllib response does."""

    def __init__(self, body):
        self._body = body
        self._pos = 0
        self.yielded = 0        # bytes actually handed out, for the cap test

    def read(self, n=-1):
        end = len(self._body) if n is None or n < 0 else self._pos + n
        chunk = self._body[self._pos:end]
        self._pos = end
        self.yielded += len(chunk)
        return chunk

    def close(self):
        pass


class _FakeStream:
    def __init__(self, body):
        self._body = body
        self.reads = 0

    def read(self, *a):
        self.reads += 1
        return self._body if self.reads == 1 else b""

    def close(self):
        pass


if __name__ == "__main__":
    unittest.main()
