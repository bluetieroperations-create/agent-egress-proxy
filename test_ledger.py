"""
Tests for the verdict->outcome ledger and the moat flywheel.

Run: python -m unittest test_ledger.py -v

aggregate_counterparties is the pure core; the flywheel test proves the loop:
a recorded verdict + observed outcome becomes the reputation that drives the
NEXT verdict.
"""
import os
import tempfile
import unittest
from decimal import Decimal

import blackwall as bw
import ledger as L


def verdict(rid, cp, amount="0.09", ts="t"):
    return {"kind": "verdict", "receipt_id": rid, "counterparty": cp,
            "amount": amount, "ts": ts}


def chain_settled(rid, tx, observed_amount=None):
    # A CHAIN-WATCH confirmation -- the only kind that counts toward reputation.
    e = {"kind": "outcome", "receipt_id": rid, "outcome": "settled",
         "source": "chain-watch", "settlement_tx": tx}
    if observed_amount is not None:
        e["observed_amount"] = observed_amount
    return e


def self_report(rid, outcome):
    return {"kind": "outcome", "receipt_id": rid, "outcome": outcome}


class TestAggregate(unittest.TestCase):
    """
    aggregate_counterparties: event stream -> per-counterparty records.

    Mutation notes:
      - dispute_rate = 0 when no outcomes -> test_unobserved_dispute_is_none FAILS.
      - Don't join outcomes by receipt_id -> test_outcome_attributed FAILS.
      - Count bad outcomes as settlements -> test_dispute_rate FAILS.
    """

    def test_unobserved_dispute_is_none(self):
        # A verdict with no outcome yet: dispute_rate UNKNOWN, not 0.
        events = [{"kind": "verdict", "receipt_id": "bw_1",
                   "counterparty": "0xA", "amount": "0.09", "ts": "t1"}]
        recs = L.aggregate_counterparties(events)
        self.assertIsNone(recs["0xA"]["dispute_rate"])
        self.assertFalse(recs["0xA"]["_meta"]["known"])

    def test_chain_confirmed_counts_self_report_does_not(self):
        # Only a chain-watch confirmation counts; a self-report is advisory.
        recs = L.aggregate_counterparties([
            verdict("bw_1", "0xA"), chain_settled("bw_1", "0xtx1")])
        self.assertEqual(recs["0xA"]["settlement_count"], 1)
        self.assertEqual(recs["0xA"]["price_history"], ["0.09"])
        self.assertTrue(recs["0xA"]["_meta"]["known"])
        # a self-report alone -> NOT counted, NOT known.
        recs2 = L.aggregate_counterparties([
            verdict("bw_2", "0xB"), self_report("bw_2", "delivered")])
        self.assertEqual(recs2["0xB"]["settlement_count"], 0)
        self.assertFalse(recs2["0xB"]["_meta"]["known"])
        self.assertEqual(recs2["0xB"]["_meta"]["advisory_self_reports"], 1)

    def test_dispute_rate_over_confirmed(self):
        # 5 chain-confirmed settlements; the payer disputes one of them.
        events = [verdict("r%d" % i, "0xA") for i in range(5)]
        events += [chain_settled("r%d" % i, "0xtx%d" % i) for i in range(5)]
        events += [self_report("r4", "disputed")]  # quality of a REAL settlement
        recs = L.aggregate_counterparties(events)
        self.assertEqual(recs["0xA"]["settlement_count"], 5)
        self.assertAlmostEqual(recs["0xA"]["dispute_rate"], 0.2)  # 1 of 5

    def test_recent_dispute_rate_windowed(self):
        # 20 clean settlements then 4 recent disputes: lifetime rate stays low, but
        # the recency window exposes the counterparty going bad.
        # Mutation: computing recent over ALL outcomes -> recent == lifetime, misses it.
        events = []
        for i in range(20):
            rid = "g%02d" % i
            events += [verdict(rid, "0xC", ts="t0%02d" % i),
                       chain_settled(rid, "tx%02d" % i)]
        for i in range(4):
            rid = "b%02d" % i
            events += [verdict(rid, "0xC", ts="t1%02d" % i),
                       chain_settled(rid, "txb%02d" % i), self_report(rid, "disputed")]
        rec = L.aggregate_counterparties(events)["0xC"]
        self.assertEqual(rec["settlement_count"], 24)
        self.assertAlmostEqual(rec["dispute_rate"], 4 / 24, places=3)   # lifetime: low
        self.assertEqual(rec["recent_outcomes"], L.RECENT_WINDOW)       # last 10
        self.assertAlmostEqual(rec["recent_dispute_rate"], 0.4, places=3)  # 6 good + 4 bad

    def test_recent_dispute_rate_none_without_outcomes(self):
        rec = L.aggregate_counterparties([verdict("r", "0xC")])["0xC"]
        self.assertIsNone(rec["recent_dispute_rate"])
        self.assertEqual(rec["recent_outcomes"], 0)

    def test_self_reported_dispute_on_unsettled_is_ignored(self):
        # Dispute on a receipt that never chain-settled -> no effect (anti-poison).
        recs = L.aggregate_counterparties([
            verdict("r1", "0xA"), self_report("r1", "disputed")])
        self.assertEqual(recs["0xA"]["settlement_count"], 0)
        self.assertIsNone(recs["0xA"]["dispute_rate"])

    def test_one_tx_confirms_once(self):
        # The SAME settlement_tx across two receipts confirms at most one.
        recs = L.aggregate_counterparties([
            verdict("r1", "0xA"), verdict("r2", "0xA"),
            chain_settled("r1", "0xSAME"), chain_settled("r2", "0xSAME")])
        self.assertEqual(recs["0xA"]["settlement_count"], 1)

    def test_chain_confirmation_is_sticky(self):
        # A later self-report must NOT erase a real chain confirmation.
        recs = L.aggregate_counterparties([
            verdict("r1", "0xA"), chain_settled("r1", "0xtx"),
            self_report("r1", "disputed")])
        self.assertEqual(recs["0xA"]["settlement_count"], 1)   # still confirmed
        self.assertAlmostEqual(recs["0xA"]["dispute_rate"], 1.0)  # quality bad

    def test_price_history_only_from_confirmed_observed_amount(self):
        # Self-reported observed_amount must NOT poison price_history.
        recs = L.aggregate_counterparties([
            verdict("r1", "0xA", amount="0.09"),
            {"kind": "outcome", "receipt_id": "r1", "outcome": "settled",
             "observed_amount": "5.00"},  # self-report (no chain-watch source)
        ])
        self.assertEqual(recs["0xA"]["price_history"], [])  # ignored

    def test_price_observations_are_payer_attributed(self):
        # Confirmed settlements carry {payer, amount} so the verdict engine can
        # build a wash-trade-resistant median. Payer-less verdicts contribute to
        # price_history but NOT to price_observations.
        events = [
            {"kind": "verdict", "receipt_id": "r1", "counterparty": "0xC",
             "amount": "5.00", "payer": "0xpayerA", "ts": "t1"},
            chain_settled("r1", "0xtx1"),
            {"kind": "verdict", "receipt_id": "r2", "counterparty": "0xC",
             "amount": "6.00", "payer": "0xpayerB", "ts": "t2"},
            chain_settled("r2", "0xtx2"),
            {"kind": "verdict", "receipt_id": "r3", "counterparty": "0xC",
             "amount": "7.00", "ts": "t3"},  # no payer
            chain_settled("r3", "0xtx3"),
        ]
        rec = L.aggregate_counterparties(events)["0xC"]
        obs = rec["price_observations"]
        self.assertEqual(len(obs), 2)  # only the two payer-bound settlements
        self.assertEqual({o["payer"] for o in obs}, {"0xpayerA", "0xpayerB"})
        self.assertEqual({o["amount"] for o in obs}, {"5.00", "6.00"})
        # price_history still includes the payer-less one
        self.assertEqual(len(rec["price_history"]), 3)
        # and it flows into the wash-resistant median path
        med, n = bw.robust_price_median(obs, min_payers=2)
        self.assertEqual(n, 2)

    def test_price_observations_carry_resource(self):
        # REGRESSION: the verdict's `resource` must flow into the observation so
        # the engine can do per-invoice-class price comparison. Mutation: drop the
        # resource tag in aggregate -> this FAILS (obs has no resource).
        events = [
            {"kind": "verdict", "receipt_id": "r1", "counterparty": "0xC",
             "amount": "5000", "payer": "0xpayerA", "resource": "INV", "ts": "t1"},
            chain_settled("r1", "0xtx1"),
        ]
        rec = L.aggregate_counterparties(events)["0xC"]
        self.assertEqual(rec["price_observations"][0]["resource"], "INV")

    def test_orphan_outcome_ignored(self):
        events = [chain_settled("ghost", "0xtx")]
        self.assertEqual(L.aggregate_counterparties(events), {})


class TestEventLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "led.jsonl")
        self.led = L.EventLedger(self.path)

    def test_roundtrip_and_aggregate(self):
        self.led.record_verdict("bw_x", "0xA", "0.09", "GO", score=0.99)
        self.led.record_outcome("bw_x", "settled", settlement_tx="0xtx",
                                source="chain-watch")
        recs = self.led.aggregate()
        self.assertEqual(recs["0xA"]["settlement_count"], 1)

    def test_invalid_outcome_rejected(self):
        with self.assertRaises(ValueError):
            self.led.record_outcome("bw_x", "exploded")

    def test_corrupt_line_skipped(self):
        with open(self.path, "a") as f:
            f.write("not json\n")
        self.led.record_verdict("bw_y", "0xB", "1.0", "HOLD")
        # read survives the corrupt line
        self.assertIn("0xB", self.led.aggregate())


class TestChainedSource(unittest.TestCase):
    def test_ledger_leads_then_falls_back(self):
        # Ledger knows 0xA; mock provides the cold-start default for others.
        led = L.EventLedger(os.path.join(tempfile.mkdtemp(), "l.jsonl"))
        for i in range(3):
            led.record_verdict("r%d" % i, "0xA", "0.09", "GO")
            led.record_outcome("r%d" % i, "settled", settlement_tx="0xtx%d" % i,
                               source="chain-watch")
        chained = L.ChainedReputationSource(
            [L.LedgerReputationSource(led), bw.MockReputationSource()])
        # Known in ledger -> ledger record wins.
        self.assertEqual(chained.lookup("0xA")["_meta"]["source"],
                         "blackwall-ledger")
        # Unknown to ledger but seeded in mock -> falls through to mock.
        rec = chained.lookup("0xSANCTIONED00000000000000000000000000003")
        self.assertTrue(rec["sanctioned"])

    def test_sanctioned_not_masked_by_leading_self_report(self):
        # A self-report on a sanctioned counterparty must NOT let the leading
        # ledger source mask the sanctions flag from a downstream source.
        class SanctSrc:
            def lookup(self, c):
                return {"settlement_count": 0, "sanctioned": True,
                        "price_history": [], "_meta": {"known": True}}
        led = L.EventLedger(os.path.join(tempfile.mkdtemp(), "l.jsonl"))
        led.record_verdict("r1", "0xSANC", "0.09", "STOP")
        led.record_outcome("r1", "settled")  # self-report
        chained = L.ChainedReputationSource(
            [L.LedgerReputationSource(led), SanctSrc()])
        self.assertTrue(chained.lookup("0xSANC")["sanctioned"])


