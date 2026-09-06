"""
bounded_server.py -- put a CEILING on in-flight requests.

WHY, MEASURED ON THE LIVE FREE-TIER DEPLOY (2026-09-06):

    conc   200   502    p50     p95
      40    40     0   1.79s   2.78s   <- clean
      60    53     7   2.15s   3.99s   <- 12% shed
      80    59    21   2.18s   3.90s   <- 26% shed
     120    68    52   1.96s   4.76s   <- 43% shed

Note what does NOT happen: p50 stays flat at ~2s while nearly half the requests
fail. The service is not getting slow, it is DROPPING traffic -- and dropping it
as a 502 from the edge, which is indistinguishable to a caller from the service
being broken. There is no backpressure anywhere in the stack to say "busy, retry".

`ThreadingHTTPServer` is thread-per-request with NO cap: 120 concurrent requests
become 120 threads competing for a fraction of a CPU. Every one of them gets
slower, the platform's edge gives up on the slowest, and the work already done on
those requests is thrown away. Admitting work you cannot finish is worse than
refusing it.

WHAT THIS DOES AND DOES NOT BUY
-------------------------------
It does NOT raise throughput. The box does what the box does; this cannot make a
free instance faster, and anyone reading a graph should not expect more 200s.

What it changes is the SHAPE of overload:
  * accepted requests keep their latency (they are not competing with 80 others),
  * excess requests get an immediate, honest `503` + `Retry-After` instead of
    waiting to be timed out into a 502,
  * a client can tell "busy, come back" from "broken", which a 502 never says.

For a PAID endpoint that distinction is the difference between a caller retrying
and a caller concluding the service is down. It is damage control, not capacity.

The limit is deliberately a SEMAPHORE around the existing threading model rather
than a rewrite to a worker pool: it is a small, reviewable change to a server
that already works, and the failure mode of getting it wrong (refusing too early)
is safe and visible.
"""
import socket
import threading
from http.server import ThreadingHTTPServer

# Concurrency the box can serve without shedding. From the table above the knee
# on a Render free instance sits between 40 and 60; 40 is the last clean rung.
# Overridable per deploy -- a bigger box wants a bigger number, and the right
# value is always MEASURED, never guessed.
DEFAULT_MAX_INFLIGHT = 40

# Sent as Retry-After on a shed request. Short, because the queue drains in
# roughly the time one request takes (~2s measured), not in minutes.
RETRY_AFTER_SECONDS = 2

# Bound on draining a refused client's in-flight request body before closing.
# Short: this runs on the accept path, so it must never become a way to occupy
# the server.
REFUSE_DRAIN_SECONDS = 0.5

# Cap on refusals being drained concurrently. The drain must NOT run on the
# accept-loop thread (that would stall new accepts by up to REFUSE_DRAIN_SECONDS
# per shed request -- turning load-shedding into a self-inflicted outage), so it
# is handed to a short-lived thread. Those threads do NO verdict work, but they
# are still threads, so they are capped; past the cap a refusal closes
# immediately and accepts a possible RST rather than growing without bound.
MAX_REFUSE_THREADS = 32

# A HEALTH CHECK MUST NEVER BE SHED. It goes through the same door as every
# other request, so under saturation it can receive a 503 -- and a platform that
# restarts an instance on a failed health check would turn load-shedding into an
# outage, which is strictly worse than the 502s this module exists to replace.
# Measured: /healthz returned 25/25 while 34 verdicts were shed, but only
# because verdicts are 3.4ms and permits turned over between probes. That is
# timing luck, not a property.
EXEMPT_PREFIXES = (b"GET /healthz", b"HEAD /healthz")

# Exempt requests bypass the ceiling, so they need their own bound or a flood of
# GET /healthz would restore the unbounded-thread problem. A real health checker
# sends one at a time; past this they fall back to normal admission.
MAX_EXEMPT_INFLIGHT = 8

# How long the REFUSAL thread may wait for a request line before deciding a
# socket is not a health check. The accept loop must never wait (that is the
# stall bug), but by the time a refusal is being handled we are already off it
# and can afford a few ms. Without this second look the exemption is a RACE:
# measured, 3 of 12 runs shed a health check purely because its bytes had not
# reached the kernel buffer when accept() returned.
EXEMPT_RECHECK_SECONDS = 0.25

