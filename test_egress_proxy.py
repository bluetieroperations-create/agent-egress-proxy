"""
Unit tests for egress_proxy pure security-boundary functions.

Run: python -m unittest test_egress_proxy.py -v

These three functions ARE the security boundary; tests are written TDD-first.
Each test class notes the mutation it kills.
"""
import unittest

import egress_proxy as ep


class TestParseConnectTarget(unittest.TestCase):
    """
    parse_connect_target: parse `CONNECT host:port HTTP/1.1` -> (host, port) | None.

    Mutation notes (these tests fail if impl is mutated):
      - Drop the CRLF/control-char check  -> the CRLF-injection / control-char
        tests below FAIL (request-smuggling guard gone).
      - Drop the port range check (1..65535) -> port 0 / 70000 / -1 tests FAIL.
      - Drop the verb check               -> the GET/garbage tests FAIL.
      - Drop the host-length cap          -> the oversized-host test FAILS.
      - int(port) without strict digits   -> "44 3" / "4a3" tests FAIL.
    """

    def test_valid_hostname(self):
        self.assertEqual(
            ep.parse_connect_target("CONNECT blackwalltier.com:443 HTTP/1.1"),
            ("blackwalltier.com", 443),
        )

    def test_valid_ipv4(self):
        self.assertEqual(
            ep.parse_connect_target("CONNECT 10.0.0.5:8080 HTTP/1.1"),
            ("10.0.0.5", 8080),
        )

    def test_valid_ipv6_bracketed(self):
        self.assertEqual(
            ep.parse_connect_target("CONNECT [::1]:443 HTTP/1.1"),
            ("::1", 443),
        )

    def test_valid_ipv6_full(self):
        self.assertEqual(
            ep.parse_connect_target("CONNECT [2001:db8::1]:443 HTTP/1.1"),
            ("2001:db8::1", 443),
        )

    def test_trailing_crlf_tolerated(self):
        # A real request line may arrive with a trailing CRLF already stripped,
        # but tolerate a trailing \r\n on the line itself.
        self.assertEqual(
            ep.parse_connect_target("CONNECT example.com:443 HTTP/1.1\r\n"),
            ("example.com", 443),
        )

    # ---- REJECTIONS (-> None) ----

    def test_reject_missing_port(self):
        self.assertIsNone(ep.parse_connect_target("CONNECT example.com HTTP/1.1"))

    def test_reject_nonnumeric_port(self):
        self.assertIsNone(ep.parse_connect_target("CONNECT example.com:4a3 HTTP/1.1"))

    def test_reject_port_zero(self):
        self.assertIsNone(ep.parse_connect_target("CONNECT example.com:0 HTTP/1.1"))

    def test_reject_port_too_large(self):
        self.assertIsNone(ep.parse_connect_target("CONNECT example.com:70000 HTTP/1.1"))

    def test_reject_port_negative(self):
        self.assertIsNone(ep.parse_connect_target("CONNECT example.com:-1 HTTP/1.1"))

    def test_reject_host_with_space(self):
        self.assertIsNone(ep.parse_connect_target("CONNECT ex ample.com:443 HTTP/1.1"))

    def test_reject_crlf_injection(self):
        # Classic request smuggling / header injection attempt.
        self.assertIsNone(
            ep.parse_connect_target("CONNECT evil\r\nHost: x:443 HTTP/1.1")
        )

    def test_reject_control_char_in_host(self):
        self.assertIsNone(
            ep.parse_connect_target("CONNECT ev\x00il.com:443 HTTP/1.1")
        )
        self.assertIsNone(
            ep.parse_connect_target("CONNECT ev\til.com:443 HTTP/1.1")
        )

    def test_reject_empty_host(self):
        self.assertIsNone(ep.parse_connect_target("CONNECT :443 HTTP/1.1"))

    def test_reject_oversized_host(self):
        big = "a" * 256
        self.assertIsNone(
            ep.parse_connect_target("CONNECT %s:443 HTTP/1.1" % big)
        )

    def test_accept_max_length_host(self):
        # boundary: exactly 255 chars is allowed
        host = ("a" * 251) + ".com"  # 255 chars
        self.assertEqual(len(host), 255)
        self.assertEqual(
            ep.parse_connect_target("CONNECT %s:443 HTTP/1.1" % host),
            (host, 443),
        )

    def test_reject_non_connect_verb(self):
        self.assertIsNone(
            ep.parse_connect_target("GET http://example.com/ HTTP/1.1")
        )

    def test_reject_garbage(self):
        self.assertIsNone(ep.parse_connect_target("garbage"))
        self.assertIsNone(ep.parse_connect_target(""))
        self.assertIsNone(ep.parse_connect_target("CONNECT"))
        self.assertIsNone(ep.parse_connect_target("CONNECT example.com:443"))

    def test_reject_unbracketed_ipv6(self):
        # Bare IPv6 (multiple colons, no brackets) is ambiguous -> reject.
        self.assertIsNone(
            ep.parse_connect_target("CONNECT ::1:443 HTTP/1.1")
        )

    def test_reject_port_with_space(self):
        self.assertIsNone(
            ep.parse_connect_target("CONNECT example.com:44 3 HTTP/1.1")
        )


