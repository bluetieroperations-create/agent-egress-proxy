"""Tests for seller_portal -- serving the seller diagnostic at a URL.

Each test names the MUTATION it kills. This service takes a string from a browser
and renders HTML back while making an outbound request to a stranger's endpoint,
so the tests here are mostly about the two things that can go wrong with that
shape: SSRF (what URL do we actually fetch, and who chose it) and injection
(what reaches the page unescaped).
"""

import json
import threading
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import seller_portal as SP
import seller_report as SR


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


def corpus(host="api.example.com", payee=PAYEE):
    return [{"payee": payee, "resources": ["https://%s/a" % host],
             "settlement_count": 100, "distinct_payers": 10,
             "min_price": "0.001", "max_price": "0.01", "category": "commerce"}]


class TestKeyHandling(unittest.TestCase):
    def test_an_oversize_key_is_refused_before_any_work(self):
        # Mutation: dropping the length cap. The key is attacker-chosen and is
        # used as a cache key, so an unbounded one is both a lookup cost and a
        # memory cost per request.
        self.assertIsNone(SP.clean_key("a" * (SP.MAX_KEY + 1)))
        self.assertIsNotNone(SP.clean_key("a" * SP.MAX_KEY))

    def test_control_characters_are_refused(self):
        # Mutation: accepting them. They reach logs and HTML; refusing at the
        # door is cheaper than escaping everywhere downstream.
        for bad in ("a\nb", "a\rb", "a\x00b", "a b", "a​b"):
            self.assertIsNone(SP.clean_key(bad), bad)

    def test_percent_encoding_is_decoded_once(self):
        self.assertEqual(SP.clean_key("api%2Eexample%2Ecom"), "api.example.com")

    def test_routing_is_pure_and_total(self):
        # Mutation: routing on a prefix match, which would make /r/../ or
        # /rogue resolve as a report.
        self.assertEqual(SP.route_of("/"), ("home", None))
        self.assertEqual(SP.route_of("/healthz"), ("health", None))
        self.assertEqual(SP.route_of("/r/abc"), ("report", "abc"))
        self.assertEqual(SP.route_of("/r/"), ("report", None))
        self.assertEqual(SP.route_of("/rogue"), (None, None))
        self.assertEqual(SP.route_of("/../../etc/passwd"), (None, None))


