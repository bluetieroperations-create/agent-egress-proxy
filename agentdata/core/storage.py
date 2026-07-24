"""
storage.py -- the cache/flywheel and the sales log.

Cache is the moat: every fetched-and-enriched result is stored keyed by
(product, version, normalized input). Coverage, latency, and margin all
compound with usage -- a repeat query costs ~0 and returns instantly.

Two backends:
  * SqliteCache  -- durable, stdlib `sqlite3`, good default for a single node.
  * MemoryCache  -- for tests / ephemeral runs.

LogStore appends one JSONL line per sale attempt (the "no silent spend"
discipline from the egress proxy, inverted to the seller side: every paid
call -- served, unpaid, or refused -- is on disk for the analytics layer).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time


class MemoryCache:
    def __init__(self):
        self._d = {}
        self._lock = threading.Lock()

    def get(self, key, now=None):
        now = time.time() if now is None else now
        with self._lock:
            row = self._d.get(key)
            if not row:
                return None
            value, expires = row
            if expires is not None and expires < now:
                self._d.pop(key, None)
                return None
            return value

    def set(self, key, value, ttl_seconds=None, now=None):
        now = time.time() if now is None else now
        expires = (now + ttl_seconds) if ttl_seconds else None
        with self._lock:
            self._d[key] = (value, expires)


class SqliteCache:
    """Durable key/value cache. Values are JSON-serializable objects."""

    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()
        # check_same_thread=False + our own lock: safe for the threaded server.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache ("
            " k TEXT PRIMARY KEY, v TEXT NOT NULL, expires REAL)")
        self._conn.commit()

    def get(self, key, now=None):
        now = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "SELECT v, expires FROM cache WHERE k=?", (key,))
            row = cur.fetchone()
            if not row:
                return None
            v, expires = row
            if expires is not None and expires < now:
                self._conn.execute("DELETE FROM cache WHERE k=?", (key,))
                self._conn.commit()
                return None
            try:
                return json.loads(v)
            except ValueError:
                return None

    def set(self, key, value, ttl_seconds=None, now=None):
        now = time.time() if now is None else now
        expires = (now + ttl_seconds) if ttl_seconds else None
        payload = json.dumps(value, separators=(",", ":"))
        with self._lock:
            self._conn.execute(
                "INSERT INTO cache(k, v, expires) VALUES(?,?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v, expires=excluded.expires",
                (key, payload, expires))
            self._conn.commit()

    def stats(self):
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM cache")
            return {"entries": cur.fetchone()[0]}


class LogStore:
    """Append-only JSONL sales log. Thread-safe. One line per sale attempt."""

    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()

    def record(self, event):
        event = dict(event)
        event.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        line = json.dumps(event, separators=(",", ":"))
        with self._lock:
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass   # logging must never break a sale
        return event