class TestFlywheel(unittest.TestCase):
    """The loop: verdict -> outcome -> drives the NEXT verdict's reputation."""

    def test_self_reports_do_NOT_graduate_only_chain_confirmed_do(self):
        # Security property: the verdict's thin-history gate counts only
        # CHAIN-CONFIRMED settlements, so unauthenticated self-reports cannot
        # graduate a counterparty to GO -- only the settlement watcher can.
        path = os.path.join(tempfile.mkdtemp(), "fly.jsonl")
        led = L.EventLedger(path)
        src = L.LedgerReputationSource(led)
        cp = "0xFRESH"
        payload = {"counterparty": cp, "amount": "0.09",
                   "asset": "USDC", "chain": "base"}

        # First contact -> thin -> HOLD.
        resp1, err = bw.forecast(payload, src, ledger=led)
        self.assertIsNone(err)
        self.assertEqual(resp1["verdict"], "HOLD")

        # 25 SELF-REPORTED deliveries -> do NOT count at all (advisory only) ->
        # still HOLD (self-reports can't graduate).
        for i in range(25):
            r, _ = bw.forecast(dict(payload), src, ledger=led)
            led.record_outcome(r["receipt_id"], "delivered")  # source=self-report
        resp2, _ = bw.forecast(payload, src, ledger=led)
        self.assertEqual(resp2["verdict"], "HOLD")
        self.assertEqual(src.lookup(cp)["settlement_count"], 0)
        self.assertEqual(src.lookup(cp)["confirmed_settlement_count"], 0)

        # Now 25 CHAIN-CONFIRMED settlements from 5 distinct payers -> GO.
        for i in range(25):
            p = dict(payload, payer="0x" + ("%040x" % (i % 5)))
            r, _ = bw.forecast(p, src, ledger=led)
            led.record_outcome(r["receipt_id"], "settled", settlement_tx="0xtx%d" % i,
                               source="chain-watch")
        resp3, _ = bw.forecast(payload, src, ledger=led)
        self.assertEqual(resp3["verdict"], "GO")