class TestSsrf(unittest.TestCase):
    """The property that matters: who chooses the URL we fetch."""

    def test_a_caller_key_never_becomes_a_probe_url(self):
        # Mutation: probing the key, or any URL derived from it. This is the
        # whole SSRF question -- if a caller's string could become the fetch
        # target, anyone could read cloud metadata through our report.
        probed = []
        portal = SP.Portal(rows=corpus(), coverage={}, category_index={})
        portal._probe_fn = lambda resources: probed.append(resources)
        for hostile in ("http://169.254.169.254/latest/meta-data/",
                        "localhost:8080", "127.0.0.1", "file:///etc/passwd"):
            key = SP.clean_key(hostile)
            if key is None:
                continue
            report = portal.report(key)
            self.assertFalse(report["found"], hostile)
        self.assertEqual(probed, [],
                         "a key we do not hold caused a network call")

    def test_a_known_key_probes_only_the_corpus_resources(self):
        # Mutation: passing the caller's key through to the probe. The probe
        # target must come from the ROW, which is our data, not the request.
        probed = []
        portal = SP.Portal(rows=corpus(), coverage={}, category_index={})
        portal._probe_fn = lambda resources: probed.append(list(resources))
        portal.report("api.example.com")
        self.assertEqual(probed, [["https://api.example.com/a"]])

    def test_private_and_metadata_addresses_are_refused(self):
        # Mutation: fetching whatever the corpus says. A resource URL is
        # harvested from a STRANGER'S advertisement -- attacker-authored content
        # we store and later fetch -- so the cloud metadata address, loopback and
        # RFC1918 must all be refused however they arrive.
        def resolve(host):
            return {"meta": ["169.254.169.254"], "loop": ["127.0.0.1"],
                    "priv": ["10.0.0.5"], "pub": ["93.184.216.34"]}[host]
        for host in ("meta", "loop", "priv"):
            ok, reason = SR.safe_probe_url("https://%s/x" % host, resolve=resolve)
            self.assertFalse(ok, host)
            self.assertIn("non-public", reason)
        self.assertTrue(SR.safe_probe_url("https://pub/x", resolve=resolve)[0])

    def test_every_resolved_address_must_be_public(self):
        # Mutation: checking only the first address. A name answering with one
        # public and one private address would pass, and the OS would then be
        # free to connect to either.
        def resolve(host):
            return ["93.184.216.34", "127.0.0.1"]
        self.assertFalse(SR.safe_probe_url("https://mixed/x", resolve=resolve)[0])

    def test_non_http_schemes_are_refused(self):
        # Mutation: allowing any scheme. urllib will happily open file:// and
        # ftp://, which turns a harvested URL into a local file read.
        for url in ("file:///etc/passwd", "gopher://h/x", "ftp://h/x"):
            self.assertFalse(SR.safe_probe_url(url, resolve=lambda h: ["1.1.1.1"])[0],
                             url)

    def test_embedded_credentials_are_refused(self):
        # Mutation: trusting urlsplit's hostname alone. `https://real@evil/x`
        # reads as "real" to a human scanning the corpus and connects to evil.
        self.assertFalse(
            SR.safe_probe_url("https://trusted@evil/x",
                              resolve=lambda h: ["93.184.216.34"])[0])

    def test_an_unresolvable_host_is_refused_not_fetched(self):
        def resolve(host):
            raise OSError("nxdomain")
        self.assertFalse(SR.safe_probe_url("https://nope/x", resolve=resolve)[0])

    def test_a_refused_probe_reads_as_us_not_looking(self):
        # Mutation: reporting a refused probe as the seller's defect. We
        # declined to fetch a URL WE harvested; that is not a fact about them.
        result = SR.probe_endpoint("file:///etc/passwd")
        self.assertIn("not probed", result["error"])
        row = SR.assess_reach(result)
        self.assertEqual(row["severity"], SR.UNKNOWN)


