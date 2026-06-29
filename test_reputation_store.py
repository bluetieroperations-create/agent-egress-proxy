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

    def test_price_observations_payer_attributed(self):
        # store carries (payer, amount) so the verdict engine can build a
        # wash-trade-resistant median across distinct funders.
        self.store.ingest_transfers([
            xf("0xCP", "1.00", frm="0xp1", tx="0xt1"),
            xf("0xCP", "1.00", frm="0xp2", tx="0xt2"),
            xf("0xCP", "100.0", frm="0xwash", tx="0xt3"),
            xf("0xCP", "100.0", frm="0xwash", tx="0xt4"),
        ])
        rec = self.store.lookup("0xCP")
        obs = rec["price_observations"]
        self.assertEqual(len(obs), 4)
        self.assertEqual({o["payer"] for o in obs}, {"0xp1", "0xp2", "0xwash"})
        med, n = bw.robust_price_median(obs, min_payers=3)
        self.assertEqual(n, 3)  # 3 distinct payers, not 4 rows
        # wash payer collapses to one rep -> median across {1,1,100} = 1
        self.assertEqual(med, Decimal("1.00"))

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
        for i in range(30):  # 30 settlements from 5 distinct payers (real)
            store.ingest_transfers([xf("0xCP", "0.09", frm="0x%040x" % (i % 5),
                                       tx="0xt%d" % i)])
        ledger = self._LedgerLike({"settlement_count": 30, "dispute_rate": 0.0,
                                   "price_history": [], "_meta": {"known": True}})
        src = RS.CombinedReputationSource([store, ledger])
        resp, err = bw.forecast(
            {"counterparty": "0xCP", "amount": "0.09", "asset": "USDC", "chain": "base"},
            src)
        self.assertIsNone(err)
        self.assertEqual(resp["verdict"], "GO")


class _FakeChain:
    """Records ingest calls; returns canned transfers per counterparty."""
    def __init__(self, by_cp):
        self.by_cp = by_cp
        self.calls = []
    def recent_inbound(self, cp):
        self.calls.append(cp)
        return self.by_cp.get(cp.lower(), [])


class TestSelfPopulating(unittest.TestCase):
    CP = "0x" + "c" * 40  # a well-formed EVM address (ingest only fires for these)

    def test_lazy_ingest_then_cached(self):
        store = RS.ReputationStore(":memory:")
        chain = _FakeChain({self.CP.lower(): [xf(self.CP, "0.09", tx="0xt1"),
                                              xf(self.CP, "0.10", tx="0xt2")]})
        src = RS.SelfPopulatingReputationSource(store, chain=chain,
                                                refresh_after_seconds=1000)
        # first lookup ingests from chain...
        rec = src.lookup(self.CP)
        self.assertEqual(rec["settlement_count"], 2)
        self.assertEqual(chain.calls, [self.CP])
        # ...second lookup is served from the store, no re-ingest.
        rec2 = src.lookup(self.CP)
        self.assertEqual(rec2["settlement_count"], 2)
        self.assertEqual(chain.calls, [self.CP])  # still just one ingest

    def test_refresh_after_window(self):
        store = RS.ReputationStore(":memory:")
        chain = _FakeChain({self.CP.lower(): [xf(self.CP, "0.09")]})
        t = [1000.0]
        src = RS.SelfPopulatingReputationSource(
            store, chain=chain, refresh_after_seconds=60, clock=lambda: t[0])
        src.lookup(self.CP)
        t[0] = 1100.0  # past the refresh window
        src.lookup(self.CP)
        self.assertEqual(len(chain.calls), 2)  # re-ingested

    def test_non_address_counterparty_skips_ingest(self):
        # Regression: a non-EVM-address counterparty must NOT be sent to the
        # indexer (it would be interpolated unencoded into the outbound URL).
        store = RS.ReputationStore(":memory:")
        chain = _FakeChain({})
        src = RS.SelfPopulatingReputationSource(store, chain=chain)
        for bad in ["addr?evil=1", "../../stats", "0xshort", "notanaddr"]:
            src.lookup(bad)
        self.assertEqual(chain.calls, [])  # nothing reached the chain client
        # a valid address DOES trigger ingest
        src2 = RS.SelfPopulatingReputationSource(
            store, chain=_FakeChain({("0x" + "a" * 40): [xf("0x" + "a" * 40, "0.09")]}))
        src2.lookup("0x" + "A" * 40)
        self.assertEqual(src2.chain.calls, ["0x" + "A" * 40])

    def test_ingest_failure_does_not_break_lookup(self):
        class Boom:
            def recent_inbound(self, cp):
                raise RuntimeError("indexer down")
        src = RS.SelfPopulatingReputationSource(RS.ReputationStore(":memory:"),
                                                chain=Boom())
        rec = src.lookup("0xCP")  # must not raise
        self.assertEqual(rec["settlement_count"], 0)  # thin -> HOLD-leaning


class TestProductionSource(unittest.TestCase):
    def test_store_plus_ledger_combined(self):
        import tempfile, os, ledger as L
        store_path = os.path.join(tempfile.mkdtemp(), "s.db")
        led = L.EventLedger(os.path.join(tempfile.mkdtemp(), "l.jsonl"))
        src = RS.production_source(store_path, ledger=led, ingest=False)
        self.assertIsInstance(src, RS.CombinedReputationSource)
        # lookup returns a merged record shape
        rec = src.lookup("0xNOBODY")
        self.assertIn("_meta", rec)
        self.assertEqual(rec["_meta"]["source"], "combined")

    def test_store_only_when_no_ledger(self):
        import tempfile, os
        src = RS.production_source(os.path.join(tempfile.mkdtemp(), "s.db"))
        self.assertIsInstance(src, RS.ReputationStore)


if __name__ == "__main__":
    unittest.main()
