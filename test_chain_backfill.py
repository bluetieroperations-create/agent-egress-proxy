"""
Tests for chain_backfill.py -- seed reputation from public Base USDC history.
Fake page transport (Blockscout-shaped items); each test states its mutation.
"""
import tempfile
import unittest

import chain_backfill as B

USDC = B.BASE_USDC
PAYEE = "0x" + "a" * 40


def _item(to, frm, value="90000", dec=6, tx=None, ts="2026-07-01T00:00:00Z",
          token=USDC):
    return {"token": {"address_hash": token},
            "total": {"value": value, "decimals": dec},
            "to": {"hash": to}, "from": {"hash": frm},
            "transaction_hash": tx or ("0x" + "1" * 64), "timestamp": ts}


def _pager(pages_by_addr):
    """pages_by_addr: addr(lower) -> list of (items, next_params). Fake pages by
    call-count (ignores the opaque next_params, like a real cursor would drive it)."""
    counts = {}

    def fetch(address, params):
        a = address.lower()
        i = counts.get(a, 0)
        counts[a] = i + 1
        seq = pages_by_addr.get(a, [])
        return seq[i] if i < len(seq) else ([], None)
    return fetch


class TestCollectPaged(unittest.TestCase):
    """Mutation notes: not following next_params -> only page 1 collected; not
    honoring max_pages -> a runaway pager isn't bounded."""

    def test_walks_all_pages(self):
        fetch = _pager({PAYEE.lower(): [([1, 2], {"c": 1}), ([3], {"c": 2}), ([4], None)]})
        self.assertEqual(B.collect_paged(fetch, PAYEE, max_pages=10), [1, 2, 3, 4])

    def test_stops_at_max_pages(self):
        fetch = _pager({PAYEE.lower(): [([1], {"c": 1}), ([2], {"c": 2}), ([3], {"c": 3})]})
        self.assertEqual(B.collect_paged(fetch, PAYEE, max_pages=2), [1, 2])

    def test_stops_on_no_next(self):
        fetch = _pager({PAYEE.lower(): [([1], None), ([2], None)]})
        self.assertEqual(B.collect_paged(fetch, PAYEE, max_pages=10), [1])


class TestPayeeTransfers(unittest.TestCase):
    """Mutation notes: not filtering inbound -> an outbound row sneaks in; not
    normalizing USDC-by-contract -> a lookalike token pollutes."""

    def test_inbound_usdc_only(self):
        fetch = _pager({PAYEE.lower(): [([
            _item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64),
            _item("0x" + "c" * 40, PAYEE, tx="0x" + "2" * 64),          # OUTBOUND -> drop
            _item(PAYEE, "0x" + "d" * 40, tx="0x" + "3" * 64, token="0x" + "e" * 40),  # non-USDC -> drop
        ], None)]})
        rows = B.payee_transfers(fetch, PAYEE)
        self.assertEqual(len(rows), 1)
        self.assertTrue(B.addresses_equal(rows[0]["to"], PAYEE))
        self.assertEqual(str(rows[0]["amount"]), "0.09")


class TestBackfill(unittest.TestCase):
    """
    Mutation notes:
      - not ingesting -> reputation stays empty (test_seeds_reputation FAILS).
      - not idempotent -> re-run double-counts (test_idempotent FAILS).
      - ingesting an invalid address -> test_skips_invalid FAILS.
    """
    def _store(self):
        from reputation_store import ReputationStore
        d = tempfile.mkdtemp()
        return ReputationStore(d + "/rep.db")

    def test_seeds_reputation(self):
        fetch = _pager({PAYEE.lower(): [([
            _item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64),
            _item(PAYEE, "0x" + "c" * 40, tx="0x" + "2" * 64),         # distinct payer
        ], None)]})
        store = self._store()
        summary = B.backfill(store, [PAYEE], fetch)
        self.assertEqual(summary["ingested"], 2)
        rec = store.lookup(PAYEE)
        self.assertGreaterEqual(rec.get("settlement_count", 0), 2)
        self.assertGreaterEqual(rec.get("distinct_payers", 0), 2)   # the Sybil signal

    def test_idempotent_rerun(self):
        pages = {PAYEE.lower(): [([_item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64)], None)]}
        store = self._store()
        self.assertEqual(B.backfill(store, [PAYEE], _pager(pages))["ingested"], 1)
        self.assertEqual(B.backfill(store, [PAYEE], _pager(pages))["ingested"], 0)

    def test_skips_invalid_address(self):
        store = self._store()
        summary = B.backfill(store, ["not-an-address", PAYEE],
                             _pager({PAYEE.lower(): [([], None)]}))
        self.assertEqual(summary["payees"], 1)
        self.assertIn("skipped", summary["per_payee"]["not-an-address"])

    def test_multi_payee(self):
        p2 = "0x" + "f" * 40
        fetch = _pager({
            PAYEE.lower(): [([_item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64)], None)],
            p2.lower(): [([_item(p2, "0x" + "c" * 40, tx="0x" + "2" * 64)], None)]})
        store = self._store()
        summary = B.backfill(store, [PAYEE, p2], fetch)
        self.assertEqual(summary["payees"], 2)
        self.assertEqual(summary["ingested"], 2)


if __name__ == "__main__":
    unittest.main()