class TestRebindingIsPinned(unittest.TestCase):
    """The address we validated is the address we dial."""

    def test_the_validated_address_is_returned_for_pinning(self):
        # Mutation: validating and discarding the address. safe_probe_url checked
        # the name and then let urllib resolve it a SECOND time, so a name that
        # answered public on the check and private on the connect walked
        # through -- the classic rebinding TOCTOU, live here because these names
        # come from strangers' advertisements we store and later fetch.
        ok, _, ip = SR.pinned_address("https://pub/x",
                                      resolve=lambda h: ["93.184.216.34"])
        self.assertTrue(ok)
        self.assertEqual(ip, "93.184.216.34")

    def test_a_refused_url_yields_no_address(self):
        for url in ("file:///etc/passwd", "https://priv/x"):
            ok, _, ip = SR.pinned_address(
                url, resolve=lambda h: ["10.0.0.5"])
            self.assertFalse(ok)
            self.assertIsNone(ip)

    def test_the_fetch_dials_the_pinned_address_and_never_re_resolves(self):
        # Mutation: passing the URL to urllib instead of connecting to the IP.
        # Dialling an address that cannot serve the name proves the socket goes
        # where we said -- a re-resolving implementation would succeed here,
        # which is exactly the bug.
        result = SR.probe_endpoint("https://blockrun.ai/x")
        self.assertIsInstance(result, dict)
        import socket
        real = socket.create_connection
        seen = []

        def spy(address, *a, **kw):
            seen.append(address)
            raise OSError("blocked by test")

        socket.create_connection = spy
        try:
            SR.probe_endpoint("https://blockrun.ai/x")
        finally:
            socket.create_connection = real
        self.assertTrue(seen, "no connection attempted")
        host, port = seen[0]
        self.assertNotEqual(host, "blockrun.ai",
                            "connected by NAME -- the address was re-resolved")
        self.assertEqual(port, 443)

    def test_tls_presents_the_HOSTNAME_not_the_pinned_ip(self):
        # Mutation: `server_hostname=conn.host`, i.e. the IP we dialled. Grepping
        # the source for "server_hostname" does NOT catch that -- the string is
        # still there. The VALUE is the whole point: presenting the IP fails
        # certificate verification against every real host, and the tempting
        # "fix" for that is disabling verification, which is far worse than the
        # rebinding bug this was closing. So intercept the handshake and read it.
        import socket
        import ssl
        seen = {}
        real_conn, real_wrap = socket.create_connection, ssl.SSLContext.wrap_socket

        class _Sock:
            def close(self):
                pass

        def fake_conn(address, *a, **kw):
            seen["dialled"] = address[0]
            return _Sock()

        def fake_wrap(self, sock, *a, **kw):
            seen["sni"] = kw.get("server_hostname")
            # The context itself, captured at the handshake -- see the
            # verification test below for why grepping the source is not enough.
            seen["verify_mode"] = self.verify_mode
            seen["check_hostname"] = self.check_hostname
            raise OSError("stop here")

        socket.create_connection = fake_conn
        ssl.SSLContext.wrap_socket = fake_wrap
        try:
            SR.probe_endpoint("https://blockrun.ai/x")
        finally:
            socket.create_connection = real_conn
            ssl.SSLContext.wrap_socket = real_wrap
        self.assertEqual(seen.get("sni"), "blockrun.ai")
        self.assertNotEqual(seen.get("dialled"), "blockrun.ai")

    def test_tls_verification_is_never_weakened(self):
        # Mutation: `ssl._create_unverified_context()`. Grepping the source for
        # CERT_NONE and `check_hostname = False` does NOT catch that -- it
        # spells the same thing a third way, and there are more spellings. So
        # assert the STATE of the context that actually reaches the handshake,
        # which is true regardless of how it was built. This is the tempting bad
        # fix for a hostname-verification failure, and it is worse than the
        # rebinding bug it would appear to solve.
        import socket
        import ssl
        seen = {}
        real_conn, real_wrap = socket.create_connection, ssl.SSLContext.wrap_socket

        class _Sock:
            def close(self):
                pass

        def fake_wrap(self, sock, *a, **kw):
            seen["verify_mode"] = self.verify_mode
            seen["check_hostname"] = self.check_hostname
            raise OSError("stop here")

        socket.create_connection = lambda *a, **kw: _Sock()
        ssl.SSLContext.wrap_socket = fake_wrap
        try:
            SR.probe_endpoint("https://blockrun.ai/x")
        finally:
            socket.create_connection = real_conn
            ssl.SSLContext.wrap_socket = real_wrap
        self.assertEqual(seen.get("verify_mode"), ssl.CERT_REQUIRED)
        self.assertIs(seen.get("check_hostname"), True)


