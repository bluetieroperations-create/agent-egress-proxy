#!/usr/bin/env python3
"""
ratelimit.py -- per-client token-bucket rate limiter for the public verdict endpoint.

The free endpoint is public and unauthenticated, so a single client can hammer it
(cold-start abuse, scraping, DoS). This is a small, thread-safe token bucket keyed by
client IP: each key gets `burst` tokens that refill at `rate/per_seconds`; a request
spends one, and an empty bucket is denied (429) with a Retry-After. Memory is bounded
(LRU eviction at `max_keys`) so a many-IP flood can't grow it without limit.

PURE + injectable: `allow(key, now)` takes the clock, so it's unit-tested without
sleeping. The server passes `time.monotonic()`.
"""
from __future__ import annotations

import threading
from collections import OrderedDict


def client_ip_from(xff, peer):
    """Client identity for rate-limiting. Behind a single trusted proxy (Render), the
    RIGHTMOST X-Forwarded-For entry is the address the proxy itself recorded -- a
    client-injected XFF value sits to its LEFT, so the rightmost is spoof-resistant.
    Falls back to the raw TCP peer when there's no XFF. PURE."""
    if xff:
        parts = [p.strip() for p in str(xff).split(",") if p.strip()]
        if parts:
            return parts[-1]
    return peer or "unknown"


class RateLimiter:
    """Token bucket per key. `rate` requests per `per_seconds`; `burst` is the bucket
    capacity (max instantaneous, default = rate). Thread-safe; bounded memory."""

    def __init__(self, rate, per_seconds=60.0, *, burst=None, max_keys=10000):
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if per_seconds <= 0:
            raise ValueError("per_seconds must be > 0")
        self.capacity = float(burst if burst is not None else rate)
        self.refill_per_sec = float(rate) / float(per_seconds)
        self.max_keys = max_keys
        self._buckets = OrderedDict()   # key -> [tokens, last_ts]
        self._lock = threading.Lock()

    def allow(self, key, now):
        """Consume one token for `key` at time `now`. Returns (allowed, retry_after):
        retry_after is 0.0 when allowed, else the seconds until one token refills."""
        with self._lock:
            e = self._buckets.get(key)
            if e is None:
                if len(self._buckets) >= self.max_keys:
                    self._buckets.popitem(last=False)   # evict least-recently-used
                e = [self.capacity, now]
                self._buckets[key] = e
            tokens = min(self.capacity, e[0] + (now - e[1]) * self.refill_per_sec)
            if tokens >= 1.0:
                tokens -= 1.0
                allowed, retry = True, 0.0
            else:
                allowed, retry = False, (1.0 - tokens) / self.refill_per_sec
            e[0], e[1] = tokens, now
            self._buckets.move_to_end(key)              # mark recently used
            return allowed, retry

    def __len__(self):
        with self._lock:
            return len(self._buckets)
