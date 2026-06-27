#!/usr/bin/env python3
"""
reputation_store.py -- Blackwall's own indexed reputation store (production source).

The data-source spike (docs/DATA_SOURCE_SPIKE.md) concluded: a free public
indexer is too slow/variable for a synchronous per-call check, so Blackwall needs
its OWN indexed store -- fed by a background ingest (slow is fine there) and read
sub-millisecond on the hot path. This is that store, backed by stdlib `sqlite3`.

  background:  chain  --ingest_from_chain-->  SQLite settlements
  hot path:    lookup(counterparty)  --SQLite SELECT (sub-ms)-->  record

It is a drop-in `lookup()` reputation source. On-chain gives the SETTLEMENT
backbone (count, volume, price, age); it cannot see disputes -- so dispute_rate
stays None here and is merged in from the ledger via CombinedReputationSource.

Stdlib only. Idempotent ingest (INSERT OR IGNORE on a settlement's natural key),
thread-safe (one connection guarded by a lock; sqlite serializes writes).
"""
from __future__ import annotations

import sqlite3
import threading

# Reuse the contract-correct extractor (token.address_hash, USDC-by-contract).
from settlement_watch import BlockscoutChain, extract_usdc_transfers  # noqa: F401

PRICE_HISTORY_LIMIT = 200  # cap the per-lookup price sample (bounded work + memory)


class ReputationStore:
    """SQLite-backed settlement index. Drop-in for MockReputationSource."""

    def __init__(self, path=":memory:", sanctioned=None, known_bad=None):
        # check_same_thread=False + a lock: the server is thread-per-connection,
        # and sqlite already serializes writers; the lock keeps our access tidy.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._init_schema()
        self.sanctioned = {s.lower() for s in (sanctioned or ())}
        self.known_bad = {k.lower() for k in (known_bad or ())}

    def _init_schema(self):
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS settlements (
                    counterparty TEXT NOT NULL,
                    payer        TEXT,
                    amount       TEXT NOT NULL,
                    tx_hash      TEXT,
                    ts           TEXT,
                    -- natural key: one tx can carry one settlement to a
                    -- counterparty for a given amount; re-ingest is idempotent.
                    UNIQUE(tx_hash, counterparty, amount)
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cp ON settlements(counterparty)")
            self._conn.commit()

    # ---- ingest (background / off the hot path) ----
    def ingest_transfers(self, transfers):
        """Upsert normalized USDC transfers (from extract_usdc_transfers).
        Returns the number of NEW rows inserted."""
        # Require tx_hash: it is the dedup key (SQLite treats NULLs as distinct,
        # so a null tx_hash would double-count on re-ingest), and a settlement
        # without a tx reference is useless to the watcher anyway.
        rows = [(t["to"], t.get("from"), str(t["amount"]),
                 t["tx_hash"], t.get("timestamp"))
                for t in transfers
                if t.get("to") and t.get("amount") is not None and t.get("tx_hash")]
        if not rows:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO settlements "
                "(counterparty, payer, amount, tx_hash, ts) VALUES (?,?,?,?,?)",
                rows)
            self._conn.commit()
            return self._conn.total_changes - before

    def ingest_from_chain(self, counterparty, chain=None):
        """Fetch a counterparty's recent inbound USDC and ingest it."""
        chain = chain or BlockscoutChain()
        return self.ingest_transfers(chain.recent_inbound(counterparty))

    # ---- hot-path read (sub-ms) ----
    def lookup(self, counterparty):
        cp = (counterparty or "").lower()
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*), MIN(ts), MAX(ts) FROM settlements "
                "WHERE counterparty=?", (cp,))
            count, first_seen, last_seen = cur.fetchone()
            amounts = [r[0] for r in self._conn.execute(
                "SELECT amount FROM settlements WHERE counterparty=? "
                "ORDER BY ts DESC LIMIT ?", (cp, PRICE_HISTORY_LIMIT))]
        return {
            "settlement_count": count,
            "dispute_rate": None,   # not on-chain; merged from the ledger
            "price_history": amounts,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "sanctioned": cp in self.sanctioned,
            "known_bad": cp in self.known_bad,
            "_meta": {"source": "reputation-store", "known": count > 0},
        }

    def close(self):
        with self._lock:
            self._conn.close()


# ===========================================================================
# Combine on-chain breadth (store) with observed disputes (ledger)
# ===========================================================================
def merge_records(records):
    """
    Field-wise merge of reputation records from multiple sources.

    settlement_count -> max (the most complete view of settled volume);
    dispute_rate     -> first OBSERVED (non-None) -- only the ledger has this;
    price_history    -> the longest available sample;
    sanctioned/known_bad -> OR (any source flagging is decisive).
    """
    records = [r for r in records if r]
    if not records:
        return None
    out = {
        "settlement_count": max((r.get("settlement_count") or 0) for r in records),
        "dispute_rate": next((r.get("dispute_rate") for r in records
                              if r.get("dispute_rate") is not None), None),
        "price_history": max((r.get("price_history") or [] for r in records),
                             key=len),
        "sanctioned": any(r.get("sanctioned") for r in records),
        "known_bad": any(r.get("known_bad") for r in records),
        "_meta": {
            "source": "combined",
            "known": any((r.get("_meta") or {}).get("known")
                         or (r.get("settlement_count") or 0) > 0 for r in records),
            "merged_from": [(r.get("_meta") or {}).get("source") for r in records],
        },
    }
    return out


class CombinedReputationSource:
    """
    Merge several reputation sources into one record (NOT first-wins).

    The production composition: the indexed store supplies on-chain settlement
    breadth, the ledger supplies the dispute/underdelivery signal that isn't
    on-chain. Both inform a single verdict.
    """

    def __init__(self, sources):
        if not sources:
            raise ValueError("need at least one source")
        self.sources = list(sources)

    def lookup(self, counterparty):
        return merge_records([s.lookup(counterparty) for s in self.sources])
