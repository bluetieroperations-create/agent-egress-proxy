"""Tests for clients/x402_pay.py -- the funded-signer test client.

Narrow by design: this client signs real EIP-3009 authorizations and is exercised
by hand against live servers. What is pinned here is the piece that used to make
an entire class of server unpayable -- the 402 response's HEADERS reaching the
challenge parser instead of being dropped in the transport.

Each test names the mutation it kills.
"""

import base64
import json
import os
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "clients"))

import x402_challenge                       # noqa: E402
import x402_pay                             # noqa: E402


ACCEPT = {"payTo": "0x" + "ab" * 20, "amount": "25000",
          "asset": "0x" + "cd" * 20, "network": "eip155:8453"}


def _hdr_value(accepts):
    blob = base64.b64encode(json.dumps({"accepts": accepts}).encode()).decode()
    return 'X402 requirements="%s"' % blob


class _Resp:
    """Minimal stand-in for a urllib response context manager."""

    def __init__(self, status, body, headers):
        self.status = status
        self.headers = headers
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class HttpJsonSurfacesHeaders(unittest.TestCase):
    def setUp(self):
        self._real = urllib.request.urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", self._real)

    def _patch(self, fn):
        urllib.request.urlopen = fn

    def test_returns_response_headers_on_a_402(self):
        # Mutation: reverting _http_json to the 3-tuple that discarded headers.
        # An x402 v2 server carries its requirements ONLY in WWW-Authenticate,
        # so dropping the headers makes such a server unpayable -- the client
        # sees a 402 with an empty body and gives up.
        headers = {"WWW-Authenticate": _hdr_value([ACCEPT])}

        def fake(req, timeout=None):
            raise urllib.error.HTTPError(
                "http://seller/", 402, "Payment Required", headers, None)

        # HTTPError with fp=None still answers .read() with b"".
        self._patch(fake)
        status, parsed, raw, resp_headers = x402_pay._http_json("http://seller/", {"a": 1})
        self.assertEqual(status, 402)
        self.assertEqual(resp_headers, headers)

        # ...and the parser can then find the requirements the body lacks.
        accepts, carrier = x402_challenge.parse_challenge(raw, resp_headers)
        self.assertEqual(carrier, x402_challenge.HDR_ACCEPTS)
        self.assertEqual(accepts, [ACCEPT])

    def test_returns_headers_on_success_too(self):
        # Mutation: setting resp_headers only in the HTTPError branch, leaving
        # the success path to raise NameError on the very next line.
        body = json.dumps({"verdict": "GO"}).encode()
        self._patch(lambda req, timeout=None: _Resp(200, body, {"content-type": "application/json"}))
        status, parsed, raw, resp_headers = x402_pay._http_json("http://seller/", {"a": 1})
        self.assertEqual(status, 200)
        self.assertEqual(parsed, {"verdict": "GO"})
        self.assertEqual(resp_headers["content-type"], "application/json")

    def test_non_json_body_still_returns_four_values(self):
        # Mutation: the ValueError fallback returning a 3-tuple, so every
        # caller unpacking four values crashes on an HTML error page.
        self._patch(lambda req, timeout=None: _Resp(500, b"<html>boom</html>", {}))
        out = x402_pay._http_json("http://seller/")
        self.assertEqual(len(out), 4)
        self.assertIsNone(out[1])


if __name__ == "__main__":
    unittest.main()
