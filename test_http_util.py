"""
Tests for http_util.get_json -- retry/backoff + read-size cap on the live JSON GET.
Transport (`opener`) and clock (`sleep`) are injected: no network, no real waiting.
Each test states the mutation it kills.
"""
import email.message
import unittest
import urllib.error

import http_util as H


class _Resp:
    """A minimal response: context-manager + `.read(n)` returning a byte prefix."""
    def __init__(self, body):
        self._b = body if isinstance(body, bytes) else body.encode("utf-8")

    def read(self, n=-1):
        return self._b if n < 0 else self._b[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(seq):
    """seq: list of _Resp or Exception; each attempt consumes the next element."""
    it = iter(seq)

    def _open(req, timeout=None):
        x = next(it)
        if isinstance(x, Exception):
            raise x
        return x
    return _open


def _http_error(code, retry_after=None):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("http://x", code, "err", hdrs, None)


class TestGetJson(unittest.TestCase):
    def _sleeper(self):
        waits = []
        return (lambda s: waits.append(s)), waits

    def test_success_first_try(self):
        sleep, waits = self._sleeper()
        out = H.get_json("http://x", opener=_opener([_Resp('{"ok": 1}')]), sleep=sleep)
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(waits, [])                 # no retry, no wait

    def test_retries_then_succeeds_on_429(self):
        # Mutation: no retry -> the first 429 propagates and history is dropped.
        sleep, waits = self._sleeper()
        seq = [_http_error(429), _http_error(429), _Resp('{"ok": 1}')]
        out = H.get_json("http://x", opener=_opener(seq), sleep=sleep,
                         retries=3, backoff=0.5)
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(waits, [0.5, 1.0])         # exponential backoff, 2 retries

    def test_retries_on_timeout_and_urlerror(self):
        sleep, waits = self._sleeper()
        seq = [TimeoutError("slow"), urllib.error.URLError("reset"), _Resp('{"ok": 1}')]
        self.assertEqual(H.get_json("http://x", opener=_opener(seq), sleep=sleep),
                         {"ok": 1})
        self.assertEqual(len(waits), 2)

    def test_permanent_4xx_not_retried(self):
        # Mutation: retrying a 404 wastes the rate-limit budget and hides a real 404.
        sleep, waits = self._sleeper()
        with self.assertRaises(urllib.error.HTTPError):
            H.get_json("http://x", opener=_opener([_http_error(404)]), sleep=sleep)
        self.assertEqual(waits, [])                 # raised immediately

    def test_exhausts_retries_then_raises(self):
        sleep, waits = self._sleeper()
        seq = [_http_error(503)] * 4                # retries=3 -> 4 attempts, all fail
        with self.assertRaises(urllib.error.HTTPError):
            H.get_json("http://x", opener=_opener(seq), sleep=sleep, retries=3, backoff=1)
        self.assertEqual(len(waits), 3)             # waited before each of 3 retries

    def test_respects_retry_after_header(self):
        # Mutation: ignoring Retry-After hammers a server that told us to wait.
        sleep, waits = self._sleeper()
        seq = [_http_error(429, retry_after=2), _Resp('{"ok": 1}')]
        H.get_json("http://x", opener=_opener(seq), sleep=sleep, backoff=0.5)
        self.assertEqual(waits, [2.0])              # honored the header, not backoff

    def test_retry_after_clamped(self):
        sleep, waits = self._sleeper()
        seq = [_http_error(429, retry_after=9999), _Resp('{"ok": 1}')]
        H.get_json("http://x", opener=_opener(seq), sleep=sleep)
        self.assertEqual(waits, [H.MAX_BACKOFF])    # clamped, not a 3-hour sleep

    def test_response_too_large_not_retried(self):
        # Mutation: unbounded read -> a hostile/huge body exhausts memory.
        sleep, waits = self._sleeper()
        big = _Resp("x" * 100)
        with self.assertRaises(H.ResponseTooLarge):
            H.get_json("http://x", opener=_opener([big]), sleep=sleep, max_bytes=10)
        self.assertEqual(waits, [])                 # deterministic error, no retry

    def test_body_exactly_at_cap_ok(self):
        out = H.get_json("http://x", opener=_opener([_Resp('{"a": 1}')]),
                         sleep=lambda s: None, max_bytes=len('{"a": 1}'))
        self.assertEqual(out, {"a": 1})

    def test_read_capped_helper(self):
        self.assertEqual(H.read_capped(_Resp("abc"), 10), b"abc")
        with self.assertRaises(H.ResponseTooLarge):
            H.read_capped(_Resp("abcdef"), 3)


class TestUserAgent(unittest.TestCase):
    """The UA is not cosmetic: Cloudflare-fronted chain endpoints answer a
    non-browser agent with a 403, and a 403 is PERMANENT here (never retried),
    so the wrong default turns a working host into a hard failure. Measured:
    robinhoodchain.blockscout.com served `Blackwall/0.1` a 403 challenge and
    the browser-prefixed UA a 200."""

    def _capture(self, **kw):
        """Run a successful GET and hand back the urllib Request that was sent."""
        seen = []

        def _open(req, timeout=None):
            seen.append(req)
            return _Resp('{"ok": 1}')

        H.get_json("http://x", opener=_open, sleep=lambda s: None, **kw)
        return seen[0]

    def test_default_ua_is_browser_prefixed(self):
        # Mutation: go back to a bare token UA -> strict Cloudflare 403s us.
        self.assertTrue(H.DEFAULT_UA.startswith("Mozilla/5.0"), H.DEFAULT_UA)

    def test_default_ua_still_identifies_us(self):
        # Mutation: paste a plain browser UA -> we stop being attributable and
        # start impersonating a browser outright rather than declaring ourselves.
        self.assertIn("Blackwall", H.DEFAULT_UA)

    def test_request_carries_the_default_ua(self):
        # Mutation: build the header dict but never attach it to the Request.
        self.assertEqual(self._capture().get_header("User-agent"), H.DEFAULT_UA)

    def test_request_asks_for_json(self):
        self.assertEqual(self._capture().get_header("Accept"), "application/json")

    def test_caller_can_override_the_ua(self):
        req = self._capture(user_agent="Blackwall-probe/9")
        self.assertEqual(req.get_header("User-agent"), "Blackwall-probe/9")

    def test_headers_kwarg_wins_over_the_default_ua(self):
        # `headers=` is applied after the defaults, so a caller with a host-specific
        # UA gets it -- silently dropping theirs would reintroduce the 403.
        req = self._capture(headers={"User-Agent": "Blackwall-host-specific/1"})
        self.assertEqual(req.get_header("User-agent"), "Blackwall-host-specific/1")

    def test_403_is_permanent_so_a_blocked_ua_fails_hard(self):
        # This is WHY the default matters: adding 403 to RETRYABLE_STATUS would
        # paper over a UA block with retries instead of surfacing it; leaving the
        # bare UA in place means a Cloudflare-fronted host never succeeds at all.
        self.assertNotIn(403, H.RETRYABLE_STATUS)
        waits = []
        with self.assertRaises(urllib.error.HTTPError):
            H.get_json("http://x", opener=_opener([_http_error(403)]),
                       sleep=lambda s: waits.append(s))
        self.assertEqual(waits, [])


if __name__ == "__main__":
    unittest.main()