class TestUsageStats(unittest.TestCase):
    """usage_stats: operator funnel metrics over the event stream."""

    EVENTS = [
        {"kind": "verdict", "ts": "2026-07-07T01:00:00Z", "counterparty": "0xA",
         "agent_id": "did:1", "payer": "0xP1", "resource": "r1", "verdict": "GO"},
        {"kind": "verdict", "ts": "2026-07-07T02:00:00Z", "counterparty": "0xB",
         "agent_id": "did:1", "payer": "0xP2", "resource": "r1", "verdict": "STOP"},
        {"kind": "verdict", "ts": "2026-07-07T03:00:00Z", "counterparty": "0xA",
         "agent_id": "did:2", "payer": "0xP2", "resource": "r2", "verdict": "HOLD"},
        {"kind": "outcome", "ts": "2026-07-07T04:00:00Z", "outcome": "settled"},  # not a verdict
    ]

    def test_counts_and_mix(self):
        # mutation: counting outcome events, or miscounting the verdict mix
        s = L.usage_stats(self.EVENTS)
        self.assertEqual(s["verdicts_total"], 3)                    # outcome excluded
        self.assertEqual(s["by_verdict"], {"GO": 1, "STOP": 1, "HOLD": 1})

    def test_distinct_callers_deduped(self):
        # mutation: counting rows instead of DISTINCT identifiers
        s = L.usage_stats(self.EVENTS)
        self.assertEqual(s["distinct_counterparties"], 2)          # 0xA twice -> 1
        self.assertEqual(s["distinct_agents"], 2)
        self.assertEqual(s["distinct_payers"], 2)                  # 0xP2 twice -> 1
        self.assertEqual(s["distinct_resources"], 2)

    def test_time_bounds(self):
        s = L.usage_stats(self.EVENTS)
        self.assertEqual(s["first_verdict"], "2026-07-07T01:00:00Z")
        self.assertEqual(s["last_verdict"], "2026-07-07T03:00:00Z")

    def test_empty_and_missing_fields(self):
        # no events, and a verdict missing identifiers -> no crash, zeros
        self.assertEqual(L.usage_stats([])["verdicts_total"], 0)
        s = L.usage_stats([{"kind": "verdict", "verdict": "GO"}])
        self.assertEqual(s["verdicts_total"], 1)
        self.assertEqual(s["distinct_payers"], 0)
        self.assertIsNone(s["first_verdict"])


