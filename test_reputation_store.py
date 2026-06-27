"""
Tests for the SQLite-backed production reputation store + record merging.

Run: python -m unittest test_reputation_store.py -v

Offline: ingest normalized transfers, read them back, confirm idempotency and
the merge of on-chain breadth (store) with observed disputes (ledger).
"""
import os
import tempfile
import unittest
from decimal import Decimal

import blackwall as bw
import reputation_store as RS


def xf(to, amount, frm="0xpayer", tx="0xtx", ts="2026-06-01T00:00:00Z"):
    return {"to": to.lower(), "from": frm.lower(), "amount": Decimal(amount),
            "tx_hash": tx, "timestamp": ts}


class TestIngestAndLookup(unittest.TestCase):
    def setUp(self):
        self.store = RS.ReputationStore(":memory:")

    def test_ingest_then_lookup(self):
        self.store.ingest_transfers([
            xf("0xCP", "0.09", tx="0xt1", ts="2026-06-01T00:00:00Z"),
            xf("0xCP", "0.10", tx="0xt2", ts="2026-06-02T00:00:00Z"),
        ])
        rec = self.store.lookup("0xCP")
        self.assertEqual(rec["settlement_count"], 2)
        self.assertEqual(sorted(rec["price_history"]), ["0.09", "0.10"])
        self.assertEqual(rec["first_seen"], "2026-06-01T00:00:00Z")
        self.assertTrue(rec["_meta"]["known"])

    def test_idempotent_reingest(self):
        rows = [xf("0xCP", "0.09", tx="0xt1")]
        self.assertEqual(self.store.ingest_transfers(rows), 1)
        self.assertEqual(self.store.ingest_transfers(rows), 0)  # no new rows
        self.assertEqual(self.store.lookup("0xCP")["settlement_count"], 1)

    def test_transfer_without_tx_hash_skipped(self):
        # No tx_hash -> not dedupable, not useful -> skipped (would double-count).
        self.assertEqual(self.store.ingest_transfers([
            {"to": "0xcp", "from": "0xp", "amount": Decimal("0.09"),
             "tx_hash": None, "timestamp": "2026-06-01T00:00:00Z"}]), 0)

    def test_unknown_counterparty(self):
        rec = self.store.lookup("0xNOBODY")
        self.assertEqual(rec["settlement_count"], 0)
        self.assertFalse(rec["_meta"]["known"])
        self.assertIsNone(rec["dispute_rate"])

    def test_case_insensitive(self):
        self.store.ingest_transfers([xf("0xABCDEF", "0.09")])
        self.assertEqual(self.store.lookup("0xabcdef")["settlement_count"], 1)

    def test_sanctioned_flagged(self):
        store = RS.ReputationStore(":memory:", sanctioned={"0xbad"})
        self.assertTrue(store.lookup("0xBAD")["sanctioned"])

    def test_persists_to_disk(self):
        path = os.path.join(tempfile.mkdtemp(), "rep.db")
        s1 = RS.ReputationStore(path)
        s1.ingest_transfers([xf("0xCP", "0.09")])
        s1.close()
        s2 = RS.ReputationStore(path)  # reopen
        self.assertEqual(s2.lookup("0xCP")["settlement_count"], 1)
        s2.close()


class TestMerge(unittest.TestCase):
    """merge_records: on-chain breadth (store) + observed disputes (ledger)."""

    def test_dispute_rate_from_ledger_settlements_from_store(self):
        store_rec = {"settlement_count": 1000, "dispute_rate": None,
                     "price_history": ["0.09"] * 5,
                     "_meta": {"source": "reputation-store", "known": True}}
        ledger_rec = {"settlement_count": 40, "dispute_rate": 0.05,
                      "price_history": ["0.09"],
                      "_meta": {"source": "blackwall-ledger", "known": True}}
        m = RS.merge_records([store_rec, ledger_rec])
        self.assertEqual(m["settlement_count"], 1000)   # max breadth
        self.assertEqual(m["dispute_rate"], 0.05)        # observed, from ledger
        self.assertEqual(len(m["price_history"]), 5)     # longest sample

    def test_sanctioned_is_or(self):
        m = RS.merge_records([{"sanctioned": False}, {"sanctioned": True}])
        self.assertTrue(m["sanctioned"])

    def test_empty(self):
        self.assertIsNone(RS.merge_records([None, None]))


class TestCombinedSourceDrivesVerdict(unittest.TestCase):
    """The combined source must slot into forecast() and graduate a counterparty."""

    class _LedgerLike:
        def __init__(self, rec):
            self.rec = rec
        def lookup(self, cp):
            return dict(self.rec)

    def test_store_breadth_plus_ledger_clean_goes(self):
        store = RS.ReputationStore(":memory:")
        for i in range(30):
            store.ingest_transfers([xf("0xCP", "0.09", tx="0xt%d" % i)])
        ledger = self._LedgerLike({"settlement_count": 30, "dispute_rate": 0.0,
                                   "price_history": [], "_meta": {"known": True}})
        src = RS.CombinedReputationSource([store, ledger])
        resp, err = bw.forecast(
            {"counterparty": "0xCP", "amount": "0.09", "asset": "USDC", "chain": "base"},
            src)
        self.assertIsNone(err)
        self.assertEqual(resp["verdict"], "GO")


if __name__ == "__main__":
    unittest.main()
