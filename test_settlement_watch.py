"""
Offline tests for settlement_watch (pure matchers + watcher with a fake chain).

Run: python -m unittest test_settlement_watch.py -v

The matching core is pure and tested from canned Blockscout JSON. The watcher is
tested with an injected fake chain + temp ledger -- no network. One test proves
the trustless flywheel: a chain-confirmed `settled` graduates a counterparty
with NO self-reports involved.
"""
import os
import tempfile
import unittest

import blackwall as bw
import ledger as L
import settlement_watch as sw

USDC = sw.BASE_USDC


def xfer(to, amount_units, frm="0xsender", token=USDC, decimals="6",
         tx="0xtx", ts="2026-06-27T05:00:00.000000Z"):
    # Mirrors the REAL Blockscout v2 shape: contract under `token.address_hash`.
    return {"token": {"address_hash": token, "symbol": "USDC"},
            "total": {"value": amount_units, "decimals": decimals},
            "to": {"hash": to}, "from": {"hash": frm},
            "transaction_hash": tx, "timestamp": ts}


class TestExtract(unittest.TestCase):
    """
    extract_usdc_transfers: keep only USDC, decimal-adjust, normalize.

    Mutation note: drop the token-address filter -> test_non_usdc_dropped FAILS
    (a worthless lookalike token of the same amount could spoof a settlement).
    """

    def test_usdc_extracted_and_adjusted(self):
        out = sw.extract_usdc_transfers([xfer("0xCP", "90000")])
        self.assertEqual(len(out), 1)
        self.assertEqual(str(out[0]["amount"]), "0.09")
        self.assertEqual(out[0]["to"], "0xcp")

    def test_non_usdc_dropped(self):
        out = sw.extract_usdc_transfers([xfer("0xCP", "90000", token="0xFAKE")])
        self.assertEqual(out, [])


class TestFindSettlement(unittest.TestCase):
    """
    find_settlement: first transfer matching recipient + amount (+ time/sender).

    Mutation notes:
      - Drop the recipient check  -> test_wrong_recipient FAILS.
      - Drop the amount check     -> test_wrong_amount FAILS.
      - Drop the since_ts guard   -> test_old_payment_excluded FAILS.
    """

    def setUp(self):
        self.transfers = sw.extract_usdc_transfers([
            xfer("0xCP", "90000", frm="0xAGENT", ts="2026-06-27T05:00:00Z"),
        ])

    def test_match(self):
        m = sw.find_settlement(self.transfers, "0xCP", "0.09")
        self.assertIsNotNone(m)

    def test_wrong_recipient(self):
        self.assertIsNone(sw.find_settlement(self.transfers, "0xOTHER", "0.09"))

    def test_wrong_amount(self):
        self.assertIsNone(sw.find_settlement(self.transfers, "0xCP", "0.10"))

    def test_tolerance(self):
        self.assertIsNotNone(
            sw.find_settlement(self.transfers, "0xCP", "0.0905", tolerance="0.001"))

    def test_sender_guard(self):
        self.assertIsNone(
            sw.find_settlement(self.transfers, "0xCP", "0.09", from_addr="0xWRONG"))
        self.assertIsNotNone(
            sw.find_settlement(self.transfers, "0xCP", "0.09", from_addr="0xAGENT"))

    def test_old_payment_excluded(self):
        # A same-amount payment from BEFORE the verdict must not count.
        self.assertIsNone(
            sw.find_settlement(self.transfers, "0xCP", "0.09",
                               since_ts="2026-06-27T06:00:00Z"))


class FakeChain:
    """Injectable chain stub."""
    def __init__(self, tx_map=None, inbound_map=None):
        self.tx_map = tx_map or {}
        self.inbound_map = inbound_map or {}

    def tx_token_transfers(self, tx_hash):
        return sw.extract_usdc_transfers(self.tx_map.get(tx_hash, []))

    def recent_inbound(self, counterparty):
        return sw.extract_usdc_transfers(self.inbound_map.get(counterparty, []))


