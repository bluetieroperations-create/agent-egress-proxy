"""Tests for bounded_server.py -- admission control.

Convention (CLAUDE.md): each test names the MUTATION it kills. The properties
here are about what happens when the box is ALREADY past what it can serve, so
every one of them drives a REAL server at real concurrency rather than asserting
on a mock -- the failure this module exists to prevent only appears under load.
"""
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

import bounded_server
from bounded_server import BoundedThreadingHTTPServer


def make_handler(hold, started, release, hold_health=False):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _serve(self):
            # CONSUME THE BODY. Without this, keep-alive re-parses the unread
            # remainder as the next request line (REQUEST_URI_TOO_LONG), which
            # both spams the suite with tracebacks and means the RST path is
            # never actually exercised.
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                remaining = n
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            self.close_connection = True
            started.release()          # signal: this request is IN FLIGHT
            # A health check is CHEAP and must not occupy the slow path -- both
            # realistic and necessary: exempt requests reach this same handler,
            # so holding here made each /healthz probe wait the full 30s (the
            # health test took 150s before this).
            if hold and (hold_health or not self.path.startswith("/healthz")):
                release.wait(30)       # occupy a permit until told otherwise
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _serve
        # The RST regression only reproduces with a request BODY in flight, so
        # the handler has to accept POSTs -- without this the raw-socket test
        # gets a 501 from BaseHTTPRequestHandler and never occupies a permit.
        do_POST = _serve

        def log_message(self, *a):
            pass
    return H


