"""
SQLite ledger: per-seller, append-only, hash-chained receipt store.

Invariants enforced here:
  * sequence per seller is dense (1, 2, 3, ...) — the regulatory
    "sequential numbering" property.
  * each receipt's prev_receipt_hash equals the stored hash of the previous
    receipt (genesis hash for sequence 1).
  * verify_chain() re-derives every hash from the stored envelopes, so a
    mutated row is detected, not trusted.

Writes are serialized by a process-level lock; SQLite provides durability.
"""
from __future__ import annotations

import json
import sqlite3
import threading

from .canonical import GENESIS_HASH
from .schema import receipt_hash

_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id  TEXT PRIMARY KEY,
    seller_id   TEXT NOT NULL,
    sequence    INTEGER NOT NULL,
    tx_hash     TEXT NOT NULL,
    receipt_hash TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    issued_at   TEXT NOT NULL,
    envelope    TEXT NOT NULL,
    UNIQUE (seller_id, sequence),
    UNIQUE (seller_id, tx_hash)
);
CREATE INDEX IF NOT EXISTS idx_receipts_seller ON receipts (seller_id, sequence);
"""


class Ledger:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        con = self._connect()
        try:
            con.executescript(_SCHEMA)
            con.commit()
        finally:
            con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._path)
        con.row_factory = sqlite3.Row
        return con

    def head(self, seller_id: str) -> tuple[int, str]:
        """(last_sequence, last_hash) for a seller; (0, GENESIS_HASH) if none."""
        con = self._connect()
        try:
            row = con.execute(
                "SELECT sequence, receipt_hash FROM receipts "
                "WHERE seller_id = ? ORDER BY sequence DESC LIMIT 1",
                (seller_id,),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return 0, GENESIS_HASH
        return row["sequence"], row["receipt_hash"]

    def next_link(self, seller_id: str) -> tuple[int, str]:
        """(sequence, prev_hash) to use for this seller's next receipt."""
        seq, h = self.head(seller_id)
        return seq + 1, h

    def append(self, envelope: dict) -> None:
        """Store a signed envelope. Rejects chain breaks and duplicates."""
        payload = envelope["payload"]
        with self._lock:
            seq, head_hash = self.head(payload["seller_id"])
            if payload["sequence"] != seq + 1:
                raise ValueError(
                    f"chain break: expected sequence {seq + 1}, got {payload['sequence']}"
                )
            if payload["prev_receipt_hash"] != head_hash:
                raise ValueError("chain break: prev_receipt_hash does not match head")
            con = self._connect()
            try:
                con.execute(
                    "INSERT INTO receipts (receipt_id, seller_id, sequence, tx_hash, "
                    "receipt_hash, prev_hash, issued_at, envelope) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        payload["receipt_id"],
                        payload["seller_id"],
                        payload["sequence"],
                        payload["settlement"]["tx_hash"].lower(),
                        receipt_hash(payload),
                        payload["prev_receipt_hash"],
                        payload["issued_at"],
                        json.dumps(envelope, ensure_ascii=False),
                    ),
                )
                con.commit()
            finally:
                con.close()

    def find_by_settlement(self, seller_id: str, tx_hash: str) -> dict | None:
        """The receipt already issued for this seller+settlement, if any.
        One (seller_id, tx_hash), one receipt — issuance is idempotent per
        seller. NOTE: this alone does not stop the SAME tx being claimed
        under different seller_id strings; the service prevents that by
        pinning seller_id and binding payee==pay_to (seller-hosted mode).
        See the service layer and the README threat model."""
        con = self._connect()
        try:
            row = con.execute(
                "SELECT envelope FROM receipts WHERE seller_id = ? AND tx_hash = ?",
                (seller_id, tx_hash.lower()),
            ).fetchone()
        finally:
            con.close()
        return json.loads(row["envelope"]) if row else None

    def get(self, receipt_id: str) -> dict | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT envelope FROM receipts WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
        finally:
            con.close()
        return json.loads(row["envelope"]) if row else None

    def verify_chain(self, seller_id: str, verify_envelope_fn=None) -> list[str]:
        """Re-derive the whole chain for a seller from stored envelopes.

        Returns a list of problems (empty = chain intact). Detects: gaps in
        sequence, broken prev links, stored-hash rows that don't match the
        recomputed payload hash, and — when verify_envelope_fn is supplied —
        any envelope whose Ed25519 SIGNATURE does not verify.

        The signature check is the only tamper detection that survives an
        attacker with database write access: the hash chain uses a keyless
        SHA-256, so a rehash-consistent rewrite passes the hash checks alone.
        Callers that care about tamper-evidence MUST pass verify_envelope_fn.
        """
        problems: list[str] = []
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT sequence, receipt_hash, prev_hash, envelope FROM receipts "
                "WHERE seller_id = ? ORDER BY sequence ASC",
                (seller_id,),
            ).fetchall()
        finally:
            con.close()
        expected_prev = GENESIS_HASH
        expected_seq = 1
        for row in rows:
            envelope = json.loads(row["envelope"])
            payload = envelope["payload"]
            if verify_envelope_fn is not None:
                try:
                    verify_envelope_fn(envelope)
                except Exception as e:
                    problems.append(f"seq {row['sequence']}: signature INVALID ({e})")
            if row["sequence"] != expected_seq:
                problems.append(f"gap: expected seq {expected_seq}, found {row['sequence']}")
                expected_seq = row["sequence"]
            recomputed = receipt_hash(payload)
            if recomputed != row["receipt_hash"]:
                problems.append(f"seq {row['sequence']}: stored hash != recomputed hash (row tampered)")
            if payload["prev_receipt_hash"] != expected_prev:
                problems.append(f"seq {row['sequence']}: prev link broken")
            expected_prev = recomputed
            expected_seq += 1
        return problems
