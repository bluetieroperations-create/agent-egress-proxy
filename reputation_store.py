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
import time

# Reuse the contract-correct extractor (token.address_hash, USDC-by-contract).
from settlement_watch import BlockscoutChain, extract_usdc_transfers  # noqa: F401
from addresses import is_evm_address

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

    def iter_settlement_edges(self):
        """Yield (payer, counterparty) for every settlement with a known payer --
        the bipartite payer->payee edges the cross-counterparty graph is built on.
        Read-only; blank/NULL payers are excluded."""
        with self._lock:
            for payer, cp in self._conn.execute(
                    "SELECT payer, counterparty FROM settlements "
                    "WHERE payer IS NOT NULL AND payer != ''"):
                yield payer, cp

    def iter_settlement_events(self, counterparty):
        """Yield (ts, payer) for a payee's settlements -- the timeline the temporal
        / velocity signal (settlement_velocity.py) is built on. Read-only."""
        cp = (counterparty or "").lower()
        with self._lock:
            for ts, payer in self._conn.execute(
                    "SELECT ts, payer FROM settlements WHERE counterparty=?", (cp,)):
                yield ts, payer

    # ---- hot-path read (sub-ms) ----
    def lookup(self, counterparty):
        cp = (counterparty or "").lower()
        with self._lock:
            # NULLIF(lower(payer),'') so a transfer with a missing/blank sender does
            # NOT add a phantom distinct payer -- that count is the Sybil signal, and
            # a blank sender must never help an address clear the thin-history gate.
            # lower() keeps this case-insensitive in step with the payer graph, so a
            # future mixed-case ingest can't split one payer into several (audit F7).
            cur = self._conn.execute(
                "SELECT COUNT(*), MIN(ts), MAX(ts), "
                "COUNT(DISTINCT NULLIF(lower(payer),'')) "
                "FROM settlements WHERE counterparty=?", (cp,))
            count, first_seen, last_seen, distinct_payers = cur.fetchone()
            rows = list(self._conn.execute(
                "SELECT amount, payer FROM settlements WHERE counterparty=? "
                "ORDER BY ts DESC LIMIT ?", (cp, PRICE_HISTORY_LIMIT)))
            amounts = [r[0] for r in rows]
            # Payer-attributed observations -> wash-trade-resistant median
            # (blackwall.robust_price_median). Only payer-bound rows qualify.
            observations = [{"payer": r[1], "amount": r[0]} for r in rows if r[1]]
        return {
            "settlement_count": count,
            # distinct on-chain senders (Sybil signal). A wash-trader can inflate
            # COUNT cheaply but not the number of distinct funded payers.
            "distinct_payers": distinct_payers or 0,
            "dispute_rate": None,   # not on-chain; merged from the ledger
            "price_history": amounts,
            "price_observations": observations,
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
        # Confirmed = max trustless count across sources. A source that omits the
        # field vouches for all of its settlement_count (on-chain store / seed);
        # the ledger reports only its chain-confirmed subset.
        "confirmed_settlement_count": max(
            (r.get("confirmed_settlement_count", r.get("settlement_count") or 0) or 0)
            for r in records),
        # Sybil signal: max distinct payers seen by any source (a lower bound on
        # the true union, fine for the gate).
        "distinct_payers": max((r.get("distinct_payers") or 0) for r in records),
        "dispute_rate": next((r.get("dispute_rate") for r in records
                              if r.get("dispute_rate") is not None), None),
        "price_history": max((r.get("price_history") or [] for r in records),
                             key=len),
        # UNION the payer-attributed samples across sources. Unlike price_history
        # (flat median, double-count-sensitive -> longest), robust_price_median
        # collapses each distinct payer to ONE representative, so concatenating is
        # safe against duplicates AND preserves payer diversity. Picking the
        # "longest" instead silently drops a shorter-but-more-diverse sample,
        # which defeats the wash-trade defense (a long single-payer sample would
        # mask a diverse one and downgrade STOP -> HOLD).
        "price_observations": [o for r in records
                               for o in (r.get("price_observations") or [])],
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


class SelfPopulatingReputationSource:
    """
    A reputation store that bootstraps itself: on a lookup for a counterparty it
    hasn't ingested (or hasn't refreshed within `refresh_after_seconds`), it
    pulls that counterparty's history from chain ONCE, then serves from the fast
    store. First sight of a counterparty is slow (~one indexer call); every
    subsequent check is the sub-ms store read.

    Lets the server run self-serve with no separate pre-warm cron. For a hot
    path that must never block, pre-warm the store via the CLI and use the bare
    ReputationStore instead.
    """

    def __init__(self, store, chain=None, refresh_after_seconds=86400,
                 clock=None):
        self.store = store
        self.chain = chain or BlockscoutChain()
        self.refresh_after = refresh_after_seconds
        self._last_ingest = {}   # counterparty -> monotonic-ish timestamp
        self._clock = clock or time.time

    def _stale(self, cp):
        last = self._last_ingest.get(cp)
        return last is None or (self._clock() - last) >= self.refresh_after

    def lookup(self, counterparty):
        cp = (counterparty or "").lower()
        # Only fetch on-chain for a well-formed EVM address: a non-address
        # counterparty has no on-chain history anyway, and -- critically -- it
        # would otherwise be interpolated UNENCODED into the outbound indexer URL
        # (path/query injection on a fixed host). Garbage -> skip ingest -> the
        # store returns a thin record -> HOLD-leaning. (The check-then-act on
        # _last_ingest can let two threads ingest the same new counterparty at
        # once; that is benign -- ingest is idempotent and store-locked.)
        if is_evm_address(counterparty) and self._stale(cp):
            try:
                self.store.ingest_from_chain(counterparty, self.chain)
            except Exception:
                # Ingest failure must not break the verdict -- serve whatever the
                # store has (possibly an unknown/thin record -> HOLD-leaning).
                pass
            self._last_ingest[cp] = self._clock()
        return self.store.lookup(counterparty)


def production_source(store_path, ledger=None, ingest=False, sanctioned=None,
                      known_bad=None):
    """
    Build the production reputation source from a SQLite store (+ optional
    ledger for disputes). With `ingest=True` the store self-populates from chain
    on first sight of a counterparty.
    """
    store = ReputationStore(store_path, sanctioned=sanctioned, known_bad=known_bad)
    store_source = SelfPopulatingReputationSource(store) if ingest else store
    if ledger is not None:
        from ledger import LedgerReputationSource
        return CombinedReputationSource(
            [store_source, LedgerReputationSource(ledger)])
    return store_source


# ---------------------------------------------------------------------------
# CLI: pre-warm a store by ingesting counterparties from chain.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        sys.stderr.write(
            "usage: python reputation_store.py STORE.db ADDR [ADDR ...]\n"
            "  ingests each counterparty's inbound USDC history into STORE.db\n")
        raise SystemExit(2)
    path, addrs = sys.argv[1], sys.argv[2:]
    store = ReputationStore(path)
    chain = BlockscoutChain()
    total = 0
    for a in addrs:
        n = store.ingest_from_chain(a, chain)
        rec = store.lookup(a)
        total += n
        sys.stdout.write("%s  +%d new  (settlements=%d)\n"
                         % (a, n, rec["settlement_count"]))
    sys.stdout.write("done: %d new settlements into %s\n" % (total, path))
    store.close()
