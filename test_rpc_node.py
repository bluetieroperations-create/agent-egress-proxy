#!/usr/bin/env python3
"""
test_rpc_node.py -- TDD for Blackwall's own JSON-RPC front door. Each test names the
MUTATION it kills. Covers the three properties that justify the module: the method
allowlist (containment), the cache (privacy + latency), and fail-safe upstream handling.
"""
import json
import unittest
import urllib.error
import urllib.request

from rpc_node import (ALLOWED_METHODS, LocalRpcNode, RpcCache, cache_key, is_allowed,
                      validate_request)


class TestAllowlist(unittest.TestCase):
    def test_reads_allowed_writes_refused(self):
        # THE CONTAINMENT PROPERTY: our endpoint must never be usable to broadcast a
        # transaction or reach an admin namespace.
        self.assertTrue(is_allowed("eth_call"))
        for bad in ("eth_sendRawTransaction", "eth_sendTransaction", "personal_unlockAccount",
                    "admin_addPeer", "miner_start", "debug_traceCall", "evm_mine", ""):
            self.assertFalse(is_allowed(bad), bad)

    def test_no_write_method_in_the_allowlist(self):
        # MUTATION: someone adding a write method to ALLOWED_METHODS should fail here.
        for m in ALLOWED_METHODS:
            self.assertFalse(m.startswith(("eth_send", "personal_", "admin_", "miner_",
                                           "debug_", "txpool_")), m)

    def test_is_allowed_never_raises(self):
        for junk in (None, 5, [], {}):
            self.assertFalse(is_allowed(junk))


class TestValidate(unittest.TestCase):
    def test_accepts_well_formed(self):
        ok, err = validate_request({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                    "params": [{}, "latest"]})
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_rejects_batch(self):
        # MUTATION: allowing a batch lets ONE request fan out into many upstream calls,
        # multiplying both cost and the query leak.
        ok, _ = validate_request([{"method": "eth_call"}])
        self.assertFalse(ok)

    def test_rejects_disallowed_and_malformed(self):
        for body in ({"method": "eth_sendRawTransaction"}, {"method": ""}, {},
                     {"method": "eth_call", "params": "notalist"},
                     {"jsonrpc": "1.0", "method": "eth_call"}):
            ok, _ = validate_request(body)
            self.assertFalse(ok, body)


class TestCacheKey(unittest.TestCase):
    def test_stable_across_dict_ordering(self):
        a = cache_key("eth_call", [{"to": "0x1", "data": "0xab"}, "latest"])
        b = cache_key("eth_call", [{"data": "0xab", "to": "0x1"}, "latest"])
        self.assertEqual(a, b)

    def test_distinguishes_different_params(self):
        self.assertNotEqual(cache_key("eth_call", [{"to": "0x1"}]),
                            cache_key("eth_call", [{"to": "0x2"}]))

    def test_never_raises_on_unserializable(self):
        cache_key("eth_call", [object()])


class TestCache(unittest.TestCase):
    def test_hit_then_expiry(self):
        now = [1000.0]
        c = RpcCache(ttl=30, clock=lambda: now[0])
        c.put("k", {"result": "0x1"})
        self.assertEqual(c.get("k"), {"result": "0x1"})
        now[0] += 31
        # MUTATION: not expiring would serve a STALE compliance answer -- a payee
        # blacklisted after the cache write would still read as fine.
        self.assertIsNone(c.get("k"))

    def test_ttl_zero_disables(self):
        c = RpcCache(ttl=0)
        c.put("k", {"result": "0x1"})
        self.assertIsNone(c.get("k"))
        self.assertEqual(len(c), 0)

    def test_bounded_size(self):
        c = RpcCache(ttl=300, max_entries=10)
        for i in range(50):
            c.put("k%d" % i, {"result": i})
        self.assertLessEqual(len(c), 10)

    def test_counts_hits_and_misses(self):
        c = RpcCache(ttl=30)
        c.get("nope")
        c.put("k", {"result": 1})
        c.get("k")
        self.assertEqual((c.hits, c.misses), (1, 1))


