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

# x402_pay raises SystemExit(2) at import when `eth-account` is absent -- it is
# the one module here with a dependency. Left bare, that exit does not fail ONE
# test, it aborts the whole `python -m unittest ...` run before a single test
# executes, and prints an install hint instead of a test summary. That is how a
# rebuilt container silently zeroes the canonical suite. Skip this module
# instead, loudly, so the other 90+ files still run.
try:
    import x402_pay                         # noqa: E402
    _MISSING_DEP = ""
except SystemExit as exc:                   # pragma: no cover - env-dependent
    x402_pay = None
    _MISSING_DEP = "x402_pay needs eth-account: %s" % (exc,)



# HONEST TEST LIMITATION, documented rather than papered over (the same call
# `bounded_server.py` makes about its backlog fix).
#
# `clients/x402_pay.py` used to `raise SystemExit(2)` at IMPORT time when
# eth-account was absent. That does not fail one test: unittest imports every
# named module before running anything, so it killed the whole
# `python -m unittest ...` invocation before a single test executed. A box
# missing one optional package therefore ran ZERO of this repo's ~2200 tests.
# Harmless while this file sat outside CLAUDE.md's canonical command; fatal the
# moment it was added to it, which is how it surfaced.
#
# MEASURED, not asserted. With the import poisoned:
#     before:  exit 2, "Ran 0 tests"   -- test_addresses never executed
#     after:   exit 0, "Ran 16 tests"  -- x402_pay's parsing tests + the sibling
#
# There is NO unit test for it here, deliberately. The property needs a fresh
# interpreter with the dep absent, and this module is one of the modules such a
# child would run -- so the child re-enters the probe and forks again. The first
# attempt did exactly that and spawned processes until it was killed. A test that
# can fork unboundedly in CI is a worse defect than the gap it covers, and an
# env-var sentinel to break the recursion is a second mechanism to get wrong.
# The guard that actually holds the line is structural: `HAVE_ETH_ACCOUNT` is a
# flag, and the only `SystemExit` left is inside `main()`, where signing happens.


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


@unittest.skipIf(x402_pay is None, _MISSING_DEP or "eth-account absent")
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