_BUSY_BODY = b'{"error":"server busy; retry shortly"}'
_BUSY_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"Content-Type: application/json\r\n"
    b"Retry-After: " + str(RETRY_AFTER_SECONDS).encode() + b"\r\n"
    b"Content-Length: " + str(len(_BUSY_BODY)).encode() + b"\r\n"
    b"Connection: close\r\n"
    b"\r\n" + _BUSY_BODY
)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that refuses rather than overcommits.

    Admission happens in `process_request`, BEFORE a thread is spawned -- the
    whole point is not to create the 120th thread. The permit is released in
    `process_request_thread`'s `finally`, which is the one place that runs for
    every admitted request however it ends.

    A shed request never acquires a permit, so it can never release one; that
    asymmetry is what keeps the counter from drifting upward over time and
    silently throttling the service to zero.
    """

    # socketserver's default is 5 -- the TCP LISTEN BACKLOG, not a request
    # queue. Measured: 50 simultaneous connects against the default produced 5
    # connection RESETS before the server ever saw them, which is exactly the
    # "indistinguishable from broken" failure this module exists to remove. The
    # backlog must comfortably exceed max_inflight so a burst QUEUES at the
    # kernel and gets an honest 503, instead of being refused at the TCP layer.
    request_queue_size = 256

    def __init__(self, *args, max_inflight=DEFAULT_MAX_INFLIGHT, **kwargs):
        self.max_inflight = int(max_inflight)
        # BoundedSemaphore, not Semaphore: an unbalanced release is a BUG that
        # would raise the ceiling forever, and it should fail loudly here rather
        # than quietly restore the behaviour this module exists to prevent.
        self._permits = threading.BoundedSemaphore(self.max_inflight)
        self.shed_count = 0
        self._shed_lock = threading.Lock()
        self._refusing = 0
        self._refuse_lock = threading.Lock()
        self._exempt = 0
        self._exempt_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def _is_exempt(self, request, timeout=None):
        """Peek at the request line. `timeout=None` means NEVER WAIT.

        Deliberately never waits: this runs on the accept loop, and blocking
        here would stall every other connection (the same mistake that made the
        refusal drain an outage). If the request line has not arrived yet the
        request simply takes the normal path -- degraded, never wrong.

        MSG_PEEK does not consume, so the handler still reads a complete request.
        """
        try:
            if timeout is None:
                request.setblocking(False)
            else:
                request.settimeout(timeout)
            try:
                head = request.recv(64, socket.MSG_PEEK)
            finally:
                request.setblocking(True)
        except (BlockingIOError, InterruptedError, OSError):
            return False
        return head.startswith(EXEMPT_PREFIXES)

    def _serve_exempt(self, request, client_address):
        """ThreadingMixIn's request body, minus any permit accounting.

        Written out rather than reusing process_request_thread because that
        method releases a permit unconditionally -- an exempt request never took
        one, and releasing it would raise on the BoundedSemaphore (or, with a
        plain Semaphore, silently raise the ceiling).
        """
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            with self._exempt_lock:
                self._exempt -= 1

    def process_request(self, request, client_address):
        # NO PEEK HERE, deliberately. An earlier version checked for a health
        # check on the accept loop with a NON-BLOCKING peek, which was a race:
        # if the request bytes had not reached the kernel buffer yet the check
        # was shed anyway (measured 3 of 12 runs). Blocking here instead is the
        # accept-loop stall bug, so neither option belongs on this path.
        #
        # It is also unnecessary. Below the ceiling a health check simply takes
        # a permit like anything else; AT the ceiling it goes to the refusal
        # thread, which rechecks with a real (blocking) peek and serves it. So
        # the exemption is enforced in exactly one place, off the hot path, and
        # mutation testing confirms it: removing the fast path changed no
        # behaviour, while removing the recheck fails the suite.
        if not self._permits.acquire(blocking=False):
            with self._shed_lock:
                self.shed_count += 1
            # Off the accept thread: see MAX_REFUSE_THREADS.
            if self._start_refusal(request, client_address):
                return
            self.shutdown_request(request)      # over the cap: close now
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # The thread was never started, so process_request_thread will not
            # run and its finally will not release. Release here or the ceiling
            # ratchets down by one on every such failure until the service
            # refuses everything.
            self._permits.release()
            raise

    def _start_refusal(self, request, client_address=None):
        """Hand the refusal to a short-lived thread. False if at the cap."""
        with self._refuse_lock:
            if self._refusing >= MAX_REFUSE_THREADS:
                return False
            self._refusing += 1
        try:
            threading.Thread(target=self._refuse_and_close,
                             args=(request, client_address),
                             daemon=True).start()
        except BaseException:
            # Under thread exhaustion start() raises. Without this the slot is
            # never returned, and after MAX_REFUSE_THREADS such failures no
            # refusal is ever drained again -- the RSTs come back permanently.
            with self._refuse_lock:
                self._refusing -= 1
            return False
        return True

    def _refuse_and_close(self, request, client_address=None):
        """Second look before refusing.

        The accept-loop peek is non-blocking and therefore RACY: a health check
        whose bytes had not yet arrived would be shed. Here we are off the accept
        loop, so we can wait briefly and serve it after all. A health check must
        never be shed -- that is the whole point of the exemption.
        """
        try:
            if client_address is not None and self._is_exempt(
                    request, timeout=EXEMPT_RECHECK_SECONDS):
                # Take a slot from the SAME budget as the fast path, so the
                # exempt bound stays MAX_EXEMPT_INFLIGHT rather than silently
                # becoming MAX_EXEMPT_INFLIGHT + MAX_REFUSE_THREADS. A raced
                # health check is still a health check, not a free pass.
                with self._exempt_lock:
                    room = self._exempt < MAX_EXEMPT_INFLIGHT
                    if room:
                        self._exempt += 1
                if room:
                    try:
                        self._serve_exempt(request, client_address)
                    finally:
                        pass          # _serve_exempt returns the slot
                    return
            self._refuse(request)
        finally:
            with self._refuse_lock:
                self._refusing -= 1

    def _refuse(self, request):
        """Send 503 and close WITHOUT resetting the connection.

        Measured: naively sendall()-then-close produced `Broken pipe` and
        `Connection reset by peer` on 3 of 50 clients. The client is typically
        still writing its request body when we answer; closing a socket with
        unread inbound data makes the kernel send an RST, which destroys the
        response we just wrote. The client then sees a transport error rather
        than the 503 -- no status, no Retry-After, nothing to act on.

        So: write the response, half-close to flush our side, then briefly drain
        whatever the client is still sending so the close is orderly.
        """
        try:
            request.sendall(_BUSY_RESPONSE)
            try:
                request.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            request.settimeout(REFUSE_DRAIN_SECONDS)
            while True:
                # Bounded: a client that keeps talking cannot hold this thread.
                if not request.recv(65536):
                    break
        except OSError:
            pass              # client already gone; nothing to tell it
        finally:
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._permits.release()