class TestProbeCooldown(unittest.TestCase):
    """How often we will touch one seller, however many people ask."""

    ROWS = [{"payee": PAYEE, "settlement_count": 5, "min_price": "0.01",
             "max_price": "0.02",
             "resources": ["https://one.example/a", "https://two.example/b"]}]

    def _portal(self, clock):
        return SP.Portal(rows=[dict(r) for r in self.ROWS], coverage={},
                         category_index={}, clock=clock)

    def _spy(self):
        calls = []
        self._original = SR.probe_resources
        SR.probe_resources = lambda res: (calls.append(list(res))
                                          or {"url": res[0], "status": 402})
        return calls

    def test_two_spellings_of_one_host_probe_it_once(self):
        # Mutation: no cooldown. The report cache is keyed by what the VISITOR
        # typed, so each spelling would otherwise probe the same stranger
        # independently. The bound is PER HOST -- that is what `_mark_probed`
        # keys on and what the docs claim -- so this pins the host, not the
        # payee. See the sibling test below for what that deliberately allows.
        calls = self._spy()
        now = [1000.0]
        try:
            portal = self._portal(lambda: now[0])
            portal.report("one.example")
            now[0] += 1
            portal.report("one.example/")          # a different cache key
            now[0] += 1
            portal.report("ONE.example")
        finally:
            SR.probe_resources = self._original
        self.assertEqual(len(calls), 1)

    def test_an_address_key_may_still_reach_an_untouched_sibling_host(self):
        # NOT a hole -- the documented bound is per host, and a payee's second
        # host is a second server that nobody has touched. Pinned explicitly
        # because a per-PAYEE reading of the cooldown is the tempting one, and
        # `seller_report.resources_for_key` (which scopes a host-keyed report to
        # that host alone) is what makes the difference visible. Mutation:
        # marking the whole payee cooled would silence a host we never probed
        # and report it as "not checked".
        calls = self._spy()
        now = [1000.0]
        try:
            portal = self._portal(lambda: now[0])
            portal.report("one.example")
            now[0] += 1
            portal.report(PAYEE)
        finally:
            SR.probe_resources = self._original
        self.assertEqual([c[0] for c in calls],
                         ["https://one.example/a", "https://two.example/b"])

    def test_only_the_hosts_actually_contacted_are_cooled(self):
        # Mutation: marking every candidate (the original), or only the first.
        # `probe_resources` stops at the first ANSWER, so a later host was never
        # touched -- cooling it would tell a visitor asking about THAT host
        # "not checked" because a sibling was busy, which is the cross-host
        # attribution resources_for_key exists to stop.
        self._original = SR.probe_resources
        # one.example answers, so two.example is never contacted.
        SR.probe_resources = lambda res: {"url": res[0], "status": 402}
        try:
            portal = self._portal(lambda: 1000.0)
            portal.report(PAYEE)
            self.assertFalse(portal._host_ready("one.example", 1000.0))
            self.assertTrue(portal._host_ready("two.example", 1000.0))
        finally:
            SR.probe_resources = self._original

    def test_a_host_tried_and_failed_before_the_winner_is_cooled(self):
        # Mutation: marking only the resource that answered. A host we contacted
        # and that failed WAS touched, and the cooldown protects strangers from
        # being touched, not from answering.
        self._original = SR.probe_resources
        SR.probe_resources = lambda res: {"url": res[-1], "status": 402}
        try:
            portal = self._portal(lambda: 1000.0)
            portal.report(PAYEE)
            self.assertFalse(portal._host_ready("one.example", 1000.0))
            self.assertFalse(portal._host_ready("two.example", 1000.0))
        finally:
            SR.probe_resources = self._original

    def test_a_seller_that_answered_nothing_is_still_cooled(self):
        # Mutation: cooling nothing when the probe returns no recognisable url.
        # A host that FAILS was still contacted, and this is the case that
        # repeats most -- a dead seller would otherwise be re-probed on every
        # request that misses the report cache, which is precisely the traffic
        # the cooldown exists to keep off a stranger.
        self._original = SR.probe_resources
        SR.probe_resources = lambda res: None
        try:
            portal = self._portal(lambda: 1000.0)
            portal.report(PAYEE)
            self.assertFalse(portal._host_ready("one.example", 1000.0))
            self.assertFalse(portal._host_ready("two.example", 1000.0))
        finally:
            SR.probe_resources = self._original

    def test_the_cooldown_expires(self):
        calls = []
        original = SR.probe_resources
        SR.probe_resources = lambda res: (calls.append(1)
                                          or {"url": res[0], "status": 402})
        now = [1000.0]
        try:
            portal = self._portal(lambda: now[0])
            portal.report("one.example")
            now[0] += SP.PROBE_COOLDOWN + 1
            portal.report("two.example")
        finally:
            SR.probe_resources = original
        self.assertEqual(len(calls), 2)

    def test_a_fully_cooled_seller_is_reported_not_checked(self):
        # Mutation: probing anyway when every host is cooling. Re-hitting a
        # stranger to tell a visitor something we already know is the behaviour
        # the cooldown exists to stop.
        portal = self._portal(lambda: 1000.0)
        for host in ("one.example", "two.example"):
            portal._mark_probed(host, 1000.0)
        self.assertIsNone(portal._probe_fn(self.ROWS[0]["resources"]))

    def test_the_cooldown_map_is_bounded(self):
        # Mutation: unbounded. The key space is the corpus today, but a refresh
        # grows it, and every other attacker-influenced map here is bounded.
        portal = self._portal(lambda: 1000.0)
        for i in range(SP.COOLDOWN_MAX + 50):
            portal._mark_probed("h%d.example" % i, 1000.0 + i)
        self.assertLessEqual(len(portal._cooldown), SP.COOLDOWN_MAX)