class TestCounterpartyFlow(unittest.TestCase):
    """counterparty_flow: aggregate/velocity -- cumulative flow to ONE payee in a window."""

    def _v(self, cp, amount, ts):
        return {"kind": "verdict", "counterparty": cp, "amount": str(amount), "ts": ts}

    def test_sums_recent_flow_to_same_counterparty(self):
        # The velocity scenario: many individually-fine sub-cap payments summing up.
        ev = [self._v("0xBAD", "5.00", "2026-07-08T04:00:00Z"),
              self._v("0xBAD", "5.00", "2026-07-08T04:10:00Z"),
              self._v("0xBAD", "5.00", "2026-07-08T04:20:00Z")]
        r = L.counterparty_flow(ev, "0xBAD", "2026-07-08T04:25:00Z", 3600)
        # Kills: not accumulating across payments -- the whole point of the signal.
        self.assertEqual(r["count"], 3)
        self.assertEqual(r["total_amount"], Decimal("15.00"))

    def test_excludes_out_of_window(self):
        # Kills: ignoring the window -> stale flow counted forever.
        ev = [self._v("0xBAD", "5.00", "2026-07-08T01:00:00Z"),   # >1h old
              self._v("0xBAD", "5.00", "2026-07-08T04:20:00Z")]
        r = L.counterparty_flow(ev, "0xBAD", "2026-07-08T04:25:00Z", 3600)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["total_amount"], Decimal("5.00"))

    def test_excludes_other_counterparties(self):
        ev = [self._v("0xBAD", "5.00", "2026-07-08T04:20:00Z"),
              self._v("0xG00D", "5.00", "2026-07-08T04:21:00Z")]
        r = L.counterparty_flow(ev, "0xBAD", "2026-07-08T04:25:00Z", 3600)
        self.assertEqual(r["count"], 1)

    def test_counterparty_match_is_case_insensitive(self):
        # Kills: exact-case match -> a checksummed vs lowercase address splits flow,
        # letting an attacker dodge the aggregate by varying case.
        ev = [self._v("0xAbCd", "5.00", "2026-07-08T04:20:00Z")]
        self.assertEqual(L.counterparty_flow(ev, "0xabcd", "2026-07-08T04:25:00Z", 3600)["count"], 1)

    def test_skips_non_verdict_and_garbage_amounts(self):
        # Kills: counting outcome events / garbage / non-positive amounts as flow.
        ev = [{"kind": "outcome", "counterparty": "0xBAD", "amount": "99", "ts": "2026-07-08T04:20:00Z"},
              self._v("0xBAD", "notanumber", "2026-07-08T04:21:00Z"),
              self._v("0xBAD", "0", "2026-07-08T04:22:00Z"),
              self._v("0xBAD", "5.00", "2026-07-08T04:23:00Z")]
        r = L.counterparty_flow(ev, "0xBAD", "2026-07-08T04:25:00Z", 3600)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["total_amount"], Decimal("5.00"))

    def test_bad_now_empty_or_no_counterparty_is_zero(self):
        ev = [self._v("0xBAD", "5.00", "2026-07-08T04:20:00Z")]
        self.assertEqual(L.counterparty_flow(ev, "0xBAD", "not-a-ts", 3600)["count"], 0)
        self.assertEqual(L.counterparty_flow([], "0xBAD", "2026-07-08T04:25:00Z", 3600)["count"], 0)
        self.assertEqual(L.counterparty_flow(ev, "", "2026-07-08T04:25:00Z", 3600)["count"], 0)

    def test_naive_timestamp_does_not_crash(self):
        # Regression (AUDIT-FOUND): a ledger ts without a Z/offset parses naive;
        # comparing it to the aware window boundary raised TypeError and crashed the
        # whole flow mid-verdict. Naive is now treated as UTC.
        ev = [self._v("0xBAD", "5.00", "2026-07-08T04:20:00")]  # no Z
        r = L.counterparty_flow(ev, "0xBAD", "2026-07-08T04:25:00Z", 3600)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["total_amount"], Decimal("5.00"))

    def test_cutoff_inclusive_and_future_excluded(self):
        # Kills: an off-by-one window boundary, or counting future-dated events.
        ev = [self._v("0xB", "5.00", "2026-07-08T04:00:00Z"),   # exactly now-window -> included
              self._v("0xB", "5.00", "2026-07-08T05:30:00Z")]   # future -> excluded
        r = L.counterparty_flow(ev, "0xB", "2026-07-08T05:00:00Z", 3600)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["total_amount"], Decimal("5.00"))

    def test_non_dict_event_does_not_crash(self):
        # Regression (AUDIT-FOUND): read_events() yields anything json.loads()
        # returns, including a valid-JSON line that is NOT an object (e.g. a bare
        # `42`, `"str"`, `null`, `[...]`). e.get() on such a value raised
        # AttributeError, violating the "never raises" contract and (via forecast's
        # try/except) SILENTLY disabling the velocity signal for the whole verdict
        # once a single such row exists in the ledger. Non-dict events must be
        # skipped, not crash.
        ev = [self._v("0xBAD", "5.00", "2026-07-08T04:20:00Z"),
              42, "junk", None, ["not", "a", "dict"],
              self._v("0xBAD", "5.00", "2026-07-08T04:21:00Z")]
        r = L.counterparty_flow(ev, "0xBAD", "2026-07-08T04:25:00Z", 3600)
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["total_amount"], Decimal("10.00"))

    def test_empty_counterparty_does_not_sum_fieldless_events(self):
        # Kills: removing the empty-target guard so an empty/absent counterparty
        # query matches events whose counterparty field is empty/missing
        # ("" == "") and sums the WHOLE ledger. An absent counterparty must match
        # NOTHING, never the aggregate ledger flow.
        ev = [{"kind": "verdict", "amount": "50.00",
               "ts": "2026-07-08T04:00:00Z"},                    # no counterparty
              {"kind": "verdict", "counterparty": "",
               "amount": "70.00", "ts": "2026-07-08T04:10:00Z"}]  # empty counterparty
        r = L.counterparty_flow(ev, "", "2026-07-08T04:25:00Z", 3600)
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["total_amount"], Decimal(0))
        r2 = L.counterparty_flow(ev, None, "2026-07-08T04:25:00Z", 3600)
        self.assertEqual(r2["count"], 0)


