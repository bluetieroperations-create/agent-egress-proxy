"""
Tests for traceipt_ingest.py -- Traceipt receipts -> Black_Wall reputation.

Each test states the mutation it kills. A settlement fixture mirrors Traceipt's
schema (traceipt/traceipt/schema.py::validate_settlement).
"""
import tempfile
import unittest

import traceipt_ingest as TI


def _receipt(*, kind="payment", verified=True, payee="0xAAA...", payer="0xBBB...",
             amount_base_units="50000", tx="0x" + "1" * 64, issued="2026-07-01T00:00:00Z"):
    return {
        "receipt_id": "rcpt_x", "kind": kind,
        "settlement": {
            "chain": "base", "tx_hash": tx, "asset": "USDC",
            "asset_contract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amount_base_units": amount_base_units, "payer": payer, "payee": payee,
            "verification_method": "rpc", "verified": verified,
        },
        "issued_at": issued,
        "commerce": {"quoted_amount_base_units": amount_base_units},
    }


class TestReceiptToTransfer(unittest.TestCase):
    """
    Mutation notes:
      - keep atomic units (don't /1e6) -> test_amount_to_decimal FAILS.
      - ingest credit notes -> test_credit_skipped FAILS.
      - ingest unverified -> test_unverified_skipped FAILS.
      - don't lowercase -> test_lowercased FAILS (would split the reputation key).
    """
    def test_maps_verified_payment(self):
        t = TI.receipt_to_transfer(_receipt(payee="0xPAYEE", payer="0xPAYER"))
        self.assertEqual(t["to"], "0xpayee")
        self.assertEqual(t["from"], "0xpayer")
        self.assertEqual(t["tx_hash"][:2], "0x")
        self.assertEqual(t["timestamp"], "2026-07-01T00:00:00Z")

    def test_amount_to_decimal(self):
        # 50000 atomic USDC (6dp) -> 0.05 token units (NOT "50000")
        t = TI.receipt_to_transfer(_receipt(amount_base_units="50000"))
        self.assertEqual(t["amount"], "0.05")

    def test_credit_skipped(self):
        self.assertIsNone(TI.receipt_to_transfer(_receipt(kind="credit")))

    def test_unverified_skipped(self):
        self.assertIsNone(TI.receipt_to_transfer(_receipt(verified=False)))

    def test_lowercased_key(self):
        t = TI.receipt_to_transfer(_receipt(payee="0xDeAdBeEf"))
        self.assertEqual(t["to"], "0xdeadbeef")

    def test_bad_amount_dropped(self):
        self.assertIsNone(TI.receipt_to_transfer(_receipt(amount_base_units="notnum")))
        self.assertIsNone(TI.receipt_to_transfer(_receipt(amount_base_units="0")))

    def test_missing_fields_dropped(self):
        r = _receipt(); r["settlement"].pop("payee")
        self.assertIsNone(TI.receipt_to_transfer(r))
        self.assertIsNone(TI.receipt_to_transfer({"kind": "payment"}))
        self.assertIsNone(TI.receipt_to_transfer(None))

    def test_batch_filters(self):
        rows = TI.receipts_to_transfers([
            _receipt(tx="0x" + "1" * 64),
            _receipt(kind="credit"),            # dropped
            _receipt(verified=False),           # dropped
            _receipt(tx="0x" + "2" * 64),
        ])
        self.assertEqual(len(rows), 2)


class TestIngestReceiptsEndToEnd(unittest.TestCase):
    """
    Mutation note: if the decimal conversion or the reputation key were wrong, the
    ingested settlement wouldn't surface under the payee's lookup -> this FAILS.
    """
    def test_receipt_feeds_reputation(self):
        from reputation_store import ReputationStore
        with tempfile.TemporaryDirectory() as d:
            store = ReputationStore(d + "/rep.db")
            payee = "0x" + "a" * 40
            n = TI.ingest_receipts(store, [
                _receipt(payee=payee, payer="0x" + "b" * 40, tx="0x" + "1" * 64,
                         amount_base_units="90000"),
                _receipt(payee=payee, payer="0x" + "c" * 40, tx="0x" + "2" * 64,
                         amount_base_units="90000"),
            ])
            self.assertEqual(n, 2)                       # two new settlements
            rec = store.lookup(payee)
            self.assertGreaterEqual(rec.get("settlement_count", 0), 2)
            # amount surfaced as decimal (0.09), usable by the price-anomaly path
            obs = rec.get("price_observations") or []
            self.assertTrue(any(str(o.get("amount")) == "0.09" for o in obs))

    def test_verify_gate_drops_unauthenticated(self):
        # a verify() that rejects everything -> nothing feeds reputation
        rows = TI.receipts_to_transfers([_receipt(), _receipt(tx="0x" + "2" * 64)],
                                        verify=lambda r: False)
        self.assertEqual(rows, [])
        # a verify() that accepts -> mapped as normal
        self.assertEqual(len(TI.receipts_to_transfers([_receipt()],
                                                      verify=lambda r: True)), 1)

    def test_verify_fails_closed_on_error(self):
        # a raising verify() must DROP the receipt, never crash ingest
        def boom(r): raise ValueError("jwks fetch failed")
        self.assertEqual(TI.receipts_to_transfers([_receipt()], verify=boom), [])

    def test_empty_is_noop(self):
        class _Boom:
            def ingest_transfers(self, x): raise AssertionError("should not be called")
        self.assertEqual(TI.ingest_receipts(_Boom(), []), 0)
        self.assertEqual(TI.ingest_receipts(_Boom(), [_receipt(kind="credit")]), 0)


if __name__ == "__main__":
    unittest.main()