class TestHostAllowed(unittest.TestCase):
    """
    host_allowed: dot-boundary, case-insensitive, trailing-dot-stripped.
    host == entry OR host.endswith("." + entry).

    Mutation notes:
      - Naive `host.endswith(entry)` (no dot)  -> evilblackwalltier.com test FAILS.
      - Drop case-folding                      -> BlackWallTier.COM test FAILS.
      - Drop trailing-dot strip                -> trailing-dot test FAILS.
      - `return True` on empty allowlist       -> fail-closed test FAILS.
    """

    AL = {"blackwalltier.com"}

    def test_exact_match(self):
        self.assertTrue(ep.host_allowed("blackwalltier.com", self.AL))

    def test_subdomain_match(self):
        self.assertTrue(ep.host_allowed("api.blackwalltier.com", self.AL))

    def test_deep_subdomain_match(self):
        self.assertTrue(ep.host_allowed("a.b.c.blackwalltier.com", self.AL))

    def test_suffix_bypass_rejected(self):
        # THE classic attack: evilblackwalltier.com must NOT match blackwalltier.com
        self.assertFalse(ep.host_allowed("evilblackwalltier.com", self.AL))

    def test_attacker_suffix_rejected(self):
        # allowed host as a left-label of an attacker domain
        self.assertFalse(ep.host_allowed("blackwalltier.com.attacker.com", self.AL))

    def test_case_insensitive(self):
        self.assertTrue(ep.host_allowed("BlackWallTier.COM", self.AL))
        self.assertTrue(ep.host_allowed("API.BlackWallTier.COM", self.AL))

    def test_case_insensitive_entry(self):
        self.assertTrue(ep.host_allowed("blackwalltier.com", {"BlackWallTier.COM"}))

    def test_trailing_dot_stripped_host(self):
        self.assertTrue(ep.host_allowed("blackwalltier.com.", self.AL))
        self.assertTrue(ep.host_allowed("api.blackwalltier.com.", self.AL))

    def test_trailing_dot_stripped_entry(self):
        self.assertTrue(ep.host_allowed("blackwalltier.com", {"blackwalltier.com."}))

    def test_empty_allowlist_fail_closed(self):
        self.assertFalse(ep.host_allowed("blackwalltier.com", set()))
        self.assertFalse(ep.host_allowed("anything.com", set()))

    def test_unknown_host(self):
        self.assertFalse(ep.host_allowed("google.com", self.AL))
        self.assertFalse(ep.host_allowed("notblackwalltier.com", self.AL))

    def test_empty_host(self):
        self.assertFalse(ep.host_allowed("", self.AL))

    def test_multi_entry_allowlist(self):
        al = {"blackwalltier.com", "openai.com"}
        self.assertTrue(ep.host_allowed("api.openai.com", al))
        self.assertTrue(ep.host_allowed("blackwalltier.com", al))
        self.assertFalse(ep.host_allowed("evil.com", al))

    def test_entry_not_prefix_matchable(self):
        # host shorter than entry must not match
        self.assertFalse(ep.host_allowed("com", self.AL))
        self.assertFalse(ep.host_allowed("tier.com", self.AL))


