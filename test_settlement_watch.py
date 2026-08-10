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
import threading
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

    def test_missing_timestamp_fails_closed_under_since(self):
        # A transfer with no/garbage timestamp must NOT satisfy a time-bounded
        # match (fail-closed), or an undated old payment could confirm a receipt.
        ts_none = sw.extract_usdc_transfers([
            xfer("0xCP", "90000", frm="0xAGENT", ts=None)])
        self.assertIsNone(
            sw.find_settlement(ts_none, "0xCP", "0.09",
                               since_ts="2026-06-27T06:00:00Z"))

    def test_empty_counterparty_never_matches(self):
        self.assertIsNone(sw.find_settlement(self.transfers, "", "0.09"))
        self.assertIsNone(sw.find_settlement(self.transfers, None, "0.09"))

    def test_negative_amount_dropped(self):
        out = sw.extract_usdc_transfers([xfer("0xCP", "-90000")])
        self.assertEqual(out, [])

    def test_chain_client_rejects_bad_tx_hash_and_address(self):
        # No outbound call for a non-hex tx_hash / non-address counterparty
        # (URL path/query injection guard).
        chain = sw.BlockscoutChain()
        self.assertEqual(chain.tx_token_transfers("0xnot/a/hash?x=1"), [])
        self.assertEqual(chain.recent_inbound("addr?evil=1"), [])


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

    def test_payer_binding_rejects_third_party_payment(self):
        # The verdict recorded a payer; a same-amount payment to the counterparty
        # from a DIFFERENT wallet must NOT confirm this agent's receipt.
        payer = "0x" + "a" * 40
        self.led.record_verdict("bw_1", "0xCP", "0.09", "GO", payer=payer,
                                ts="2026-06-27T04:00:00Z")
        wrong = FakeChain(inbound_map={"0xCP": [
            xfer("0xCP", "90000", frm="0x" + "b" * 40,
                 ts="2026-06-27T05:00:00Z")]})
        self.assertEqual(sw.SettlementWatcher(wrong, self.led).confirm_pending(), 0)
        # The agent's OWN payment confirms it.
        right = FakeChain(inbound_map={"0xCP": [
            xfer("0xCP", "90000", frm=payer, ts="2026-06-27T05:00:00Z")]})
        self.assertEqual(sw.SettlementWatcher(right, self.led).confirm_pending(), 1)

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

        # 25 GO payments from 5 DISTINCT payers (real service, not wash-trade),
        # each confirmed ON-CHAIN by the watcher (no self-report).
        for i in range(25):
            payer = "0x" + ("%040x" % (i % 5))
            r, _ = bw.forecast(dict(payload, payer=payer), src, ledger=led)
            tx = "0xtx%d" % i
            chain = FakeChain(tx_map={tx: [xfer(cp, "90000", tx=tx)]})
            w = sw.SettlementWatcher(chain, led)
            w.confirm_by_tx(r["receipt_id"], cp, "0.09", tx)

        rec = src.lookup(cp)
        self.assertEqual(rec["_meta"]["chain_confirmed_settlements"], 25)
        self.assertGreaterEqual(rec["distinct_payers"], 3)
        r2, _ = bw.forecast(payload, src, ledger=led)
        self.assertEqual(r2["verdict"], "GO")

    def test_single_payer_wash_trade_does_NOT_graduate(self):
        # Sybil guard: 25 confirmed settlements but all from ONE payer -> HOLD.
        led = L.EventLedger(os.path.join(tempfile.mkdtemp(), "w.jsonl"))
        src = L.LedgerReputationSource(led)
        cp = "0xCP"
        payload = {"counterparty": cp, "amount": "0.09", "asset": "USDC",
                   "chain": "base", "payer": "0x" + "a" * 40}  # one wallet
        for i in range(25):
            r, _ = bw.forecast(dict(payload), src, ledger=led)
            tx = "0xtx%d" % i
            sw.SettlementWatcher(FakeChain(tx_map={tx: [xfer(cp, "90000", tx=tx)]}),
                                 led).confirm_by_tx(r["receipt_id"], cp, "0.09", tx)
        rec = src.lookup(cp)
        self.assertEqual(rec["distinct_payers"], 1)
        self.assertEqual(bw.forecast(payload, src, ledger=led)[0]["verdict"], "HOLD")


