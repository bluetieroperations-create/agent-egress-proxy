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
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._permits.acquire(blocking=False):
            with self._shed_lock:
                self.shed_count += 1
            # Off the accept thread: see MAX_REFUSE_THREADS.
            if self._start_refusal(request):
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

    def _start_refusal(self, request):
        """Hand the refusal to a short-lived thread. False if at the cap."""
        with self._refuse_lock:
            if self._refusing >= MAX_REFUSE_THREADS:
                return False
            self._refusing += 1
        threading.Thread(target=self._refuse_and_close, args=(request,),
                         daemon=True).start()
        return True

    def _refuse_and_close(self, request):
        try:
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