class TestDecide(unittest.TestCase):
    """
    decide(host, mode, allowlist) -> "forward" | "block".

    Mutation notes:
      - observe returning anything but "forward" -> observe tests FAIL.
      - unknown mode not failing closed          -> unknown-mode-block test FAILS.
      - enforce not consulting host_allowed       -> enforce tests FAIL.
    """

    AL = {"blackwalltier.com"}

    def test_observe_always_forwards_allowed(self):
        self.assertEqual(ep.decide("blackwalltier.com", "observe", self.AL), "forward")

    def test_observe_always_forwards_disallowed(self):
        # observe never blocks, even unknown hosts (logging happens regardless)
        self.assertEqual(ep.decide("evil.com", "observe", self.AL), "forward")

    def test_observe_forwards_empty_allowlist(self):
        self.assertEqual(ep.decide("anything.com", "observe", set()), "forward")

    def test_enforce_forwards_allowed(self):
        self.assertEqual(ep.decide("blackwalltier.com", "enforce", self.AL), "forward")
        self.assertEqual(ep.decide("api.blackwalltier.com", "enforce", self.AL), "forward")

    def test_enforce_blocks_disallowed(self):
        self.assertEqual(ep.decide("evil.com", "enforce", self.AL), "block")
        self.assertEqual(ep.decide("evilblackwalltier.com", "enforce", self.AL), "block")

    def test_enforce_blocks_empty_allowlist(self):
        self.assertEqual(ep.decide("blackwalltier.com", "enforce", set()), "block")

    def test_unknown_mode_fails_closed(self):
        # anything that isn't "observe" is treated as enforce/fail-closed
        self.assertEqual(ep.decide("evil.com", "garbage", self.AL), "block")
        self.assertEqual(ep.decide("blackwalltier.com", "garbage", self.AL), "forward")
        self.assertEqual(ep.decide("evil.com", "", self.AL), "block")
        self.assertEqual(ep.decide("evil.com", None, self.AL), "block")


class TestLoadAllowlist(unittest.TestCase):
    """load_allowlist: parse a file (comments, blanks, case-fold, trailing dot)."""

    def test_parse(self):
        import tempfile, os
        content = (
            "# comment line\n"
            "\n"
            "blackwalltier.com\n"
            "   openai.com   \n"
            "API.Example.COM\n"
            "trailingdot.com.\n"
            "# another comment\n"
        )
        fd, path = tempfile.mkstemp()
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            al = ep.load_allowlist(path)
            self.assertIn("blackwalltier.com", al)
            self.assertIn("openai.com", al)
            self.assertIn("api.example.com", al)   # case-folded
            self.assertIn("trailingdot.com", al)    # trailing dot stripped
            self.assertNotIn("# comment line", al)
            self.assertNotIn("", al)
            self.assertEqual(len(al), 4)
        finally:
            os.unlink(path)

    def test_missing_file_returns_empty(self):
        al = ep.load_allowlist("C:/nonexistent/path/does/not/exist.txt")
        self.assertEqual(al, set())