class TestServer(unittest.TestCase):
    """Exercises the REAL HTTP path (not just the pure helpers)."""

    def setUp(self):
        self.calls = []

        def fwd(body):
            self.calls.append(body)
            return {"jsonrpc": "2.0", "id": body.get("id"), "result": "0xdeadbeef"}

        self.node = LocalRpcNode("http://upstream.invalid", port=0, forwarder=fwd,
                                 quiet=True)
        import threading
        self.node._httpd = None
        self.t = threading.Thread(target=self.node.serve_forever, daemon=True)
        self.t.start()
        for _ in range(200):                       # wait for bind
            if self.node._httpd is not None:
                break
            import time
            time.sleep(0.01)
        self.url = "http://127.0.0.1:%d/" % self.node.port

    def tearDown(self):
        self.node.shutdown()

    def _restart(self):
        """Rebind the server so a newly-assigned forwarder takes effect."""
        import threading
        import time
        self.node.shutdown()
        self.node.cache = type(self.node.cache)(ttl=self.node.cache.ttl)
        self.node._httpd = None
        threading.Thread(target=self.node.serve_forever, daemon=True).start()
        for _ in range(300):
            if self.node._httpd is not None:
                break
            time.sleep(0.01)
        self.url = "http://127.0.0.1:%d/" % self.node.port

    def _post(self, body):
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.getcode(), json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_forwards_and_returns_result(self):
        code, out = self._post({"jsonrpc": "2.0", "id": 7, "method": "eth_call",
                                "params": [{"to": "0x1"}, "latest"]})
        self.assertEqual(code, 200)
        self.assertEqual(out["result"], "0xdeadbeef")
        self.assertEqual(out["id"], 7)
        self.assertEqual(len(self.calls), 1)

    def test_second_identical_call_is_served_from_cache(self):
        # THE PRIVACY PROPERTY: a repeat check must NOT re-leak upstream.
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": "0x1"}, "latest"]}
        self._post(body)
        self._post(dict(body, id=2))
        self.assertEqual(len(self.calls), 1)       # still ONE upstream call

    def test_disallowed_method_never_reaches_upstream(self):
        code, out = self._post({"jsonrpc": "2.0", "id": 1,
                                "method": "eth_sendRawTransaction", "params": ["0xf8"]})
        self.assertIn("error", out)
        self.assertEqual(len(self.calls), 0)       # containment held

    def test_upstream_failure_is_a_clean_rpc_error(self):
        def boom(body):
            raise IOError("down")
        self.node.forwarder = boom
        self._restart()
        code, out = self._post({"jsonrpc": "2.0", "id": 3, "method": "eth_call",
                                "params": [{"to": "0x9"}, "latest"]})
        self.assertIn("error", out)
        self.assertIn("upstream unavailable", out["error"]["message"])

    def test_empty_cache_is_not_skipped(self):
        # AUDIT BUG (live-caught): RpcCache defines __len__, so an EMPTY cache is FALSY.
        # `if self.cache` skipped the lookup entirely until an entry existed, so the very
        # first repeat of a query still leaked upstream.
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": "0xabc"}, "latest"]}
        self._post(body)
        self.assertEqual(len(self.calls), 1)
        self._post(dict(body, id=2))
        self.assertEqual(len(self.calls), 1)         # served from a previously-empty cache

    def test_evm_revert_is_cached_but_transient_error_is_not(self):
        # PRIVACY: a revert is a DETERMINISTIC answer -- and a blacklisted payee is
        # precisely the case that reverts, so not caching it would re-leak the most
        # sensitive query every time. A bare error (rate limit) must NOT be pinned.
        seen = []

        def revert(body):
            seen.append(body)
            return {"jsonrpc": "2.0", "id": body.get("id"),
                    "error": {"code": 3, "message": "execution reverted",
                              "data": "0x08c379a0"}}
        self.node.forwarder = revert
        self._restart()
        b = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
             "params": [{"to": "0xdead"}, "latest"]}
        self._post(b)
        self._post(dict(b, id=2))
        self.assertEqual(len(seen), 1)              # revert cached -> no re-leak

        transient = []

        def flaky(body):
            transient.append(body)
            return {"jsonrpc": "2.0", "id": body.get("id"),
                    "error": {"code": -32005, "message": "rate limited"}}
        self.node.forwarder = flaky
        self._restart()
        b2 = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
              "params": [{"to": "0xbeef"}, "latest"]}
        self._post(b2)
        self._post(dict(b2, id=2))
        self.assertEqual(len(transient), 2)         # transient NOT pinned -> retried

    def test_concurrent_duplicates_coalesce_into_one_upstream_call(self):
        # AUDIT BUG (measured): the cache stops SEQUENTIAL re-leaks only. 8 concurrent
        # identical requests produced 8 upstream calls, so an agent doing parallel checks
        # leaked the same payer/payee pair N times. SingleFlight makes N duplicates cost
        # exactly ONE disclosure.
        import threading
        import time
        seen = []

        def slow(body):
            seen.append(body)
            time.sleep(0.25)
            return {"jsonrpc": "2.0", "id": body.get("id"), "result": "0x1"}
        self.node.forwarder = slow
        self._restart()
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": "0x1", "from": "0xA"}, "latest"]}
        ths = [threading.Thread(target=lambda i=i: self._post(dict(body, id=i)))
               for i in range(8)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        self.assertEqual(len(seen), 1, "8 concurrent duplicates must cost ONE upstream call")

    def test_singleflight_propagates_failure_to_followers(self):
        # A leader's failure must not hang or silently succeed for the followers.
        from rpc_node import SingleFlight
        import threading
        import time
        sf = SingleFlight()
        errs = []

        def boom():
            time.sleep(0.15)
            raise IOError("down")

        def run():
            try:
                sf.do("k", boom, timeout=5)
            except Exception as e:
                errs.append(type(e).__name__)
        ths = [threading.Thread(target=run) for _ in range(4)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        self.assertEqual(len(errs), 4)
        self.assertTrue(all(e == "OSError" for e in errs), errs)  # IOError is OSError

    def test_healthz_reports_real_counters(self):
        # AUDIT BUG: healthz read a static `stats` dict that was never updated, so it
        # always reported 0/0 -- a monitoring lie.
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": "0x1", "from": "0xA"}, "latest"]}
        self._post(body)
        self._post(dict(body, id=2))
        with urllib.request.urlopen(self.url + "healthz", timeout=5) as r:
            h = json.loads(r.read())
        self.assertEqual(h["hits"], self.node.cache.hits)
        self.assertEqual(h["misses"], self.node.cache.misses)
        self.assertGreater(h["hits"], 0)

    def test_healthz(self):
        with urllib.request.urlopen(self.url + "healthz", timeout=5) as r:
            self.assertTrue(json.loads(r.read())["ok"])

    def test_oversized_and_garbage_bodies(self):
        req = urllib.request.Request(self.url, data=b"not json",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                out = json.loads(r.read())
        except urllib.error.HTTPError as e:
            out = json.loads(e.read())
        self.assertIn("error", out)
        self.assertEqual(len(self.calls), 0)


if __name__ == "__main__":
    unittest.main()