class TestWatchLoopClosesFlywheel(unittest.TestCase):
    """The wiring: run_watch_loop driving a real watcher confirms a pending GO
    from the chain and writes a chain-confirmed outcome -- the loop, not a direct
    confirm_by_tx call, closes the moat's read-back."""

    def test_loop_confirms_pending_go(self):
        led = L.EventLedger(os.path.join(tempfile.mkdtemp(), "loop.jsonl"))
        payer = "0x" + "a" * 40
        led.record_verdict("bw_1", "0xCP", "0.09", "GO", payer=payer,
                           ts="2026-06-27T04:00:00Z")
        chain = FakeChain(inbound_map={"0xCP": [
            xfer("0xCP", "90000", frm=payer, ts="2026-06-27T05:00:00Z")]})
        watcher = sw.SettlementWatcher(chain, led)

        # Drive it through the real loop; the watcher stops the loop after its
        # first pass so this is deterministic and fast.
        ev = threading.Event()

        class OneShot:
            def confirm_pending(self, limit=None):
                n = watcher.confirm_pending(limit=limit)
                ev.set()
                return n

        passes = bw.run_watch_loop(OneShot(), 1, 25, ev)
        self.assertEqual(passes, 1)
        recs = led.aggregate()
        self.assertEqual(recs["0xCP"]["settlement_count"], 1)
        self.assertTrue(recs["0xCP"]["_meta"]["known"])
        self.assertEqual(recs["0xCP"]["_meta"]["chain_confirmed_settlements"], 1)


