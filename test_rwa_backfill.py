#!/usr/bin/env python3
"""
test_rwa_backfill.py -- TDD for the zero-customer RWA corpus backfill. Each test names
the MUTATION it kills.
"""
import os
import tempfile
import unittest

import rwa_backfill as bf
from rwa_backfill import backfill, backfill_token, token_acquisitions
from rwa_ledger import RwaLedger, issuer_trust

TOKEN = "0x" + "22" * 20
B1 = "0x" + "aa" * 20
B2 = "0x" + "bb" * 20
ZERO = "0x" + "00" * 20


def _xfer(to, tx, ts="2026-01-01T00:00:00Z", value="100"):
    return {"to": {"hash": to}, "from": {"hash": ZERO}, "tx_hash": tx,
            "timestamp": ts, "total": {"value": value}}


class TestAcquisitions(unittest.TestCase):
    def _fetch(self, items):
        def fetch(token, params):
            return (items, None)      # single page
        return fetch

    def test_extracts_transfers_to_real_wallets(self):
        acq = token_acquisitions(self._fetch(
            [_xfer(B1, "0x1"), _xfer(B2, "0x2")]), TOKEN)
        self.assertEqual({a["to"] for a in acq}, {B1, B2})

    def test_excludes_burns_and_self(self):
        # MUTATION: counting a transfer to 0x0 (redeem) or to the token itself as an
        # acquisition would fabricate settlements.
        acq = token_acquisitions(self._fetch(
            [_xfer(ZERO, "0x1"), _xfer(TOKEN, "0x2"), _xfer(B1, "0x3")]), TOKEN)
        self.assertEqual([a["to"] for a in acq], [B1])

    def test_dedupes_tx_recipient(self):
        acq = token_acquisitions(self._fetch(
            [_xfer(B1, "0x1"), _xfer(B1, "0x1")]), TOKEN)
        self.assertEqual(len(acq), 1)

    def test_cap(self):
        items = [_xfer(B1, "0x%d" % i) for i in range(10)]
        self.assertEqual(len(token_acquisitions(self._fetch(items), TOKEN, cap=3)), 3)

    def test_pagination(self):
        pages = [([_xfer(B1, "0x1")], {"p": 2}), ([_xfer(B2, "0x2")], None)]
        calls = {"n": 0}

        def fetch(token, params):
            r = pages[calls["n"]]
            calls["n"] += 1
            return r
        self.assertEqual(len(token_acquisitions(fetch, TOKEN, max_pages=5)), 2)


class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.led = RwaLedger(os.path.join(tempfile.mkdtemp(), "rwa.jsonl"))

    def _fetch(self, items):
        return lambda token, params: (items, None)

    def test_backfill_token_direct(self):
        # backfill_token takes the token RECORD dict (not the ledger.record method).
        rec = {"token": TOKEN, "issuer": "ondo", "symbol": "AAPLon",
               "underlying_symbol": "AAPL"}
        n = backfill_token(self.led, rec, "ethereum",
                           self._fetch([_xfer(B1, "0x1"), _xfer(B2, "0x2")]), set())
        self.assertEqual(n, 2)

    def test_writes_buy_and_settled_outcome(self):
        rec = {"token": TOKEN, "issuer": "ondo", "symbol": "AAPLon",
               "underlying_symbol": "AAPL"}
        s = backfill(self.led, [rec], "ethereum",
                     self._fetch([_xfer(B1, "0x1"), _xfer(B2, "0x2")]))
        self.assertEqual(s["acquisitions"], 2)
        rows = self.led.load()
        buys = [e for e in rows if e["event"] == "buy"]
        outs = [e for e in rows if e["event"] == "outcome"]
        self.assertEqual(len(buys), 2)
        self.assertTrue(all(o["settled"] for o in outs))
        self.assertTrue(all(b["source"] == "backfill" for b in buys))
        self.assertTrue(all(b["payer"] in (B1, B2) for b in buys))   # buyer captured

    def test_idempotent_across_runs(self):
        # MUTATION: not keying on tx+recipient (or not loading existing ids) would
        # double-count every re-run.
        rec = {"token": TOKEN, "issuer": "ondo"}
        fetch = self._fetch([_xfer(B1, "0x1"), _xfer(B2, "0x2")])
        backfill(self.led, [rec], "ethereum", fetch)
        s2 = backfill(self.led, [rec], "ethereum", fetch)   # re-run
        self.assertEqual(s2["acquisitions"], 0)
        self.assertEqual(len([e for e in self.led.load() if e["event"] == "buy"]), 2)

    def test_failsoft_bad_token(self):
        s = backfill(self.led, [{"token": "not-an-address"}], "ethereum", self._fetch([]))
        self.assertEqual(s["tokens"], 0)
        self.assertIn("skipped", s["per_token"]["not-an-address"])

    def test_backfilled_issuer_becomes_gradeable(self):
        # THE POINT: an issuer with no data is "insufficient"; after backfilling >=5
        # settled acquisitions it becomes gradeable (settlement-proven).
        rec = {"token": TOKEN, "issuer": "ondo"}
        items = [_xfer("0x" + ("%02x" % i) * 20, "0x%d" % i) for i in range(1, 7)]
        backfill(self.led, [rec], "ethereum", self._fetch(items))
        prof = self.led.issuer_profile("ondo")
        self.assertGreaterEqual(prof["outcomes"], 6)
        self.assertEqual(prof["settlement_success_rate"], 1.0)
        self.assertNotEqual(issuer_trust(prof)["grade"], "insufficient")


if __name__ == "__main__":
    unittest.main()