class TestEscaping(unittest.TestCase):
    def test_the_key_is_html_escaped_in_the_report(self):
        # Mutation: interpolating raw. SIXTH instance of the untrusted-echo
        # class here and the first that is XSS rather than a forged log line --
        # this text reaches a browser.
        report = SR.build_report("<script>alert(1)</script>", [])
        page = SP.render_report(report)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_finding_text_is_escaped_too(self):
        # Mutation: escaping the key but not the findings. Every finding carries
        # strings authored by the party being reported on -- host, payee,
        # category, and their own revert text.
        report = {"key": "k", "found": True, "severity": SR.WARNING,
                  "hosts": ["</div><script>x</script>"], "category": "c",
                  "settlements": 1, "distinct_payers": 1,
                  "findings": [{"code": "c", "severity": SR.WARNING,
                                "title": "<img src=x onerror=y>",
                                "detail": "<b>d</b>", "evidence": "'\"e"}]}
        page = SP.render_report(report)
        for raw in ("<script>x</script>", "<img src=x onerror=y>", "<b>d</b>"):
            self.assertNotIn(raw, page)

    def test_quotes_are_escaped_for_attribute_safety(self):
        # Mutation: html.escape(quote=False). A finding severity flows into a
        # class attribute; unescaped quotes there break out of it.
        self.assertNotIn('"', SP.esc('a"b'))
        self.assertNotIn("'", SP.esc("a'b"))


class TestRateLimitIdentity(unittest.TestCase):
    """Who the limiter counts, once this sits behind a CDN."""

    XFF = "203.0.113.9, 172.71.1.1"   # real client, then Cloudflare

    def test_depth_two_finds_the_real_client_behind_a_cdn(self):
        # Mutation: always taking the rightmost entry. That is correct behind ONE
        # proxy and wrong behind Cloudflare-in-front-of-Render, where it is the
        # CDN's address -- so every visitor in the world collapses into one
        # bucket and the limiter becomes a GLOBAL 30/minute cap. Not a bypass; a
        # self-inflicted outage the first time the page gets attention.
        self.assertEqual(SP.client_key(self.XFF, "10.0.0.1", 2), "203.0.113.9")
        self.assertEqual(SP.client_key(self.XFF, "10.0.0.1", 1), "172.71.1.1")

    def test_a_shorter_chain_than_claimed_falls_back_to_the_raw_peer(self):
        # Mutation: indexing anyway, or taking the leftmost. The leftmost entry
        # is written by the CLIENT, so trusting it when the chain is shorter than
        # claimed hands every visitor a forgeable identity and voids the limit.
        self.assertEqual(SP.client_key("1.1.1.1", "10.0.0.1", 3), "10.0.0.1")
        self.assertEqual(SP.client_key(None, "10.0.0.1", 2), "10.0.0.1")
        self.assertEqual(SP.client_key("", "10.0.0.1", 1), "10.0.0.1")

    def test_a_junk_depth_falls_back_to_the_safe_default(self):
        for depth in (0, -5, None, "two"):
            self.assertTrue(SP.client_key(self.XFF, "10.0.0.1", depth))

    def test_the_default_is_the_under_stating_one(self):
        # Under-stating groups clients together; over-stating trusts an extra
        # attacker-written entry. The default must err the safe way.
        self.assertEqual(SP.DEFAULT_PROXY_DEPTH, 1)

    def test_the_depth_reaches_the_handler(self):
        # Mutation: adding the flag and never binding it -- the wired-and-inert
        # pattern, which this repo has now hit five times.
        self.assertEqual(SP._Handler.proxy_depth, SP.DEFAULT_PROXY_DEPTH)
        import inspect
        self.assertIn("proxy_depth", inspect.getsource(SP.serve_forever))