class TestStarvationRetirement(unittest.TestCase):
    """confirm_pending age-bound: stale never-settling GOs must be retired from the
    scan BEFORE the batch slice, so they can't clog the batch and starve real ones."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "l.jsonl")
        self.led = L.EventLedger(self.path)
        self.now = "2026-07-08T05:00:00Z"

    def test_starvation_regression_stale_go_does_not_block_fresh(self):
        # THE BUG: with limit=1, an OLD never-settling GO sits at the front of the
        # chronological pending list and permanently eats the single batch slot,
        # so a FRESH settleable GO behind it is never scanned. Age-retirement fixes
        # it: the old one drops out and the fresh one gets confirmed.
        payer = "0x" + "a" * 40
        self.led.record_verdict("bw_old", "0xDEAD", "0.09", "GO", payer=payer,
                                ts="2020-01-01T00:00:00Z")   # ancient, never settles
        self.led.record_verdict("bw_new", "0xCP", "0.09", "GO", payer=payer,
                                ts="2026-07-08T04:30:00Z")   # fresh, settleable
        chain = FakeChain(inbound_map={"0xCP": [
            xfer("0xCP", "90000", frm=payer, ts="2026-07-08T04:45:00Z")]})
        w = sw.SettlementWatcher(chain, self.led, max_age_seconds=86400)
        n = w.confirm_pending(limit=1, now_ts=self.now)
        self.assertEqual(n, 1)  # fresh one confirmed despite the stale one ahead of it
        self.assertEqual(self.led.aggregate()["0xCP"]["settlement_count"], 1)

    def test_stale_go_is_retired_even_when_settleable(self):
        # An old GO that COULD match on-chain is still retired past the window
        # (we've stopped chasing it). Contrast with no-max-age below.
        payer = "0x" + "a" * 40
        self.led.record_verdict("bw_old", "0xCP", "0.09", "GO", payer=payer,
                                ts="2020-01-01T00:00:00Z")
        chain = FakeChain(inbound_map={"0xCP": [
            xfer("0xCP", "90000", frm=payer, ts="2020-01-01T01:00:00Z")]})
        w = sw.SettlementWatcher(chain, self.led, max_age_seconds=86400)
        self.assertEqual(w.confirm_pending(now_ts=self.now), 0)  # retired -> not scanned

    def test_no_max_age_keeps_old_behavior(self):
        # Backward compat: max_age_seconds=None -> old GOs are still scanned.
        payer = "0x" + "a" * 40
        self.led.record_verdict("bw_old", "0xCP", "0.09", "GO", payer=payer,
                                ts="2020-01-01T00:00:00Z")
        chain = FakeChain(inbound_map={"0xCP": [
            xfer("0xCP", "90000", frm=payer, ts="2020-01-01T01:00:00Z")]})
        w = sw.SettlementWatcher(chain, self.led, max_age_seconds=None)
        self.assertEqual(w.confirm_pending(now_ts=self.now), 1)

    def test_fresh_go_within_window_is_kept(self):
        # A GO inside the window is NOT retired.
        payer = "0x" + "a" * 40
        self.led.record_verdict("bw_new", "0xCP", "0.09", "GO", payer=payer,
                                ts="2026-07-08T04:59:00Z")
        chain = FakeChain(inbound_map={"0xCP": [
            xfer("0xCP", "90000", frm=payer, ts="2026-07-08T04:59:30Z")]})
        w = sw.SettlementWatcher(chain, self.led, max_age_seconds=86400)
        self.assertEqual(w.confirm_pending(now_ts=self.now), 1)

    def test_unparseable_ts_is_kept_not_retired(self):
        # A verdict we can't age (bad/missing ts) is kept -> keep trying, never
        # silently dropped by the age filter.
        payer = "0x" + "a" * 40
        self.led.record_verdict("bw_x", "0xCP", "0.09", "GO", payer=payer,
                                ts="not-a-timestamp")
        chain = FakeChain(inbound_map={"0xCP": [
            xfer("0xCP", "90000", frm=payer, ts="2026-07-08T04:59:30Z")]})
        w = sw.SettlementWatcher(chain, self.led, max_age_seconds=86400)
        self.assertEqual(w.confirm_pending(now_ts=self.now), 1)

    def test_verdict_exactly_at_cutoff_is_kept(self):
        # BOUNDARY (kills >= -> >): a GO whose ts is EXACTLY the cutoff
        # (now - max_age) must be KEPT, not retired. With now=05:00 and
        # max_age=3600, cutoff is 04:00; a verdict at exactly 04:00:00 is on the
        # keep side of an inclusive boundary. A `>` mutation drops it.
        payer = "0x" + "a" * 40
        self.led.record_verdict("bw_edge", "0xCP", "0.09", "GO", payer=payer,
                                ts="2026-07-08T04:00:00Z")  # exactly now - 3600s
        chain = FakeChain(inbound_map={"0xCP": [
            xfer("0xCP", "90000", frm=payer, ts="2026-07-08T04:00:10Z")]})
        w = sw.SettlementWatcher(chain, self.led, max_age_seconds=3600)
        self.assertEqual(w.confirm_pending(now_ts=self.now), 1)  # kept, confirmed

    def test_zero_max_age_disables_retirement_not_retire_everything(self):
        # FOOT-GUN GUARD: an operator setting max_age=0 (or negative) almost
        # certainly means "no age bound / unlimited" (the common 0=off convention),
        # NOT "retire every pending GO". The buggy behavior floored 0 to a cutoff of
        # `now`, retiring even a GO recorded seconds ago whose settlement already
        # indexed -- silently killing the whole confirmation flywheel while
        # /v1/stats still shows passes climbing. 0/negative must DISABLE retirement.
        payer = "0x" + "a" * 40
        self.led.record_verdict("bw_fresh", "0xCP", "0.09", "GO", payer=payer,
                                ts="2026-07-08T04:59:30Z")  # 30s before now
        chain = FakeChain(inbound_map={"0xCP": [
            xfer("0xCP", "90000", frm=payer, ts="2026-07-08T04:59:40Z")]})
        w0 = sw.SettlementWatcher(chain, self.led, max_age_seconds=0)
        self.assertEqual(w0.confirm_pending(now_ts=self.now), 1)  # NOT retired

    def test_negative_max_age_disables_retirement(self):
        payer = "0x" + "a" * 40
        self.led.record_verdict("bw_fresh", "0xCP", "0.09", "GO", payer=payer,
                                ts="2026-07-08T04:59:30Z")
        chain = FakeChain(inbound_map={"0xCP": [
            xfer("0xCP", "90000", frm=payer, ts="2026-07-08T04:59:40Z")]})
        wneg = sw.SettlementWatcher(chain, self.led, max_age_seconds=-5)
        self.assertEqual(wneg.confirm_pending(now_ts=self.now), 1)  # NOT retired


if __name__ == "__main__":
    unittest.main()