class TestPayerFlow(unittest.TestCase):
    """payer_flow: SYBIL complement -- ONE payer's total outflow + fan-out (distinct
    counterparties) in a window. Splitting a drain across fresh addresses raises the
    distinct count, which is the tell."""

    def _v(self, payer, cp, amount, ts):
        return {"kind": "verdict", "payer": payer, "counterparty": cp,
                "amount": str(amount), "ts": ts}

    def test_sums_outflow_across_different_counterparties(self):
        # THE POINT: a distributed drain -- one payer, many DIFFERENT payees. A
        # per-counterparty view sees count 1 each; the payer view sees the whole.
        ev = [self._v("0xAGENT", "0xa", "5.00", "2026-07-08T04:00:00Z"),
              self._v("0xAGENT", "0xb", "5.00", "2026-07-08T04:10:00Z"),
              self._v("0xAGENT", "0xc", "5.00", "2026-07-08T04:20:00Z")]
        r = L.payer_flow(ev, "0xAGENT", "2026-07-08T04:25:00Z", 3600)
        # Kills: keying the sum on counterparty instead of payer (would split to 5 each).
        self.assertEqual(r["total_amount"], Decimal("15.00"))
        self.assertEqual(r["count"], 3)
        # Kills: not counting fan-out -- the distributed-drain signature.
        self.assertEqual(r["distinct_counterparties"], 3)

    def test_distinct_counts_unique_payees_not_payments(self):
        # Many payments to the SAME payee = high count but LOW fan-out (that's the
        # per-counterparty/velocity case, not a sybil split).
        ev = [self._v("0xAGENT", "0xa", "5.00", "2026-07-08T04:00:00Z"),
              self._v("0xAGENT", "0xa", "5.00", "2026-07-08T04:10:00Z"),
              self._v("0xAGENT", "0xa", "5.00", "2026-07-08T04:20:00Z")]
        r = L.payer_flow(ev, "0xAGENT", "2026-07-08T04:25:00Z", 3600)
        self.assertEqual(r["count"], 3)
        self.assertEqual(r["distinct_counterparties"], 1)

    def test_distinct_is_case_insensitive_on_counterparty(self):
        # Kills: exact-case distinct -> an attacker inflates fan-out (or dodges the
        # velocity aggregate) by varying payee address case. Same address, one payee.
        ev = [self._v("0xAGENT", "0xAbCd", "5.00", "2026-07-08T04:00:00Z"),
              self._v("0xAGENT", "0xabcd", "5.00", "2026-07-08T04:10:00Z")]
        r = L.payer_flow(ev, "0xAGENT", "2026-07-08T04:25:00Z", 3600)
        self.assertEqual(r["distinct_counterparties"], 1)

    def test_excludes_other_payers_and_out_of_window(self):
        ev = [self._v("0xAGENT", "0xa", "5.00", "2026-07-08T01:00:00Z"),   # >1h old
              self._v("0xOTHER", "0xb", "5.00", "2026-07-08T04:20:00Z"),    # other payer
              self._v("0xAGENT", "0xc", "5.00", "2026-07-08T04:21:00Z")]
        r = L.payer_flow(ev, "0xAGENT", "2026-07-08T04:25:00Z", 3600)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["total_amount"], Decimal("5.00"))
        self.assertEqual(r["distinct_counterparties"], 1)

    def test_payer_match_is_case_insensitive(self):
        ev = [self._v("0xAgEnT", "0xa", "5.00", "2026-07-08T04:20:00Z")]
        self.assertEqual(
            L.payer_flow(ev, "0xagent", "2026-07-08T04:25:00Z", 3600)["count"], 1)

    def test_skips_non_verdict_garbage_and_non_dict(self):
        # Kills: counting outcomes / garbage amounts as outflow, or crashing on a
        # non-dict ledger row (same class as the audit-found counterparty_flow bug).
        ev = [{"kind": "outcome", "payer": "0xAGENT", "counterparty": "0xz",
               "amount": "99", "ts": "2026-07-08T04:20:00Z"},
              self._v("0xAGENT", "0xa", "notanumber", "2026-07-08T04:21:00Z"),
              self._v("0xAGENT", "0xb", "0", "2026-07-08T04:22:00Z"),
              42, "junk", None, ["x"],
              self._v("0xAGENT", "0xc", "5.00", "2026-07-08T04:23:00Z")]
        r = L.payer_flow(ev, "0xAGENT", "2026-07-08T04:25:00Z", 3600)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["total_amount"], Decimal("5.00"))
        self.assertEqual(r["distinct_counterparties"], 1)

    def test_naive_timestamp_does_not_crash(self):
        ev = [self._v("0xAGENT", "0xa", "5.00", "2026-07-08T04:20:00")]  # no Z
        r = L.payer_flow(ev, "0xAGENT", "2026-07-08T04:25:00Z", 3600)
        self.assertEqual(r["count"], 1)

    def test_empty_payer_matches_nothing(self):
        # A verdict with no payer must NOT let an empty query sum the whole ledger.
        ev = [self._v("0xAGENT", "0xa", "5.00", "2026-07-08T04:20:00Z")]
        self.assertEqual(L.payer_flow(ev, "", "2026-07-08T04:25:00Z", 3600)["count"], 0)
        self.assertEqual(L.payer_flow(ev, None, "2026-07-08T04:25:00Z", 3600)["count"], 0)

    def test_missing_payer_field_not_matched_by_empty(self):
        # Events with NO payer field must not be swept up when querying a real payer.
        ev = [{"kind": "verdict", "counterparty": "0xa", "amount": "5.00",
               "ts": "2026-07-08T04:20:00Z"}]  # no payer key
        self.assertEqual(
            L.payer_flow(ev, "0xAGENT", "2026-07-08T04:25:00Z", 3600)["count"], 0)

    def test_empty_payer_does_not_sum_payerless_events(self):
        # Kills: removing the empty-target guard so an empty query no longer
        # short-circuits, then matches events whose payer field is empty/missing
        # ("" == "") and sums the WHOLE ledger. The prior empty-payer tests use
        # events that HAVE a payer, so they pass even with the guard gone -- this
        # one exercises the actual danger: absent payer must sum to zero, not all.
        ev = [{"kind": "verdict", "counterparty": "0xa", "amount": "50.00",
               "ts": "2026-07-08T04:00:00Z"},                       # no payer key
              {"kind": "verdict", "payer": "", "counterparty": "0xb",
               "amount": "70.00", "ts": "2026-07-08T04:10:00Z"}]    # empty payer
        r = L.payer_flow(ev, "", "2026-07-08T04:25:00Z", 3600)
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["total_amount"], Decimal(0))
        self.assertEqual(r["distinct_counterparties"], 0)


if __name__ == "__main__":
    unittest.main()