class TestCache(unittest.TestCase):
    def test_a_repeat_request_does_not_re_probe_the_seller(self):
        # Mutation: no cache. Every page refresh would hit a stranger's endpoint,
        # which makes a public URL a way to aim traffic at a third party.
        calls = []
        portal = SP.Portal(rows=corpus(), coverage={}, category_index={},
                           clock=lambda: 1000.0)
        portal._probe_fn = lambda resources: calls.append(1)
        portal.report("api.example.com")
        portal.report("api.example.com")
        self.assertEqual(len(calls), 1)

    def test_entries_expire(self):
        now = [1000.0]
        cache = SP.ReportCache(ttl=10.0)
        cache.put("k", "v", now[0])
        self.assertEqual(cache.get("k", 1009.0), "v")
        self.assertIsNone(cache.get("k", 1010.0))

    def test_the_cache_is_bounded(self):
        # Mutation: unbounded. The key space is attacker-chosen, so a caller
        # asking for a million distinct keys would grow the process until it dies.
        cache = SP.ReportCache(max_entries=3)
        for i in range(10):
            cache.put("k%d" % i, i, 1.0)
        self.assertLessEqual(len(cache), 3)

    def test_misses_are_cached_too(self):
        # Mutation: caching only hits. An enumeration flood is all misses, and
        # those are exactly the requests worth making cheap.
        portal = SP.Portal(rows=corpus(), coverage={}, category_index={},
                           clock=lambda: 5.0)
        portal.report("nobody.example")
        self.assertIsNotNone(portal.cache.get("nobody.example", 5.0))


class TestGraphIsPrecomputed(unittest.TestCase):
    def test_the_payer_graph_is_never_built_on_a_request(self):
        # Mutation: building it per request. It takes MINUTES over the real
        # corpus, so the first visitor would hang and so would everyone after.
        portal = SP.Portal(rows=corpus(), coverage={}, category_index={})
        portal.graph = {PAYEE: {"distinct_payers": 5, "established_payers": 3}}
        signal, err = portal._cross_fn(PAYEE)
        self.assertEqual(signal["established_payers"], 3)
        self.assertIsNone(err)

    def test_the_graph_lookup_is_case_insensitive(self):
        # Mutation: exact-match lookup. A live 402 returns EIP-55 checksummed
        # while the crawl stores lowercase -- the join that missed 64 of 69
        # endpoints in advertised_prices, which would silently blank the one
        # finding a seller cannot get anywhere else.
        portal = SP.Portal(rows=corpus(), coverage={}, category_index={})
        portal.graph = {PAYEE.lower(): {"established_payers": 3}}
        self.assertIsNotNone(portal._cross_fn(PAYEE.upper())[0])

    def test_a_broken_store_fails_soft_and_loud(self):
        # Mutation: letting the exception escape. The portal must still answer;
        # the demand finding says the graph could not be read.
        portal = SP.Portal(rows=corpus(), coverage={}, category_index={},
                           store_path="/nonexistent/nope.db")
        self.assertIsNotNone(portal.graph_error)
        self.assertEqual(portal.graph, {})