class TestNoSilentEgress(unittest.TestCase):
    """
    Server-level guarantee: every connection that reaches an upstream is
    logged, even if the relay raises mid-stream. Regression for the
    'tunnel exception suppresses the destination log line' defect.

    Mutation note: revert _handle_connect to log ONLY after _tunnel returns
    (instead of logging the destination before relaying) and this test FAILS
    with zero log lines for a connection that DID reach upstream.
    """

    def _make_proxy(self, log_path, mode="observe", allowlist=None):
        import socket as _s
        proxy = ep.EgressProxy(host="127.0.0.1", port=0, mode=mode,
                               allowlist=allowlist or set(), log_path=log_path)
        proxy._listener = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        proxy._listener.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
        proxy._listener.bind(("127.0.0.1", 0))
        proxy._port = proxy._listener.getsockname()[1]
        proxy._listener.listen(16)
        proxy._listener.settimeout(0.5)
        return proxy

    def _run(self, proxy):
        import socket as _s
        import threading as _t

        def loop():
            while not proxy._stop.is_set():
                try:
                    conn, addr = proxy._listener.accept()
                except _s.timeout:
                    continue
                except OSError:
                    break
                if not proxy._sem.acquire(blocking=False):
                    conn.close()
                    continue
                _t.Thread(target=proxy._handle_wrapped,
                          args=(conn, addr), daemon=True).start()
        th = _t.Thread(target=loop, daemon=True)
        th.start()
        return th

    def test_destination_logged_even_when_tunnel_raises(self):
        import os
        import socket as _s
        import tempfile
        import threading as _t
        import time as _time

        # throwaway upstream sink so create_connection upstream succeeds
        sink = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        sink.bind(("127.0.0.1", 0))
        sink_port = sink.getsockname()[1]
        sink.listen(4)

        def sink_loop():
            while True:
                try:
                    c, _ = sink.accept()
                except OSError:
                    break
                _t.Thread(target=lambda c=c: (c.recv(65536), c.close()),
                          daemon=True).start()
        _t.Thread(target=sink_loop, daemon=True).start()

        fd, log_path = tempfile.mkstemp()
        os.close(fd)
        os.unlink(log_path)

        # Force the tunnel to raise (simulates the select-ValueError window).
        orig = ep.EgressProxy._tunnel

        def boom(client_sock, upstream_sock):
            try:
                upstream_sock.close()
            except OSError:
                pass
            raise ValueError("file descriptor cannot be a negative integer (-1)")
        ep.EgressProxy._tunnel = staticmethod(boom)

        proxy = self._make_proxy(log_path, mode="observe")
        try:
            self._run(proxy)
            _time.sleep(0.2)
            s = _s.create_connection(("127.0.0.1", proxy._port), timeout=3)
            s.settimeout(3)
            s.sendall(("CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n"
                       % sink_port).encode())
            try:
                s.recv(100)
            except OSError:
                pass
            s.close()
            _time.sleep(0.4)
        finally:
            # Restore as a staticmethod -- `orig` is the underlying plain
            # function (a staticmethod accessed via the class unwraps to it),
            # so re-binding it bare would turn _tunnel back into an instance
            # method and pass `self`, polluting every later test. Re-wrap it.
            ep.EgressProxy._tunnel = staticmethod(orig)
            proxy.shutdown()
            sink.close()

        self.assertTrue(os.path.exists(log_path), "no log file written at all")
        with open(log_path, encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        os.unlink(log_path)
        # The destination reached upstream; it MUST appear in the log despite
        # the tunnel exception.
        self.assertTrue(
            any('"host":"127.0.0.1"' in l and '"port":%d' % sink_port in l
                for l in lines),
            "silent egress: connection reached upstream but no destination "
            "log line was written (lines=%r)" % lines,
        )


class _Sentinel:
    """
    A throwaway loopback upstream that records whether it was ever connected
    to. A blocked/rejected host must produce ZERO connections here -- that is
    the real "a blocked host is never reached" guarantee.
    """

    def __init__(self):
        import socket as _s
        import threading as _t
        self._s = _s
        self._t = _t
        self.connections = 0
        self._lock = _t.Lock()
        self._stop = False
        self.sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        self.sock.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.host, self.port = self.sock.getsockname()
        self.sock.listen(8)
        self.sock.settimeout(0.3)
        self._th = _t.Thread(target=self._loop, daemon=True)
        self._th.start()

    def _loop(self):
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except self._s.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self.connections += 1

            def serve(c=c):
                try:
                    # Echo a tiny HTTP reply so a plain-HTTP relay completes,
                    # and so a CONNECT tunnel has something to carry.
                    data = c.recv(65536)
                    if data:
                        c.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                            b"Connection: close\r\n\r\nhi"
                        )
                except OSError:
                    pass
                finally:
                    try:
                        c.close()
                    except OSError:
                        pass
            self._t.Thread(target=serve, daemon=True).start()

    @property
    def hits(self):
        with self._lock:
            return self.connections

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class TestServerWiring(unittest.TestCase):
    """
    SERVER-LEVEL integration tests: boot the REAL EgressProxy.serve_forever()
    on an ephemeral 127.0.0.1 port and drive real client sockets through it.
    The pure-function tests above cover parse/allow/decide; these cover the
    WIRING that connects them to the socket layer -- the regressions that
    would otherwise stay green.

    Each test names the mutation it KILLS (verified red-then-green by hand:
    temporarily break the wiring, confirm the test fails, restore).
    """

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp(prefix="egress_wire_")
        self._proxies = []
        self._sentinels = []

    def tearDown(self):
        import shutil
        for p in self._proxies:
            try:
                p.shutdown()
            except Exception:
                pass
        for s in self._sentinels:
            try:
                s.close()
            except Exception:
                pass
        try:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
        except Exception:
            pass

    # ---- helpers ----
    def _sentinel(self):
        s = _Sentinel()
        self._sentinels.append(s)
        return s

    def _boot(self, mode="observe", allowlist=None, log_name="egress.log"):
        import os
        import threading as _t
        import time as _time
        log_path = os.path.join(self._tmpdir, log_name)
        proxy = ep.EgressProxy(host="127.0.0.1", port=0, mode=mode,
                               allowlist=allowlist or set(), log_path=log_path)
        self._proxies.append(proxy)
        th = _t.Thread(target=proxy.serve_forever, daemon=True)
        th.start()
        # Wait for the listener to bind and serve_forever to record the port.
        deadline = _time.time() + 5.0
        while _time.time() < deadline:
            if proxy._listener is not None and proxy.port != 0:
                break
            _time.sleep(0.01)
        self.assertNotEqual(proxy.port, 0, "proxy never bound an ephemeral port")
        return proxy, log_path

    def _read_log(self, log_path):
        import json
        import os
        if not os.path.exists(log_path):
            return []
        recs = []
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    recs.append(json.loads(line))
        return recs

    def _client(self, port):
        import socket as _s
        c = _s.create_connection(("127.0.0.1", port), timeout=5)
        c.settimeout(5)
        return c

    def _recv_status(self, c):
        """Read the first response line and return it as text."""
        buf = b""
        try:
            while b"\r\n" not in buf and len(buf) < 4096:
                chunk = c.recv(256)
                if not chunk:
                    break
                buf += chunk
        except OSError:
            pass
        return buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")

    def _settle(self):
        import time as _time
        _time.sleep(0.4)

    # ---- W1: enforce BLOCKS without ever connecting upstream ----
    def test_W1_enforce_blocks_without_connecting(self):
        """
        enforce + non-allowlisted host -> client gets 403 AND the sentinel
        upstream records ZERO connections.

        KILLS: removing the `if action == "block"` short-circuit in
        _handle_connect (so a blocked host still calls create_connection).
        If the block path were dropped, the sentinel hit count goes to 1 and
        this test FAILS.
        """
        sentinel = self._sentinel()
        # allowlist intentionally does NOT contain the sentinel host
        proxy, _log = self._boot(mode="enforce", allowlist={"never-allowed.example"})
        c = self._client(proxy.port)
        c.sendall(("CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n" % sentinel.port).encode())
        status = self._recv_status(c)
        c.close()
        self._settle()
        self.assertIn("403", status, "enforce did not return 403 for blocked host")
        self.assertEqual(sentinel.hits, 0,
                         "BLOCKED host was still reached upstream (silent egress)")

    # ---- W1b: enforce ALLOWS an allowlisted host (tunnel works + logs) ----
    def test_W1b_enforce_allows_allowlisted(self):
        """
        enforce + allowlisted host (allowlist points at the sentinel) ->
        upstream IS reached, tunnel works, and a decision=allow log line exists.

        KILLS: enforce not consulting host_allowed / always blocking. If the
        allow path were broken, sentinel.hits stays 0 and/or no allow line is
        logged and this test FAILS.
        """
        sentinel = self._sentinel()
        proxy, log_path = self._boot(mode="enforce", allowlist={sentinel.host})
        c = self._client(proxy.port)
        c.sendall(("CONNECT %s:%d HTTP/1.1\r\n\r\n"
                   % (sentinel.host, sentinel.port)).encode())
        status = self._recv_status(c)
        # drive a byte through the established tunnel
        if "200" in status:
            try:
                c.sendall(b"GET / HTTP/1.0\r\n\r\n")
                c.recv(256)
            except OSError:
                pass
        c.close()
        self._settle()
        self.assertIn("200", status, "tunnel was not established for allowed host")
        self.assertGreaterEqual(sentinel.hits, 1,
                                "allowed host never reached upstream")
        recs = self._read_log(log_path)
        self.assertTrue(
            any(r.get("decision") == "allow" and r.get("host") == sentinel.host
                for r in recs),
            "no decision=allow log line for the allowed host (recs=%r)" % recs,
        )

    # ---- W2: observe LOGS every forward ----
    def test_W2_observe_logs_every_forward(self):
        """
        observe + any host -> forwarded AND a log line written naming the dest.

        KILLS: dropping the _log call on the observe-forward path. If logging
        were removed, the JSONL file has no line for this destination and this
        test FAILS.
        """
        sentinel = self._sentinel()
        proxy, log_path = self._boot(mode="observe")
        c = self._client(proxy.port)
        c.sendall(("CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n" % sentinel.port).encode())
        status = self._recv_status(c)
        c.close()
        self._settle()
        self.assertIn("200", status, "observe did not establish the tunnel")
        self.assertGreaterEqual(sentinel.hits, 1, "observe did not forward")
        recs = self._read_log(log_path)
        self.assertTrue(
            any(r.get("host") == "127.0.0.1" and r.get("port") == sentinel.port
                for r in recs),
            "observe forwarded but wrote no log line for the destination "
            "(recs=%r)" % recs,
        )

    # ---- W3: listener binds 127.0.0.1 only ----
    def test_W3_binds_localhost_only(self):
        """
        The listening socket's bound address is 127.0.0.1, never 0.0.0.0.

        KILLS: changing the bind host to "" / "0.0.0.0". getsockname()[0]
        would then be "0.0.0.0" and this test FAILS. (There is exactly one
        bind() in serve_forever, and it uses self.host which main() pins to
        127.0.0.1; the constructor default is also 127.0.0.1.)
        """
        proxy, _log = self._boot(mode="observe")
        bound_ip = proxy._listener.getsockname()[0]
        self.assertEqual(bound_ip, "127.0.0.1",
                         "listener bound a non-loopback interface: %r" % bound_ip)

    # ---- W4: plain-HTTP enforce gates AND logs ----
    def test_W4_plain_http_enforce_blocks_and_logs(self):
        """
        GET http://<non-allowlisted>/ in enforce -> 403, upstream not hit,
        decision=block logged. Confirms the PLAIN-HTTP path also gates + logs,
        not just CONNECT.

        KILLS: removing the decide()/block branch in _handle_plain (plain HTTP
        forwarding without gating). The sentinel would then be hit and/or no
        block line logged, and this test FAILS.
        """
        sentinel = self._sentinel()
        proxy, log_path = self._boot(mode="enforce",
                                     allowlist={"never-allowed.example"})
        c = self._client(proxy.port)
        # absolute-form plain HTTP request to the sentinel (NOT allowlisted)
        c.sendall(("GET http://127.0.0.1:%d/ HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
                   % (sentinel.port, sentinel.port)).encode())
        status = self._recv_status(c)
        c.close()
        self._settle()
        self.assertIn("403", status, "plain-HTTP enforce did not return 403")
        self.assertEqual(sentinel.hits, 0,
                         "plain-HTTP block still reached upstream (silent egress)")
        recs = self._read_log(log_path)
        self.assertTrue(
            any(r.get("decision") == "block" and r.get("host") == "127.0.0.1"
                for r in recs),
            "plain-HTTP block was not logged with decision=block (recs=%r)" % recs,
        )

    # ---- FIX 2: every rejected ATTEMPT is logged (complete attempt record) ----
    def test_oversize_header_attempt_is_logged(self):
        """
        An oversize-header request egresses NOTHING (never connects upstream)
        but MUST still be logged with decision=reject-oversize, so a rejected
        attempt does not vanish from the observe record.

        KILLS: removing the _log(... "reject-oversize") call in _handle. The
        attempt would then leave no trace and this test FAILS.
        """
        proxy, log_path = self._boot(mode="observe")
        c = self._client(proxy.port)
        # send a CONNECT line then a flood of header bytes past MAX_HEADER_BYTES
        # without ever terminating the header block (no \r\n\r\n).
        try:
            c.sendall(b"CONNECT 127.0.0.1:9 HTTP/1.1\r\n")
            blob = (b"X-Pad: " + b"a" * 2000 + b"\r\n")
            sent = 0
            while sent < ep.MAX_HEADER_BYTES + 4096:
                c.sendall(blob)
                sent += len(blob)
        except OSError:
            pass
        try:
            self._recv_status(c)
        except OSError:
            pass
        c.close()
        self._settle()
        recs = self._read_log(log_path)
        self.assertTrue(
            any(r.get("decision") == "reject-oversize" for r in recs),
            "oversize attempt produced NO log line (recs=%r)" % recs,
        )

    def test_malformed_connect_attempt_is_logged(self):
        """
        A malformed CONNECT target egresses nothing but MUST be logged with
        decision=reject-parse.

        KILLS: removing the _log(... "reject-parse") call in _handle_connect.
        The malformed attempt would vanish and this test FAILS.
        """
        proxy, log_path = self._boot(mode="observe")
        c = self._client(proxy.port)
        # well-formed request framing (terminates headers) but a garbage target
        c.sendall(b"CONNECT not-a-valid-target HTTP/1.1\r\n\r\n")
        self._recv_status(c)
        c.close()
        self._settle()
        recs = self._read_log(log_path)
        self.assertTrue(
            any(r.get("decision") == "reject-parse" for r in recs),
            "malformed CONNECT attempt produced NO log line (recs=%r)" % recs,
        )


if __name__ == "__main__":
    unittest.main()
