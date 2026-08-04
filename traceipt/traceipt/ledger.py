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
from .merkle import inclusion_proof, merkle_root
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
    anchor_id   INTEGER,
    anchor_pos  INTEGER,
    kind        TEXT NOT NULL DEFAULT 'payment',
    ref_receipt_id TEXT,
    UNIQUE (seller_id, sequence),
    UNIQUE (seller_id, tx_hash)
);
CREATE INDEX IF NOT EXISTS idx_receipts_seller ON receipts (seller_id, sequence);
CREATE INDEX IF NOT EXISTS idx_receipts_anchor ON receipts (anchor_id, anchor_pos);
CREATE INDEX IF NOT EXISTS idx_receipts_ref ON receipts (ref_receipt_id);

CREATE TABLE IF NOT EXISTS anchors (
    anchor_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT NOT NULL,
    leaf_count       INTEGER NOT NULL,
    root             TEXT NOT NULL,
    onchain_network  TEXT,
    onchain_tx       TEXT
);

-- Anchoring-as-a-service: externally submitted digests (e.g. AAR/acta
-- receipt-chain heads) folded into the same Merkle batches as receipts.
CREATE TABLE IF NOT EXISTS attestations (
    attestation_id TEXT PRIMARY KEY,
    hash           TEXT NOT NULL,
    type           TEXT NOT NULL,
    ref            TEXT,
    submitted_at   TEXT NOT NULL,
    payer          TEXT,
    anchor_id      INTEGER,
    anchor_pos     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_att_anchor ON attestations (anchor_id, anchor_pos);
CREATE INDEX IF NOT EXISTS idx_att_hash ON attestations (hash);
"""


class Ledger:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        con = self._connect()
        try:
            con.executescript(_SCHEMA)
            # migrate pre-v0.2 databases (columns added for credit notes)
            for ddl in (
                "ALTER TABLE receipts ADD COLUMN kind TEXT NOT NULL DEFAULT 'payment'",
                "ALTER TABLE receipts ADD COLUMN ref_receipt_id TEXT",
                # 'sealed' = anchored here; 'recovered' = re-derived from the
                # chain (root + tx known, original leaves not) -- see recover.py.
                "ALTER TABLE anchors ADD COLUMN source TEXT NOT NULL DEFAULT 'sealed'",
            ):
                try:
                    con.execute(ddl)
                except sqlite3.OperationalError:
                    pass  # column already exists
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
                    "receipt_hash, prev_hash, issued_at, envelope, kind, ref_receipt_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        payload["receipt_id"],
                        payload["seller_id"],
                        payload["sequence"],
                        payload["settlement"]["tx_hash"].lower(),
                        receipt_hash(payload),
                        payload["prev_receipt_hash"],
                        payload["issued_at"],
                        json.dumps(envelope, ensure_ascii=False),
                        payload.get("kind", "payment"),
                        (payload.get("credit") or {}).get("credits_receipt_id"),
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

    def credited_total(self, receipt_id: str) -> int:
        """Sum of base units already credited (refunded) against a receipt,
        across all credit notes that reference it."""
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT envelope FROM receipts WHERE kind='credit' "
                "AND ref_receipt_id = ?", (receipt_id,)
            ).fetchall()
        finally:
            con.close()
        total = 0
        for row in rows:
            payload = json.loads(row["envelope"])["payload"]
            total += int(payload["settlement"]["amount_base_units"])
        return total

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

    def chain_manifest(self, seller_id: str) -> list[dict]:
        """Ordered, compact manifest of a seller's WHOLE chain: one row per
        receipt (sequence, id, hash, prev-link, kind, anchored). Enough for a
        third party to check density + linkage without downloading every
        envelope. Backs the completeness proof (see completeness.py)."""
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT sequence, receipt_id, receipt_hash, prev_hash, kind, "
                "anchor_id FROM receipts WHERE seller_id = ? ORDER BY sequence ASC",
                (seller_id,),
            ).fetchall()
        finally:
            con.close()
        return [{
            "sequence": r["sequence"],
            "receipt_id": r["receipt_id"],
            "receipt_hash": r["receipt_hash"],
            "prev_receipt_hash": r["prev_hash"],
            "kind": r["kind"],
            "anchored": r["anchor_id"] is not None,
        } for r in rows]

    # -- Merkle anchoring --------------------------------------------------
    def create_anchor(self, created_at: str, publisher=None) -> dict | None:
        """Seal all not-yet-anchored receipts AND externally submitted
        attestations into one Merkle batch (receipts first, then
        attestations, one contiguous position sequence).

        `publisher`, if given, is called with the hex root and returns
        (network, tx_hash) for the on-chain publication, or None. Returns the
        anchor summary, or None if there is nothing to anchor.
        """
        with self._lock:
            con = self._connect()
            try:
                r_rows = con.execute(
                    "SELECT receipt_id, receipt_hash FROM receipts "
                    "WHERE anchor_id IS NULL ORDER BY rowid ASC"
                ).fetchall()
                a_rows = con.execute(
                    "SELECT attestation_id, hash FROM attestations "
                    "WHERE anchor_id IS NULL ORDER BY rowid ASC"
                ).fetchall()
                if not r_rows and not a_rows:
                    return None
                leaves = ([r["receipt_hash"].encode("utf-8") for r in r_rows] +
                          [a["hash"].encode("utf-8") for a in a_rows])
                root_hex = merkle_root(leaves).hex()
                onchain = publisher(root_hex) if publisher else None
                network, tx = onchain if onchain else (None, None)
                cur = con.execute(
                    "INSERT INTO anchors (created_at, leaf_count, root, "
                    "onchain_network, onchain_tx) VALUES (?,?,?,?,?)",
                    (created_at, len(leaves), root_hex, network, tx),
                )
                anchor_id = cur.lastrowid
                pos = 0
                for r in r_rows:
                    con.execute(
                        "UPDATE receipts SET anchor_id = ?, anchor_pos = ? "
                        "WHERE receipt_id = ?", (anchor_id, pos, r["receipt_id"]))
                    pos += 1
                for a in a_rows:
                    con.execute(
                        "UPDATE attestations SET anchor_id = ?, anchor_pos = ? "
                        "WHERE attestation_id = ?",
                        (anchor_id, pos, a["attestation_id"]))
                    pos += 1
                con.commit()
            finally:
                con.close()
            return {"anchor_id": anchor_id, "leaf_count": len(leaves),
                    "receipts": len(r_rows), "attestations": len(a_rows),
                    "root": root_hex, "onchain_network": network,
                    "onchain_tx": tx, "created_at": created_at}

    def _anchor_leaves(self, con, anchor_id: int) -> list[bytes]:
        """Full ordered leaf list of an anchor, merged across receipts and
        attestations by their global anchor_pos."""
        rows = con.execute(
            "SELECT anchor_pos, receipt_hash AS leaf FROM receipts "
            "WHERE anchor_id = ? "
            "UNION ALL "
            "SELECT anchor_pos, hash AS leaf FROM attestations "
            "WHERE anchor_id = ? "
            "ORDER BY anchor_pos ASC", (anchor_id, anchor_id)
        ).fetchall()
        return [r["leaf"].encode("utf-8") for r in rows]

    def list_anchors(self) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT anchor_id, created_at, leaf_count, root, "
                "onchain_network, onchain_tx FROM anchors ORDER BY anchor_id ASC"
            ).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]

    def anchor_by_root(self, root_hex: str) -> dict | None:
        """The anchor row with this root (sealed OR recovered), or None. Lets a
        verifier confirm a proof's recomputed root is one we published on-chain,
        without trusting the caller about which tx carries it."""
        con = self._connect()
        try:
            row = con.execute(
                "SELECT anchor_id, created_at, leaf_count, root, onchain_network, "
                "onchain_tx, source FROM anchors WHERE root = ?", (root_hex.lower(),)
            ).fetchone()
        finally:
            con.close()
        return dict(row) if row else None

    def record_recovered_anchor(self, *, root: str, onchain_tx: str | None,
                                onchain_network: str | None,
                                created_at: str) -> bool:
        """Insert an anchor re-derived from the chain (root + tx known, leaves
        not). Idempotent: a root already present (sealed or recovered) is left
        untouched. Returns True if a new row was inserted. leaf_count is 0
        because the batch's leaves are not on-chain -- only the root is."""
        with self._lock:
            con = self._connect()
            try:
                if con.execute("SELECT 1 FROM anchors WHERE root = ?",
                               (root.lower(),)).fetchone():
                    return False
                con.execute(
                    "INSERT INTO anchors (created_at, leaf_count, root, "
                    "onchain_network, onchain_tx, source) VALUES (?,?,?,?,?, 'recovered')",
                    (created_at, 0, root.lower(), onchain_network, onchain_tx))
                con.commit()
                return True
            finally:
                con.close()

    def inclusion_for(self, receipt_id: str) -> dict | None:
        """Merkle inclusion proof for a receipt, or None if it is not yet
        anchored / unknown. The proof lets anyone recompute the anchor root
        offline (see receipts.merkle.verify_inclusion)."""
        con = self._connect()
        try:
            row = con.execute(
                "SELECT anchor_id, anchor_pos, receipt_hash FROM receipts "
                "WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
            if row is None or row["anchor_id"] is None:
                return None
            anchor = con.execute(
                "SELECT created_at, leaf_count, root, onchain_network, onchain_tx "
                "FROM anchors WHERE anchor_id = ?", (row["anchor_id"],)
            ).fetchone()
            leaves = self._anchor_leaves(con, row["anchor_id"])
        finally:
            con.close()
        m = row["anchor_pos"]
        proof = inclusion_proof(leaves, m)
        return {
            "receipt_id": receipt_id,
            "anchor_id": row["anchor_id"],
            "leaf_index": m,
            "tree_size": len(leaves),
            "leaf_data": row["receipt_hash"],
            "audit_path": [h.hex() for h in proof],
            "root": anchor["root"],
            "onchain_network": anchor["onchain_network"],
            "onchain_tx": anchor["onchain_tx"],
            "anchored_at": anchor["created_at"],
        }

    # -- anchoring-as-a-service (external attestations) --------------------
    def submit_attestation(self, *, digest: str, type_: str, ref: str | None,
                           submitted_at: str, payer: str | None = None):
        """Queue an external digest for the next Merkle batch.

        Returns (record, idempotent). Re-submitting a digest that is still
        waiting for an anchor returns the existing record (no double
        anchoring); once anchored, the same digest may be submitted again to
        attest continued existence at a later time."""
        import secrets as _secrets
        with self._lock:
            con = self._connect()
            try:
                row = con.execute(
                    "SELECT * FROM attestations WHERE hash = ? "
                    "AND anchor_id IS NULL", (digest,)
                ).fetchone()
                if row is not None:
                    return dict(row), True
                att_id = "att_" + _secrets.token_hex(10)
                con.execute(
                    "INSERT INTO attestations (attestation_id, hash, type, ref, "
                    "submitted_at, payer) VALUES (?,?,?,?,?,?)",
                    (att_id, digest, type_, ref, submitted_at, payer),
                )
                con.commit()
                row = con.execute(
                    "SELECT * FROM attestations WHERE attestation_id = ?",
                    (att_id,)).fetchone()
                return dict(row), False
            finally:
                con.close()

    def get_attestation(self, att_id: str) -> dict | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT * FROM attestations WHERE attestation_id = ?",
                (att_id,)).fetchone()
        finally:
            con.close()
        return dict(row) if row else None

    def find_pending_attestation(self, digest: str) -> dict | None:
        """The still-unanchored attestation for `digest`, or None. A read-only
        idempotency pre-check so the caller can detect a duplicate submission
        (and skip charging for it) BEFORE settling — mirrors the (hash, anchor
        IS NULL) match submit_attestation uses to dedupe."""
        con = self._connect()
        try:
            row = con.execute(
                "SELECT * FROM attestations WHERE hash = ? AND anchor_id IS NULL",
                (digest,)).fetchone()
        finally:
            con.close()
        return dict(row) if row else None

    def attestation_inclusion(self, att_id: str) -> dict | None:
        """Merkle inclusion proof for an external attestation, or None if
        unknown / not yet anchored."""
        con = self._connect()
        try:
            row = con.execute(
                "SELECT anchor_id, anchor_pos, hash FROM attestations "
                "WHERE attestation_id = ?", (att_id,)
            ).fetchone()
            if row is None or row["anchor_id"] is None:
                return None
            anchor = con.execute(
                "SELECT created_at, leaf_count, root, onchain_network, onchain_tx "
                "FROM anchors WHERE anchor_id = ?", (row["anchor_id"],)
            ).fetchone()
            leaves = self._anchor_leaves(con, row["anchor_id"])
        finally:
            con.close()
        m = row["anchor_pos"]
        proof = inclusion_proof(leaves, m)
        return {
            "attestation_id": att_id,
            "anchor_id": row["anchor_id"],
            "leaf_index": m,
            "tree_size": len(leaves),
            "leaf_data": row["hash"],
            "audit_path": [h.hex() for h in proof],
            "root": anchor["root"],
            "onchain_network": anchor["onchain_network"],
            "onchain_tx": anchor["onchain_tx"],
            "anchored_at": anchor["created_at"],
        }