class TestLiveServer(unittest.TestCase):
    """A REAL server on a real socket -- the wired-and-inert guard."""

    @classmethod
    def setUpClass(cls):
        from ratelimit import RateLimiter
        portal = SP.Portal(rows=corpus(), coverage={}, category_index={},
                           probe=False)
        handler = type("_T", (SP._Handler,),
                       {"portal": portal, "limiter": RateLimiter(1000, 60.0)})
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get_noredirect(self, path):
        """urlopen FOLLOWS redirects, so the default helper never sees a 302 --
        it reports the followed 200 and no Location. The redirect IS the
        behaviour under test here (it is what makes a report shareable, and
        where an open redirect would live), so it needs an opener that stops."""
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None
        opener = urllib.request.build_opener(_NoRedirect)
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with opener.open(url, timeout=10) as r:
                return r.status, r.read().decode("utf-8"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8"), dict(e.headers or {})

    def get(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.status, r.read().decode("utf-8"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8"), dict(e.headers or {})

    def test_health_and_home(self):
        self.assertEqual(self.get("/healthz")[0], 200)
        status, body, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Why agents are not paying you", body)

    def test_a_report_renders(self):
        status, body, headers = self.get("/r/api.example.com")
        self.assertEqual(status, 200)
        self.assertIn("Findings", body)
        self.assertIn("text/html", headers["Content-Type"])

    def test_json_is_available_for_machines(self):
        status, body, headers = self.get("/r/api.example.com?format=json")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertTrue(json.loads(body)["found"])

    def test_a_hostile_key_is_escaped_on_the_wire(self):
        # Mutation: escaping in the renderer but not on the served path. This is
        # the end-to-end version of the XSS test -- it would catch a second,
        # unescaped render path being added later.
        status, body, _ = self.get("/r/%3Cscript%3Ealert(1)%3C/script%3E")
        self.assertNotIn("<script>alert(1)", body)

    def test_the_security_headers_are_actually_sent(self):
        # Mutation: defining the headers and never sending them -- the
        # wired-and-inert pattern, which this repo has now hit four times.
        _, _, headers = self.get("/r/api.example.com")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_the_form_redirects_to_a_shareable_url(self):
        status, _, headers = self.get_noredirect("/go?key=api.example.com")
        self.assertIn(status, (301, 302))
        self.assertEqual(headers["Location"], "/r/api.example.com")

    def test_the_redirect_cannot_leave_our_host(self):
        # Mutation: not encoding the key into the Location. `//evil.com` would
        # then become a protocol-relative URL and the form an open redirect.
        _, _, headers = self.get_noredirect("/go?key=%2F%2Fevil.com")
        location = headers["Location"]
        self.assertTrue(location.startswith("/r/"), location)
        self.assertNotIn("//evil.com", location)

    def test_junk_routes_404(self):
        self.assertEqual(self.get("/rogue")[0], 404)
        self.assertEqual(self.get("/r/")[0], 400)

    def test_an_oversize_key_is_refused_on_the_wire(self):
        self.assertEqual(self.get("/r/" + "a" * 5000)[0], 400)


class TestRateLimit(unittest.TestCase):
    def test_a_flood_is_refused_with_retry_after(self):
        # Mutation: no limiter. Every report makes a live request to a stranger's
        # endpoint, so an unlimited public URL is a way to aim traffic at them.
        from ratelimit import RateLimiter
        portal = SP.Portal(rows=corpus(), coverage={}, category_index={},
                           probe=False)
        handler = type("_L", (SP._Handler,),
                       {"portal": portal, "limiter": RateLimiter(1, 60.0, burst=1)})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            codes = []
            for _ in range(3):
                try:
                    with urllib.request.urlopen(
                            "http://127.0.0.1:%d/r/api.example.com" % port,
                            timeout=10) as r:
                        codes.append(r.status)
                except urllib.error.HTTPError as e:
                    codes.append(e.code)
                    self.assertIsNotNone(e.headers.get("Retry-After"))
            self.assertIn(429, codes)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
