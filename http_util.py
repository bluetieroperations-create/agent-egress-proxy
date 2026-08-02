#!/usr/bin/env python3
"""
http_util.py -- hardened JSON GET for the live data path (stdlib only).

Two guarantees the raw `urlopen` calls in the crawl/backfill path didn't give:

  * RETRY with exponential backoff on TRANSIENT failures (HTTP 429/5xx, timeouts,
    connection errors) so a rate-limited public API (Blockscout, the CDP Bazaar)
    doesn't silently drop a payee's history. Honors `Retry-After` on a 429.
    PERMANENT 4xx (404/400/403/...) are raised immediately, never retried.
  * a READ-SIZE CAP -- `urlopen().read()` is otherwise unbounded, so a hostile or
    runaway endpoint could exhaust memory. Reading past the cap is a hard error
    (`ResponseTooLarge`), and it is deterministic, so it is NOT retried.

The transport (`opener`) and `sleep` are injectable, so the retry ladder is unit-
tested with no network and no real waiting.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 12
DEFAULT_UA = "Blackwall/0.1"
DEFAULT_MAX_BYTES = 8 * 1024 * 1024      # 8 MiB -- a Bazaar/Blockscout page is << this
DEFAULT_RETRIES = 3                       # total attempts = retries + 1
DEFAULT_BACKOFF = 0.5                     # seconds; doubles each retry
MAX_BACKOFF = 8.0                         # cap a single wait (also caps Retry-After)

# Transient HTTP statuses worth retrying. Everything else (404, 400, 403, 401...)
# is permanent -- retrying it just wastes the rate-limit budget.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ResponseTooLarge(Exception):
    """The response body exceeded the read-size cap."""


def _retry_after_seconds(err):
    """`Retry-After` (seconds form) from a 429/503, clamped, or None."""
    try:
        v = err.headers.get("Retry-After") if err.headers else None
        return min(float(v), MAX_BACKOFF) if v is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def read_capped(resp, max_bytes=DEFAULT_MAX_BYTES):
    """Read at most `max_bytes` from a response; error if there is more. Reads one
    extra byte so 'exactly at the cap' passes and 'one over' fails."""
    data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ResponseTooLarge("response exceeded %d bytes" % max_bytes)
    return data


def get_json(url, *, timeout=DEFAULT_TIMEOUT, user_agent=DEFAULT_UA,
             max_bytes=DEFAULT_MAX_BYTES, retries=DEFAULT_RETRIES,
             backoff=DEFAULT_BACKOFF, opener=None, sleep=None, headers=None):
    """GET `url`, parse JSON, with bounded retry/backoff and a read-size cap.

    Retries transient failures (429/5xx, timeout, connection error) up to `retries`
    times; a permanent 4xx or an oversize/parse error is raised immediately. After
    exhausting retries the last transient error is raised."""
    _open = opener or urllib.request.urlopen
    _sleep = sleep or time.sleep
    hdrs = {"User-Agent": user_agent, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    delay = backoff
    last = None
    for attempt in range(retries + 1):
        try:
            with _open(req, timeout=timeout) as r:
                return json.loads(read_capped(r, max_bytes).decode("utf-8"))
        except urllib.error.HTTPError as e:            # HTTPError IS-A URLError: first
            last = e
            if e.code not in RETRYABLE_STATUS or attempt == retries:
                raise
            wait = _retry_after_seconds(e)
            _sleep(wait if wait is not None else min(delay, MAX_BACKOFF))
            delay *= 2
        except (urllib.error.URLError, TimeoutError) as e:
            # URLError wraps connection refused/reset/DNS; socket.timeout is a
            # TimeoutError subclass on modern Python. Both are transient.
            last = e
            if attempt == retries:
                raise
            _sleep(min(delay, MAX_BACKOFF))
            delay *= 2
    raise last            # defensive: the loop returns or raises above