class TestWatcher(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "l.jsonl")
        self.led = L.EventLedger(self.path)

    def test_confirm_by_tx_records_chain_settled(self):
        self.led.record_verdict("bw_1", "0xCP", "0.09", "GO")  # verdict exists
        chain = FakeChain(tx_map={"0xGOOD": [xfer("0xCP", "90000")]})
        w = sw.SettlementWatcher(chain, self.led)
        m = w.confirm_by_tx("bw_1", "0xCP", "0.09", "0xGOOD")
        self.assertIsNotNone(m)
        recs = self.led.aggregate()
        self.assertEqual(recs["0xCP"]["settlement_count"], 1)
        self.assertEqual(recs["0xCP"]["_meta"]["chain_confirmed_settlements"], 1)

    def test_confirm_by_tx_rejects_mismatch(self):
        # tx exists but pays the WRONG recipient -> nothing recorded.
        chain = FakeChain(tx_map={"0xBAD": [xfer("0xATTACKER", "90000")]})
        w = sw.SettlementWatcher(chain, self.led)
        self.assertIsNone(w.confirm_by_tx("bw_1", "0xCP", "0.09", "0xBAD"))
        self.assertEqual(self.led.aggregate(), {})

    def test_confirm_pending_scans_and_records(self):
        # A GO verdict with no outcome -> watcher matches inbound -> settled.
        self.led.record_verdict("bw_1", "0xCP", "0.09", "GO",
                                ts="2026-06-27T04:00:00Z")
        chain = FakeChain(inbound_map={"0xCP": [
            xfer("0xCP", "90000", ts="2026-06-27T05:00:00Z")]})
        w = sw.SettlementWatcher(chain, self.led)
        self.assertEqual(w.confirm_pending(), 1)
        # Idempotent: already has an outcome now -> no double confirm.
        self.assertEqual(w.confirm_pending(), 0)

    def test_one_tx_confirms_at_most_one_receipt(self):
        # Two GO receipts, same counterparty + amount, but only ONE on-chain
        # payment -> exactly one confirmed (no over-count). Regression.
        self.led.record_verdict("bw_A", "0xCP", "0.09", "GO",
                                ts="2026-06-27T04:00:00Z")
        self.led.record_verdict("bw_B", "0xCP", "0.09", "GO",
                                ts="2026-06-27T04:00:00Z")
        chain = FakeChain(inbound_map={"0xCP": [
            xfer("0xCP", "90000", tx="0xONLYONE", ts="2026-06-27T05:00:00Z")]})
        w = sw.SettlementWatcher(chain, self.led)
        self.assertEqual(w.confirm_pending(), 1)
        self.assertEqual(self.led.aggregate()["0xCP"]["settlement_count"], 1)

    def test_confirm_pending_skips_hold(self):
        # Only GO verdicts are settlement candidates.
        self.led.record_verdict("bw_1", "0xCP", "0.09", "HOLD")
        chain = FakeChain(inbound_map={"0xCP": [xfer("0xCP", "90000")]})
        w = sw.SettlementWatcher(chain, self.led)
        self.assertEqual(w.confirm_pending(), 0)


class TestTrustlessFlywheel(unittest.TestCase):
    """Reputation built from CHAIN-CONFIRMED settlements only -- no self-reports."""

    def test_chain_confirmed_settlements_graduate_counterparty(self):
        path = os.path.join(tempfile.mkdtemp(), "fly.jsonl")
        led = L.EventLedger(path)
        src = L.LedgerReputationSource(led)
        cp = "0xCP"
        payload = {"counterparty": cp, "amount": "0.09",
                   "asset": "USDC", "chain": "base"}

        # First contact -> thin -> HOLD.
        r1, _ = bw.forecast(payload, src, ledger=led)
        self.assertEqual(r1["verdict"], "HOLD")

        # 25 GO payments, each confirmed ON-CHAIN by the watcher (no self-report).
        for i in range(25):
            r, _ = bw.forecast(dict(payload), src, ledger=led)
            tx = "0xtx%d" % i
            chain = FakeChain(tx_map={tx: [xfer(cp, "90000", tx=tx)]})
            w = sw.SettlementWatcher(chain, led)
            # confirm THIS receipt directly (scan would also work).
            w.confirm_by_tx(r["receipt_id"], cp, "0.09", tx)

        rec = src.lookup(cp)
        self.assertEqual(rec["_meta"]["chain_confirmed_settlements"], 25)
        r2, _ = bw.forecast(payload, src, ledger=led)
        self.assertEqual(r2["verdict"], "GO")


if __name__ == "__main__":
    unittest.main()