class Admission(unittest.TestCase):

    def setUp(self):
        self.release = threading.Event()
        self.started = threading.Semaphore(0)
        self.srv = None

    def tearDown(self):
        self.release.set()
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()

    def serve(self, max_inflight, hold=True, hold_health=False):
        H = make_handler(hold, self.started, self.release, hold_health)
        self.srv = BoundedThreadingHTTPServer(("127.0.0.1", 0), H,
                                              max_inflight=max_inflight)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d" % self.srv.server_address[1]

    def get(self, url, out, i):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                out[i] = (r.status, r.read())
        except urllib.error.HTTPError as e:
            out[i] = (e.code, e.read())
        except Exception as e:
            out[i] = ("ERR", str(e).encode())

    def test_excess_requests_get_503_not_a_hang(self):
        """THE POINT. Past the ceiling a caller must be TOLD to retry, promptly.
        MUTATION: removing the acquire() check -- every request is then admitted,
        nothing returns 503, and overload silently becomes latency (and, on a
        real host, an edge 502 the caller cannot distinguish from broken)."""
        url = self.serve(max_inflight=3)
        out = {}
        th = [threading.Thread(target=self.get, args=(url, out, i)) for i in range(12)]
        for t in th:
            t.start()
        # Let the 3 admitted requests actually occupy their permits first.
        for _ in range(3):
            self.assertTrue(self.started.acquire(timeout=10))
        # Give the rest time to be shed (they are refused without a thread).
        deadline = time.time() + 10
        while time.time() < deadline and sum(1 for v in out.values()
                                             if v[0] == 503) < 9:
            time.sleep(0.05)
        self.release.set()
        for t in th:
            t.join(20)
        codes = [v[0] for v in out.values()]
        self.assertEqual(codes.count(503), 9, codes)
        self.assertEqual(codes.count(200), 3, codes)

    def post_raw(self, host, port, out, i, body_bytes=200000):
        """A POST whose body is still being WRITTEN when the server answers.

        This is what a real client does and what `urllib` with a GET does NOT:
        the RST only happens when the server closes a socket that still has
        unread inbound data. Testing with a bodyless GET is why the first
        version of this test passed with the bug still in place.
        """
        import socket as _s
        try:
            c = _s.create_connection((host, port), timeout=20)
            c.sendall(b"POST /x HTTP/1.1\r\nHost: t\r\n"
                      b"Content-Type: application/json\r\n"
                      b"Content-Length: " + str(body_bytes).encode() + b"\r\n\r\n")
            chunk = b"a" * 8192
            sent = 0
            try:
                while sent < body_bytes:
                    c.sendall(chunk[:min(8192, body_bytes - sent)])
                    sent += 8192
            except OSError:
                pass            # server answered early; that is allowed
            data = b""
            while b"\r\n\r\n" not in data:
                b = c.recv(4096)
                if not b:
                    break
                data += b
            c.close()
            out[i] = data.split(b"\r\n", 1)[0].decode(errors="replace") or "EMPTY"
        except Exception as e:
            out[i] = "ERR:%s" % type(e).__name__

    def test_every_client_gets_an_HTTP_RESPONSE_not_a_reset(self):
        """SMOKE ONLY -- read the limitation before trusting this.

        60 simultaneous POSTs with a body, against a ceiling of 2: every client
        must come away with an HTTP status line rather than a transport error.

        WHAT THIS DOES NOT COVER, stated because the first version of this test
        silently claimed that it did. The real defect -- 3 `Broken pipe` + 2
        `Connection reset by peer` out of 50 on a live server -- has two causes,
        and MUTATION TESTING SHOWS NEITHER IS KILLED HERE:

          * `request_queue_size = 5` (the TCP listen backlog). Loopback accepts
            too fast to overflow a backlog from Python threads.
          * closing a socket that still holds unread inbound data, which makes
            the kernel send an RST that destroys the 503 already written.
            A 200 KB body fits in loopback socket buffers, so `sendall` finishes
            before the server answers and nothing is ever in flight.

        Both were verified on the REAL path instead (a live server behind the
        platform's proxy): 5 transport errors before the fix, 0 after, with
        `Retry-After: 2` present on every shed response. That measurement is the
        evidence for those two lines, not this test. Reproducing them in a unit
        test needs a real network path or an injected slow-loris client; until
        one exists, do not read a green run here as covering them.
        """
        url = self.serve(max_inflight=2)
        host, port = "127.0.0.1", self.srv.server_address[1]
        out = {}
        th = [threading.Thread(target=self.post_raw, args=(host, port, out, i))
              for i in range(60)]
        for t in th:
            t.start()
        for _ in range(2):
            self.assertTrue(self.started.acquire(timeout=15))
        self.release.set()
        for t in th:
            t.join(30)
        errs = {k: v for k, v in out.items()
                if str(v).startswith("ERR") or v == "EMPTY"}
        self.assertEqual(errs, {}, "clients got a transport error / empty reply")
        self.assertEqual(len(out), 60)
        self.assertTrue(all("HTTP/1." in v for v in out.values()),
                        "statuses seen: %r" % (sorted(set(out.values()))[:6],))

    def test_health_checks_are_NEVER_shed(self):
        """AUDIT FINDING (high). /healthz went through the same ceiling as every
        other request, so under saturation it could receive a 503 -- and a
        platform that restarts an instance on a failed health check would turn
        load-shedding into an OUTAGE, strictly worse than the 502s this module
        replaces. It measured clean live (25/25 while 34 verdicts shed) only
        because verdicts are 3.4ms and permits turned over between probes: that
        is timing luck, not a property.
        MUTATION: removing the _is_exempt() check -- health is then shed exactly
        when the box is under the load that makes the check matter most."""
        url = self.serve(max_inflight=1)          # 1 permit, held by the first
        out = {}
        blocker = threading.Thread(target=self.get, args=(url, out, "held"))
        blocker.start()
        self.assertTrue(self.started.acquire(timeout=10))   # permit occupied

        # Ceiling is now fully saturated. A normal request must be shed...
        self.get(url, out, "normal")
        self.assertEqual(out["normal"][0], 503)
        # ...but a health check must still be served.
        for i in range(5):
            self.get(url + "/healthz", out, "hc%d" % i)
        self.release.set()
        blocker.join(20)
        hc = [out["hc%d" % i][0] for i in range(5)]
        self.assertEqual(hc, [200] * 5, "a health check was shed: %r" % hc)

    def test_exempt_path_is_itself_bounded(self):
        """MUTATION: exempting without a cap -- a flood of GET /healthz would
        then spawn unbounded threads and restore the very problem the ceiling
        exists to prevent. An exemption is not a bypass.

        `hold_health=True` is load-bearing: health checks must actually OCCUPY
        the exempt path for the cap to be reachable. An earlier version used the
        fast health handler, so every exempt request finished instantly, the cap
        was never hit, and mutation testing showed removing it changed nothing.
        """
        url = self.serve(max_inflight=1, hold_health=True)
        out = {}
        n = bounded_server.MAX_EXEMPT_INFLIGHT + 5
        th = [threading.Thread(target=self.get, args=(url + "/healthz", out, i))
              for i in range(n)]
        for t in th:
            t.start()
        # In flight at once = MAX_EXEMPT_INFLIGHT on the exempt path, PLUS the
        # ceiling itself, because overflow falls through to normal admission
        # rather than being dropped.
        ceiling = 1
        for _ in range(bounded_server.MAX_EXEMPT_INFLIGHT + ceiling):
            self.assertTrue(self.started.acquire(timeout=15))
        # ...and NOT ONE MORE. Without the cap every extra sails through the
        # exempt path too, and this is the assertion that dies.
        self.assertFalse(
            self.started.acquire(timeout=2.0),
            "more than MAX_EXEMPT_INFLIGHT + ceiling were served at once "
            "-- the exempt path is unbounded")
        self.release.set()
        for t in th:
            t.join(25)
        self.assertEqual(len(out), n)
        self.assertTrue(all(v[0] in (200, 503) for v in out.values()),
                        sorted({v[0] for v in out.values()}))

    def test_refusal_slot_is_returned_when_a_thread_cannot_start(self):
        """MUTATION: incrementing _refusing before start() without a rollback.
        Under thread exhaustion the slot is never returned, and after
        MAX_REFUSE_THREADS such failures NO refusal is ever drained again -- the
        connection resets come back permanently, long after the load that caused
        them is gone."""
        H = make_handler(False, self.started, self.release)
        srv = BoundedThreadingHTTPServer(("127.0.0.1", 0), H, max_inflight=1)
        try:
            real = threading.Thread

            class Boom:
                def __init__(self, *a, **k):
                    pass

                def start(self):
                    raise RuntimeError("can't start new thread")

            threading.Thread = Boom
            try:
                ok = srv._start_refusal(object())
            finally:
                threading.Thread = real
            self.assertFalse(ok)
            self.assertEqual(srv._refusing, 0, "refusal slot leaked")
        finally:
            srv.server_close()

    def test_503_carries_retry_after_and_a_json_body(self):
        """A shed response must be ACTIONABLE. MUTATION: dropping Retry-After,
        or returning a bare status with no body -- a client then has no basis to
        back off and no way to log what happened."""
        url = self.serve(max_inflight=1)
        out = {}
        th = [threading.Thread(target=self.get, args=(url, out, i)) for i in range(4)]
        for t in th:
            t.start()
        self.assertTrue(self.started.acquire(timeout=10))
        deadline = time.time() + 10
        while time.time() < deadline and not any(v[0] == 503 for v in out.values()):
            time.sleep(0.05)
        # Fetch one directly so we can read the headers off the error.
        try:
            urllib.request.urlopen(url, timeout=10)
            hdrs = None
        except urllib.error.HTTPError as e:
            hdrs, body = e.headers, e.read()
            self.assertEqual(e.code, 503)
            self.assertEqual(hdrs.get("Retry-After"),
                             str(bounded_server.RETRY_AFTER_SECONDS))
            self.assertTrue(json.loads(body)["error"])
        self.release.set()
        for t in th:
            t.join(20)
        self.assertIsNotNone(hdrs, "expected a 503 while the permit was held")

    def test_permits_are_returned_so_the_server_recovers(self):
        """MUTATION: never releasing (or releasing only on the happy path). The
        ceiling then ratchets down until the service refuses EVERYTHING -- a
        far worse failure than the one being fixed, and one that only appears
        after the server has been up a while."""
        url = self.serve(max_inflight=2, hold=False)
        out = {}
        for round_ in range(4):
            th = [threading.Thread(target=self.get, args=(url, out, "%d-%d" % (round_, i)))
                  for i in range(2)]
            for t in th:
                t.start()
            for t in th:
                t.join(20)
        codes = [v[0] for v in out.values()]
        self.assertEqual(codes.count(200), 8, codes)
        self.assertEqual(self.srv.shed_count, 0)

    def test_shedding_is_counted(self):
        """MUTATION: shedding silently. An operator must be able to see that the
        ceiling is being hit -- otherwise 'we return 503 sometimes' is invisible
        and nobody learns the box needs to be bigger."""
        url = self.serve(max_inflight=1)
        out = {}
        th = [threading.Thread(target=self.get, args=(url, out, i)) for i in range(5)]
        for t in th:
            t.start()
        self.assertTrue(self.started.acquire(timeout=10))
        deadline = time.time() + 10
        while time.time() < deadline and self.srv.shed_count < 4:
            time.sleep(0.05)
        self.release.set()
        for t in th:
            t.join(20)
        self.assertEqual(self.srv.shed_count, 4)

    def test_admitted_requests_are_not_slowed_by_the_shed_ones(self):
        """The REASON to shed: work you admit should finish at its normal speed
        rather than competing with work you cannot finish.
        MUTATION: queueing the excess (blocking acquire) instead of refusing --
        the admitted requests then wait behind a backlog and the latency the
        ceiling was meant to protect goes away."""
        url = self.serve(max_inflight=2, hold=False)
        out = {}
        t0 = time.time()
        th = [threading.Thread(target=self.get, args=(url, out, i)) for i in range(40)]
        for t in th:
            t.start()
        for t in th:
            t.join(30)
        elapsed = time.time() - t0
        codes = [v[0] for v in out.values()]
        self.assertEqual(len(out), 40)
        self.assertEqual(codes.count(200) + codes.count(503), 40, codes)
        # 40 requests against a ceiling of 2 must resolve fast BECAUSE most are
        # refused outright -- a blocking queue would serialize all 40.
        self.assertLess(elapsed, 15.0)

    def test_unbalanced_release_raises_rather_than_lifting_the_ceiling(self):
        """MUTATION: threading.Semaphore instead of BoundedSemaphore. A stray
        release would then silently RAISE the ceiling forever, restoring exactly
        the unbounded behaviour this module exists to prevent -- and nothing
        would report it."""
        H = make_handler(False, self.started, self.release)
        srv = BoundedThreadingHTTPServer(("127.0.0.1", 0), H, max_inflight=2)
        try:
            with self.assertRaises(ValueError):
                srv._permits.release()      # one more than were ever acquired
        finally:
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
