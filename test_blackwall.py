"""
Unit tests for blackwall pure decision-boundary functions + the forecast path.

Run: python -m unittest test_blackwall.py -v

The decision functions (parse_amount, price_anomaly_ratio, anomaly_score,
reputation_score, decide_payment) ARE the boundary; tests are TDD-first. Each
class notes the mutation it kills.
"""
import json
import threading
import unittest
from decimal import Decimal

import blackwall as bw

try:
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    _HAVE_ETH_ACCOUNT = True
except Exception:  # pragma: no cover - optional cross-check dependency
    _HAVE_ETH_ACCOUNT = False


class TestParseAmount(unittest.TestCase):
    """
    parse_amount: positive Decimal | None.

    Mutation notes:
      - Drop the `<= 0` check        -> zero / negative tests FAIL.
      - Parse via float              -> the float-rejection + exactness tests FAIL.
      - Drop the finite check        -> NaN/Inf tests FAIL.
    """

    def test_valid_decimal_string(self):
        self.assertEqual(bw.parse_amount("0.09"), Decimal("0.09"))

    def test_exactness_preserved(self):
        # Decimal, not float: "0.10" stays exact.
        self.assertEqual(bw.parse_amount("0.10"), Decimal("0.10"))

    def test_integer_ok(self):
        self.assertEqual(bw.parse_amount(5), Decimal("5"))

    def test_reject_none(self):
        self.assertIsNone(bw.parse_amount(None))

    def test_reject_empty(self):
        self.assertIsNone(bw.parse_amount(""))

    def test_reject_zero(self):
        self.assertIsNone(bw.parse_amount("0"))

    def test_reject_negative(self):
        self.assertIsNone(bw.parse_amount("-0.01"))

    def test_reject_nonnumeric(self):
        self.assertIsNone(bw.parse_amount("free"))

    def test_reject_float_type(self):
        # A bare float is rejected (binary rounding); callers must send a string.
        self.assertIsNone(bw.parse_amount(0.09))

    def test_reject_nan_inf(self):
        self.assertIsNone(bw.parse_amount("NaN"))
        self.assertIsNone(bw.parse_amount("Infinity"))

    def test_reject_underscore_and_scientific(self):
        # Decimal accepts these but they're surprising for a money field.
        self.assertIsNone(bw.parse_amount("1_000"))
        self.assertIsNone(bw.parse_amount("1e3"))
        self.assertIsNone(bw.parse_amount("+5"))


class TestPriceAnomalyRatio(unittest.TestCase):
    """
    price_anomaly_ratio: amount / median(history), or None with no history.

    Mutation notes:
      - Return 0.0 instead of None on empty history -> the no-history test FAILS
        (UNKNOWN must stay distinct from "fine").
      - Use mean instead of median -> the outlier-robustness test FAILS.
    """

    def test_no_history_is_none(self):
        self.assertIsNone(bw.price_anomaly_ratio("0.09", []))

    def test_at_median(self):
        self.assertAlmostEqual(
            bw.price_anomaly_ratio("0.09", ["0.09", "0.09", "0.09"]), 1.0)

    def test_overcharge_ratio(self):
        self.assertAlmostEqual(
            bw.price_anomaly_ratio("0.72", ["0.09", "0.09", "0.09"]), 8.0)

    def test_median_is_outlier_robust(self):
        # One huge outlier must not drag the baseline up (mean would).
        ratio = bw.price_anomaly_ratio("0.09", ["0.09", "0.09", "100.0"])
        self.assertAlmostEqual(ratio, 1.0)


class TestAnomalyScore(unittest.TestCase):
    """
    anomaly_score: ratio -> 0..1, only overcharge counts.

    Mutation notes:
      - Score cheaper-than-median > 0 -> the discount test FAILS.
      - Drop the min(1.0, ...) clamp  -> the cap test FAILS.
    """

    def test_none_is_zero(self):
        self.assertEqual(bw.anomaly_score(None), 0.0)

    def test_at_or_below_median_is_zero(self):
        self.assertEqual(bw.anomaly_score(1.0), 0.0)
        self.assertEqual(bw.anomaly_score(0.5), 0.0)  # a discount is not anomalous

    def test_full_at_stop_ratio(self):
        self.assertEqual(bw.anomaly_score(bw.STOP_ANOMALY_RATIO), 1.0)

    def test_clamped_above_stop_ratio(self):
        self.assertEqual(bw.anomaly_score(100.0), 1.0)

    def test_crosses_hold_ceiling(self):
        # ~3.1x median should already be anomalous enough to block an auto-GO.
        self.assertGreaterEqual(bw.anomaly_score(3.1), bw.HOLD_ANOMALY)


class TestReputationScore(unittest.TestCase):
    """
    reputation_score: Beta(good+1, bad+1) posterior mean in 0..1.

    Mutation notes:
      - Score a no-history wallet 1.0 -> the prior test FAILS ("no evidence" != trust).
      - Ignore dispute_rate           -> the disputed-history test FAILS.
    """

    def test_unknown_wallet_is_prior_half(self):
        self.assertAlmostEqual(bw.reputation_score({"settlement_count": 0}), 0.5)

    def test_clean_long_history_is_high(self):
        rep = bw.reputation_score({"settlement_count": 1240, "dispute_rate": 0.002})
        self.assertGreater(rep, 0.9)

    def test_disputed_history_is_low(self):
        rep = bw.reputation_score({"settlement_count": 200, "dispute_rate": 0.6})
        self.assertLess(rep, 0.5)


class TestDecidePayment(unittest.TestCase):
    """
    decide_payment: the GO / HOLD / STOP core.

    Mutation notes:
      - Drop the sanctioned check       -> test_stop_sanctioned FAILS.
      - Drop the recipient-mismatch STOP -> test_stop_recipient_mismatch FAILS.
      - Drop the thin-history gate       -> test_hold_thin_history FAILS (would GO).
      - Drop the amount-threshold gate   -> test_hold_over_budget FAILS (would GO).
      - Drop the >= STOP_ANOMALY_RATIO   -> test_stop_price_wildly_off FAILS (HOLD).
    """

    GOOD = {"settlement_count": 1240, "dispute_rate": 0.002}
    STABLE_HISTORY = ["0.09", "0.09", "0.088", "0.092"]

    def test_go_reputable_in_budget_normal_price(self):
        v = bw.decide_payment("0.09", self.GOOD, self.STABLE_HISTORY,
                              counterparty="0xA")
        self.assertEqual(v["verdict"], "GO")
        self.assertGreater(v["score"], 0.8)
        self.assertEqual(v["signals"]["reversibility"], "irreversible")

    def test_stop_sanctioned(self):
        rec = dict(self.GOOD, sanctioned=True)
        v = bw.decide_payment("0.09", rec, self.STABLE_HISTORY, counterparty="0xA")
        self.assertEqual(v["verdict"], "STOP")
        self.assertEqual(v["score"], 0.0)  # hard STOP floors trust

    def test_stop_known_bad(self):
        rec = dict(self.GOOD, known_bad=True)
        v = bw.decide_payment("0.09", rec, self.STABLE_HISTORY, counterparty="0xA")
        self.assertEqual(v["verdict"], "STOP")

    def test_stop_recipient_mismatch(self):
        v = bw.decide_payment("0.09", self.GOOD, self.STABLE_HISTORY,
                              counterparty="0xATTACKER",
                              expected_recipient="0xLEGIT")
        self.assertEqual(v["verdict"], "STOP")
        self.assertEqual(v["score"], 0.0)

    def test_recipient_match_is_case_insensitive(self):
        # Regression: a checksummed counterparty and the lowercase 402 recipient
        # are the SAME address -> NOT a mismatch -> must not STOP.
        checksummed = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        v = bw.decide_payment("0.09", self.GOOD, self.STABLE_HISTORY,
                              counterparty=checksummed,
                              expected_recipient=checksummed.lower())
        self.assertNotEqual(v["verdict"], "STOP")

    def test_stop_price_wildly_off(self):
        # 8x the counterparty's OWN median -> STOP. STABLE_HISTORY is DENSE (4
        # obs) and TIGHT (all ~0.09), so even the flat median is trustworthy
        # enough (flat_stop_trustworthy) to hard-STOP. Mutation: drop the
        # `>= STOP_ANOMALY_RATIO` -> HOLD (not STOP).
        v = bw.decide_payment("0.72", self.GOOD, self.STABLE_HISTORY,
                              counterparty="0xA")
        self.assertEqual(v["verdict"], "STOP")

    def test_flat_anomaly_untrustworthy_holds_not_stops(self):
        # REGRESSION (live false-STOP): an 8x+ anomaly off an UNTRUSTWORTHY flat
        # median must NOT hard-STOP -- it downgrades to HOLD (fails the GO gate,
        # routed to a human). This is the "62x its own median" false STOP on
        # active wallets (exchange addresses, vitalik.eth) whose sparse/dispersed
        # on-chain receipts make the flat median a meaningless reference. Two ways
        # a flat median is untrustworthy; BOTH must HOLD, not STOP.
        # (a) TOO SPARSE (< FLAT_STOP_MIN_OBS observations):
        thin = bw.decide_payment("0.72", self.GOOD, ["0.09", "0.09"],
                                 counterparty="0xA")
        self.assertEqual(thin["signals"]["price_basis"], "flat")
        self.assertEqual(thin["verdict"], "HOLD")
        # (b) TOO DISPERSED (max/min > FLAT_STOP_SPREAD -- a wallet's varied receipts):
        noisy = bw.decide_payment("8.20", self.GOOD, ["0.01", "0.09", "1.0"],
                                  counterparty="0xA")
        self.assertEqual(noisy["signals"]["price_basis"], "flat")
        self.assertEqual(noisy["verdict"], "HOLD")
        # Mutation: drop the flat_stop_trustworthy guard -> both become STOP.

    def test_hold_thin_history(self):
        thin = {"settlement_count": 3, "dispute_rate": 0.0}
        v = bw.decide_payment("0.09", thin, ["0.09", "0.09"], counterparty="0xA")
        self.assertEqual(v["verdict"], "HOLD")

    def test_captive_sybil_blocks_go_but_never_stops(self):
        # Reputable, normal price -> would GO. A captive-Sybil payer-graph signal
        # (clears the distinct gate but all payers captive) escalates to HOLD.
        # Mutation: drop `not captive_sybil` from the go gate -> this GOes.
        sig = {"captive_sybil": True, "distinct_payers": 4,
               "established_payers": 0, "captive_ratio": 1.0, "cross_score": 0.0}
        v = bw.decide_payment("0.09", self.GOOD, self.STABLE_HISTORY,
                              counterparty="0xA", payer_graph_signal=sig)
        self.assertEqual(v["verdict"], "HOLD")             # never STOP -- conservative
        self.assertTrue(any("captive" in r for r in v["reasons"]))
        self.assertEqual(v["signals"]["cross_counterparty"]["established_payers"], 0)

    def test_sybil_ring_gates_hold(self):
        # Stage 3 (docs/DATA_COMPLETENESS.md): sybil_ring now GATES (HOLD) -- coverage
        # convergence proven (coverage_eval.py). A closed cluster where NOT ONE payer
        # pays a trusted anchor escalates. Mutation: drop sybil_ring from graph_sybil
        # -> this GOes (the old advisory behavior) and the assert FAILS.
        sig = {"captive_sybil": False, "sybil_ring": True, "distinct_payers": 6,
               "established_payers": 5, "captive_ratio": 0.167,
               "reputable_payers": 0, "avg_payer_reputation": 0.0}
        v = bw.decide_payment("0.09", self.GOOD, self.STABLE_HISTORY,
                              counterparty="0xA", payer_graph_signal=sig)
        self.assertEqual(v["verdict"], "HOLD")                     # now blocked
        self.assertFalse(v["hard_stop"])                           # HOLD-only, never STOP
        self.assertTrue(any("anchor" in r and "escalating" in r for r in v["reasons"]))
        self.assertTrue(v["signals"]["cross_counterparty"]["sybil_ring"])
        self.assertTrue(v["signals"]["cross_counterparty"]["sybil_ring_gated"])

    def test_sybil_ring_is_hold_only_never_stop(self):
        # BOUNDARY: sybil_ring can turn GO->HOLD but must NEVER cause a STOP or set
        # hard_stop, and must NEVER downgrade a real STOP. Mutation: route sybil_ring
        # into the stop/hard_stop path -> these asserts FAIL.
        ring = {"captive_sybil": False, "sybil_ring": True, "distinct_payers": 6,
                "established_payers": 5, "reputable_payers": 0}
        # alone -> HOLD, not STOP
        v = bw.decide_payment("0.09", self.GOOD, self.STABLE_HISTORY,
                              counterparty="0xA", payer_graph_signal=ring)
        self.assertEqual(v["verdict"], "HOLD")
        self.assertFalse(v["hard_stop"])
        # with a real STOP (sanctioned) -> still STOP + hard_stop (ring doesn't clear it)
        v2 = bw.decide_payment("0.09", dict(self.GOOD, sanctioned=True),
                               self.STABLE_HISTORY, counterparty="0xA",
                               payer_graph_signal=ring)
        self.assertEqual(v2["verdict"], "STOP")
        self.assertTrue(v2["hard_stop"])

    def test_sybil_ring_reversibility_flag(self):
        # the SYBIL_RING_GATES lock demotes to advisory without a logic change.
        sig = {"captive_sybil": False, "sybil_ring": True, "distinct_payers": 6,
               "established_payers": 5, "reputable_payers": 0}
        orig = bw.SYBIL_RING_GATES
        try:
            bw.SYBIL_RING_GATES = False
            v = bw.decide_payment("0.09", self.GOOD, self.STABLE_HISTORY,
                                  counterparty="0xA", payer_graph_signal=sig)
            self.assertEqual(v["verdict"], "GO")                   # demoted -> advisory
            self.assertFalse(v["signals"]["cross_counterparty"]["sybil_ring_gated"])
            self.assertTrue(any("advisory" in r for r in v["reasons"]))
        finally:
            bw.SYBIL_RING_GATES = orig

    def test_healthy_graph_signal_surfaced_and_stays_go(self):
        sig = {"captive_sybil": False, "distinct_payers": 40,
               "established_payers": 25, "captive_ratio": 0.375, "cross_score": 0.625}
        v = bw.decide_payment("0.09", self.GOOD, self.STABLE_HISTORY,
                              counterparty="0xA", payer_graph_signal=sig)
        self.assertEqual(v["verdict"], "GO")               # corroboration doesn't block
        self.assertEqual(v["signals"]["cross_counterparty"]["established_payers"], 25)

    def test_no_graph_signal_is_fail_open(self):
        v = bw.decide_payment("0.09", self.GOOD, self.STABLE_HISTORY,
                              counterparty="0xA")            # no signal
        self.assertEqual(v["verdict"], "GO")
        self.assertIsNone(v["signals"]["cross_counterparty"])

    def test_stale_blocks_go_but_never_stops(self):
        # a dormant/abandoned endpoint escalates to HOLD. Mutation: dropping `stale`
        # from the go gate -> a payee with no recent settlement stays GO.
        sig = {"stale": True, "recency_days": 200, "age_days": 500,
               "peak_day_share": 0.2, "burst_sybil": False}
        v = bw.decide_payment("0.09", self.GOOD, self.STABLE_HISTORY,
                              counterparty="0xA", temporal_signal=sig)
        self.assertEqual(v["verdict"], "HOLD")
        self.assertTrue(any("dormant" in r or "day(s)" in r for r in v["reasons"]))
        self.assertTrue(v["signals"]["temporal"]["stale"])

    def test_burst_is_diagnostic_never_gates(self):
        # burst_sybil is backfill-windowed and must NOT block GO -- only surfaced.
        # Mutation: gating on burst_sybil -> this HOLDs a reputable payee.
        sig = {"stale": False, "recency_days": 1, "age_days": 0.9,
               "peak_day_share": 0.95, "burst_sybil": True}
        v = bw.decide_payment("0.09", self.GOOD, self.STABLE_HISTORY,
                              counterparty="0xA", temporal_signal=sig)
        self.assertEqual(v["verdict"], "GO")                       # not blocked
        self.assertTrue(v["signals"]["temporal"]["burst_sybil_advisory"])

    def test_no_temporal_signal_is_fail_open(self):
        v = bw.decide_payment("0.09", self.GOOD, self.STABLE_HISTORY, counterparty="0xA")
        self.assertEqual(v["verdict"], "GO")
        self.assertIsNone(v["signals"]["temporal"])

    def test_going_bad_holds_despite_clean_lifetime(self):
        # 1000 clean settlements (lifetime 0.4%) but 40% of the LAST 10 disputed ->
        # trending bad. reputation_score (volume-averaged) still says GO; the recency
        # gate escalates to HOLD. Mutation: dropping `going_bad` -> this GOes.
        rec = {"settlement_count": 1000, "dispute_rate": 0.004,
               "recent_dispute_rate": 0.4, "recent_outcomes": 10}
        v = bw.decide_payment("0.09", rec, self.STABLE_HISTORY, counterparty="0xA")
        self.assertEqual(v["verdict"], "HOLD")
        self.assertTrue(any("trending bad" in r for r in v["reasons"]))
        self.assertEqual(v["signals"]["recent_dispute_rate"], 0.4)

    def test_low_recent_disputes_still_go(self):
        rec = {"settlement_count": 1000, "dispute_rate": 0.004,
               "recent_dispute_rate": 0.1, "recent_outcomes": 10}
        self.assertEqual(bw.decide_payment("0.09", rec, self.STABLE_HISTORY,
                                           counterparty="0xA")["verdict"], "GO")

    def test_going_bad_needs_enough_recent_outcomes(self):
        # 1 of 2 recent disputed = 50%, but too few outcomes to call a trend -> GO.
        rec = {"settlement_count": 1000, "dispute_rate": 0.004,
               "recent_dispute_rate": 0.5, "recent_outcomes": 2}
        self.assertEqual(bw.decide_payment("0.09", rec, self.STABLE_HISTORY,
                                           counterparty="0xA")["verdict"], "GO")

    def test_hold_over_budget(self):
        # Reputable + normal price, but amount above the auto-approve threshold.
        v = bw.decide_payment("25.00", self.GOOD,
                              ["25.0", "25.0", "24.0"], counterparty="0xA")
        self.assertEqual(v["verdict"], "HOLD")
        self.assertEqual(v["signals"]["blast_radius"], "unbounded")

    def test_hold_anomalous_but_not_stop(self):
        # ~4x median: above the HOLD ceiling, below the STOP ratio.
        v = bw.decide_payment("0.36", self.GOOD, self.STABLE_HISTORY,
                              counterparty="0xA")
        self.assertEqual(v["verdict"], "HOLD")

    def test_reputable_no_price_history_goes_but_flags_unknown(self):
        # DELIBERATE tradeoff (see BLACKWALL.md known-limitations): a reputable,
        # in-budget counterparty with NO price history still GOes -- the anomaly
        # is UNKNOWN (treated as 0), surfaced as a reason, NOT silently a HOLD.
        # The budget gate bounds the exposure (large + no-history -> HOLD; see
        # test_no_history_large_amount_holds).
        v = bw.decide_payment("0.09", self.GOOD, [], counterparty="0xA")
        self.assertEqual(v["verdict"], "GO")
        self.assertTrue(any("price anomaly unknown" in r for r in v["reasons"]))

    def test_no_history_large_amount_holds(self):
        # The budget gate is what bounds the no-price-history GO path.
        v = bw.decide_payment("50.00", self.GOOD, [], counterparty="0xA")
        self.assertEqual(v["verdict"], "HOLD")


class TestSignReceipt(unittest.TestCase):
    """sign_receipt: deterministic, prefixed, key-dependent."""

    def test_deterministic_and_prefixed(self):
        obj = {"verdict": "GO", "score": 0.9}
        r1 = bw.sign_receipt(obj, key=b"k")
        r2 = bw.sign_receipt(obj, key=b"k")
        self.assertEqual(r1, r2)
        self.assertTrue(r1.startswith("bw_"))

    def test_key_changes_receipt(self):
        obj = {"verdict": "GO"}
        self.assertNotEqual(bw.sign_receipt(obj, key=b"k1"),
                            bw.sign_receipt(obj, key=b"k2"))


class TestConfirmedSettlementGate(unittest.TestCase):
    """
    decide_payment's thin-history gate counts CONFIRMED settlements only.

    Mutation note: gate on settlement_count instead of confirmed_settlement_count
    -> test_self_reported_inflation_still_holds FAILS (would GO).
    """

    def test_self_reported_inflation_still_holds(self):
        # 1000 reported settlements but 0 confirmed -> thin -> HOLD.
        rec = {"settlement_count": 1000, "confirmed_settlement_count": 0,
               "dispute_rate": 0.0}
        v = bw.decide_payment("0.09", rec, ["0.09"] * 5, counterparty="0xA")
        self.assertEqual(v["verdict"], "HOLD")
        self.assertTrue(any("chain-confirmed" in r for r in v["reasons"]))

    def test_confirmed_settlements_go(self):
        rec = {"settlement_count": 1000, "confirmed_settlement_count": 30,
               "dispute_rate": 0.0}
        v = bw.decide_payment("0.09", rec, ["0.09"] * 5, counterparty="0xA")
        self.assertEqual(v["verdict"], "GO")

    def test_absent_field_vouches_all(self):
        # On-chain/seed sources omit the field -> count all (backward compatible).
        rec = {"settlement_count": 30, "dispute_rate": 0.0}
        v = bw.decide_payment("0.09", rec, ["0.09"] * 5, counterparty="0xA")
        self.assertEqual(v["verdict"], "GO")


class TestSybilDiversityGate(unittest.TestCase):
    """GO requires confirmed settlements from >= MIN_DISTINCT_PAYERS payers."""

    BASE = {"settlement_count": 50, "confirmed_settlement_count": 50,
            "dispute_rate": 0.0}

    def _v(self, distinct):
        rec = dict(self.BASE)
        if distinct is not None:
            rec["distinct_payers"] = distinct
        return bw.decide_payment("0.09", rec, ["0.09"] * 5, counterparty="0xA")

    def test_few_payers_holds(self):
        v = self._v(1)  # wash-trade shape
        self.assertEqual(v["verdict"], "HOLD")
        self.assertTrue(any("distinct payer" in r for r in v["reasons"]))

    def test_at_threshold_goes(self):
        self.assertEqual(self._v(bw.MIN_DISTINCT_PAYERS)["verdict"], "GO")

    def test_below_threshold_holds(self):
        self.assertEqual(self._v(bw.MIN_DISTINCT_PAYERS - 1)["verdict"], "HOLD")

    def test_absent_vouches_all(self):
        # seed/mock sources omit it -> not gated (backward compatible).
        self.assertEqual(self._v(None)["verdict"], "GO")


class TestReportToken(unittest.TestCase):
    """sign/verify_report_token: capability authorizing an outcome report."""

    def test_roundtrip(self):
        t = bw.sign_report_token("bw_abc", key=b"k")
        self.assertTrue(bw.verify_report_token("bw_abc", t, key=b"k"))

    def test_wrong_receipt_rejected(self):
        t = bw.sign_report_token("bw_abc", key=b"k")
        self.assertFalse(bw.verify_report_token("bw_other", t, key=b"k"))

    def test_missing_or_garbage_rejected(self):
        self.assertFalse(bw.verify_report_token("bw_abc", None, key=b"k"))
        self.assertFalse(bw.verify_report_token("bw_abc", "deadbeef", key=b"k"))

    def test_wrong_key_rejected(self):
        t = bw.sign_report_token("bw_abc", key=b"k1")
        self.assertFalse(bw.verify_report_token("bw_abc", t, key=b"k2"))

    def test_not_the_receipt_id(self):
        # domain-separated: the token is not just the receipt id.
        self.assertNotEqual(bw.sign_report_token("bw_abc", key=b"k"), "bw_abc")


class TestValidateRequest(unittest.TestCase):
    """validate_request: required fields + typed context."""

    BASE = {"counterparty": "0xA", "amount": "0.09", "asset": "USDC", "chain": "base"}

    def test_ok(self):
        clean, err = bw.validate_request(dict(self.BASE))
        self.assertIsNone(err)
        self.assertEqual(clean["amount"], Decimal("0.09"))

    def test_missing_field(self):
        bad = dict(self.BASE)
        del bad["asset"]
        clean, err = bw.validate_request(bad)
        self.assertIsNone(clean)
        self.assertIn("asset", err)

    def test_bad_amount(self):
        bad = dict(self.BASE, amount="-1")
        clean, err = bw.validate_request(bad)
        self.assertIsNone(clean)

    def test_context_price_history_type(self):
        bad = dict(self.BASE, context={"quoted_price_history": "0.09"})
        clean, err = bw.validate_request(bad)
        self.assertIsNone(clean)

    def test_payer_optional(self):
        clean, err = bw.validate_request(dict(self.BASE))
        self.assertIsNone(err)
        self.assertIsNone(clean["payer"])

    def test_payer_valid_normalized(self):
        addr = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        clean, err = bw.validate_request(dict(self.BASE, payer=addr))
        self.assertIsNone(err)
        self.assertEqual(clean["payer"], addr.lower())  # normalized

    def test_payer_malformed_rejected(self):
        clean, err = bw.validate_request(dict(self.BASE, payer="0xNOPE"))
        self.assertIsNone(clean)
        self.assertIn("payer", err)

    def test_counterparty_address_canonicalized(self):
        # A mixed-case EVM-address counterparty is lowercased so reputation
        # doesn't split across spellings of one address.
        addr = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        clean, err = bw.validate_request(dict(self.BASE, counterparty=addr))
        self.assertIsNone(err)
        self.assertEqual(clean["counterparty"], addr.lower())


class TestForecastPayloadSimulation(unittest.TestCase):
    """
    forecast() with payload simulation (Phase 1): the agent's ACTUAL signed
    payment is cross-checked against the claim. A mismatch is a hard STOP.

    Mutation notes:
      - don't thread payload_mismatch_reasons into decide_payment -> the swap
        tests below stop STOPping.
      - treat the X-PAYMENT transport header (Blackwall's fee) as the scored
        payment -> the recipient swap would false-positive; here it comes from the
        BODY field, so these stay correct.
    """
    GOOD = "0xKNOWNGOOD000000000000000000000000000001"
    USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    def setUp(self):
        self.src = bw.MockReputationSource()

    def _xpayment(self, *, to, value, asset=None, network="base", signature="0x" + "2" * 130):
        payload = {"authorization": {"from": "0x" + "b" * 40, "to": to,
                                     "value": value, "validAfter": "0",
                                     "validBefore": "9999999999",
                                     "nonce": "0x" + "1" * 64}}
        if signature is not None:
            payload["signature"] = signature
        payment = {"x402Version": 2,
                   "accepted": {"scheme": "exact", "network": network,
                                "asset": asset or self.USDC},
                   "payload": payload}
        return bw._b64_header(payment)

    def test_matching_payment_stays_go(self):
        # baseline: a known-good counterparty is GO with NO payment attached
        base = {"counterparty": self.GOOD, "amount": "0.09",
                "asset": self.USDC, "chain": "base"}
        self.assertEqual(bw.forecast(dict(base), self.src)[0]["verdict"], "GO")
        # a MATCHING payment (0.09 USDC -> GOOD) with recipient+amount right stays GO.
        # No signature attached -> Phase 2 degrades to an advisory warning, not a stop.
        payload = dict(base, payment_authorization=self._xpayment(
            to=self.GOOD, value="90000", signature=None))
        resp, err = bw.forecast(payload, self.src)
        self.assertIsNone(err)
        self.assertEqual(resp["verdict"], "GO")
        self.assertFalse(resp["hard_stop"])

    def test_recipient_swap_hard_stops(self):
        # score "pay GOOD" but the signed auth actually pays a DIFFERENT address
        payload = {"counterparty": self.GOOD, "amount": "0.09",
                   "asset": self.USDC, "chain": "base",
                   "payment_authorization": self._xpayment(
                       to="0x" + "d" * 40, value="90000")}
        resp, err = bw.forecast(payload, self.src)
        self.assertEqual(resp["verdict"], "STOP")
        self.assertTrue(resp["hard_stop"])
        self.assertTrue(any("recipient mismatch" in r for r in resp["reasons"]))

    def test_amount_inflation_hard_stops(self):
        # score $0.09 but the signed auth moves $5.00 (5_000_000 atomic)
        payload = {"counterparty": self.GOOD, "amount": "0.09",
                   "asset": self.USDC, "chain": "base",
                   "payment_authorization": self._xpayment(
                       to=self.GOOD, value="5000000")}
        resp, _ = bw.forecast(payload, self.src)
        self.assertEqual(resp["verdict"], "STOP")
        self.assertTrue(resp["hard_stop"])
        self.assertTrue(any("value" in r for r in resp["reasons"]))

    def test_no_payment_is_unaffected(self):
        # the check is opt-in: no payment_authorization -> normal verdict, no stop
        payload = {"counterparty": self.GOOD, "amount": "0.09",
                   "asset": self.USDC, "chain": "base"}
        resp, _ = bw.forecast(payload, self.src)
        self.assertEqual(resp["verdict"], "GO")

    def test_phase2_bad_signer_hard_stops_through_forecast(self):
        # Phase 1 fields all match (to==counterparty, value, asset, chain), but the
        # signature was made by a different key than `from` -> Phase 2 recovers a
        # signer != from -> hard STOP end-to-end through forecast().
        import secp256k1 as S
        import eip712 as E
        payer = E.pubkey_to_address(S.privkey_to_pub(0xBEEF))   # stated from
        cp = "0x" + "a" * 40
        message = {"from": payer, "to": cp, "value": "90000", "validAfter": "0",
                   "validBefore": "9999999999", "nonce": "0x" + "1" * 64}
        z = E.transfer_authorization_digest(
            {"name": "USD Coin", "version": "2", "chainId": 8453,
             "verifyingContract": self.USDC}, message)
        r, s, rec = S.ecdsa_sign(z, 0xA11CE)                    # signed by a DIFFERENT key
        sig = "0x" + (r.to_bytes(32, "big") + s.to_bytes(32, "big")
                      + bytes([27 + rec])).hex()
        payment = {"x402Version": 2,
                   "accepted": {"scheme": "exact", "network": "base", "asset": self.USDC},
                   "payload": {"signature": sig, "authorization": message}}
        payload = {"counterparty": cp, "amount": "0.09", "asset": self.USDC,
                   "chain": "base", "payment_authorization": bw._b64_header(payment)}
        resp, err = bw.forecast(payload, self.src)
        self.assertIsNone(err)
        self.assertEqual(resp["verdict"], "STOP")
        self.assertTrue(resp["hard_stop"])
        self.assertTrue(any("signer" in r for r in resp["reasons"]))

    def test_bad_payment_authorization_type_rejected(self):
        payload = {"counterparty": self.GOOD, "amount": "0.09",
                   "asset": self.USDC, "chain": "base",
                   "payment_authorization": {"not": "a string"}}
        resp, err = bw.forecast(payload, self.src)
        self.assertIsNone(resp)
        self.assertIn("payment_authorization", err)

    def test_phase3_unlimited_approval_hard_stops(self):
        # a "payment" that is actually an UNLIMITED token approval -> hard STOP
        spender = "0x" + "d" * 40
        data = "0x095ea7b3" + (bytes(12) + bytes.fromhex(spender[2:])).hex() \
            + ((1 << 256) - 1).to_bytes(32, "big").hex()
        payload = {"counterparty": self.GOOD, "amount": "0.09", "asset": self.USDC,
                   "chain": "base", "transaction": {"to": self.USDC, "data": data}}
        resp, err = bw.forecast(payload, self.src)
        self.assertIsNone(err)
        self.assertEqual(resp["verdict"], "STOP")
        self.assertTrue(resp["hard_stop"])
        self.assertTrue(any("UNLIMITED" in r for r in resp["reasons"]))

    def test_phase3_clean_transfer_stays_go(self):
        # a token transfer to the RIGHT counterparty is not flagged
        gh = "0x" + "a" * 40
        data = "0xa9059cbb" + (bytes(12) + bytes.fromhex(gh[2:])).hex() \
            + (90000).to_bytes(32, "big").hex()
        # use a known-good hex counterparty by registering it in the mock is hard;
        # instead assert the transaction alone adds no hard-stop reason.
        from calldata import screen_transaction
        r = screen_transaction({"counterparty": gh, "amount": "0.09"},
                               {"to": self.USDC, "data": data})
        self.assertTrue(r["matches"])

    def test_bad_transaction_type_rejected(self):
        payload = {"counterparty": self.GOOD, "amount": "0.09", "asset": self.USDC,
                   "chain": "base", "transaction": "not-an-object"}
        resp, err = bw.forecast(payload, self.src)
        self.assertIsNone(resp)
        self.assertIn("transaction", err)


@unittest.skipUnless(_HAVE_ETH_ACCOUNT, "eth-account not installed")
class TestForecastRealSignatureEndToEnd(unittest.TestCase):
    """LIVE end-to-end: a REAL eth-account signature through the full forecast()
    verdict path -- proves Phase 2 recovers real signatures without false-STOPs and
    catches a real foreign-signer payment. Skips when eth-account isn't installed."""
    USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    TYPES = {
        "EIP712Domain": [
            {"name": "name", "type": "string"}, {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"}],
        "TransferWithAuthorization": [
            {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]}

    def _payment(self, signer_acct, from_addr, to, value=90000):
        msg = {"from": signer_acct.address, "to": to, "value": value, "validAfter": 0,
               "validBefore": 9999999999, "nonce": bytes.fromhex("11" * 32)}
        signed = Account.sign_message(encode_typed_data(full_message={
            "types": self.TYPES,
            "domain": {"name": "USD Coin", "version": "2", "chainId": 8453,
                       "verifyingContract": self.USDC},
            "primaryType": "TransferWithAuthorization", "message": msg}),
            private_key=signer_acct.key)
        sig = "0x" + signed.r.to_bytes(32, "big").hex() + signed.s.to_bytes(32, "big").hex() \
            + bytes([signed.v]).hex()
        return bw._b64_header({
            "x402Version": 2,
            "accepted": {"scheme": "exact", "network": "base", "asset": self.USDC},
            "payload": {"signature": sig,
                        "authorization": {"from": from_addr, "to": to,
                                          "value": str(value), "validAfter": "0",
                                          "validBefore": "9999999999",
                                          "nonce": "0x" + "11" * 32}}})

    def test_real_matching_signature_no_hard_stop(self):
        acct = Account.create()
        to = "0x" + "a" * 40
        payload = {"counterparty": to, "amount": "0.09", "asset": self.USDC,
                   "chain": "base",
                   "payment_authorization": self._payment(acct, acct.address, to)}
        resp, err = bw.forecast(payload, bw.MockReputationSource())
        self.assertIsNone(err)
        self.assertFalse(resp["hard_stop"])          # real signature must not false-STOP
        self.assertFalse(any("signer" in r for r in resp["reasons"]))

    def test_real_foreign_signer_hard_stops(self):
        signer, stated = Account.create(), Account.create()
        to = "0x" + "a" * 40
        payload = {"counterparty": to, "amount": "0.09", "asset": self.USDC,
                   "chain": "base",
                   "payment_authorization": self._payment(signer, stated.address, to)}
        resp, err = bw.forecast(payload, bw.MockReputationSource())
        self.assertIsNone(err)
        self.assertEqual(resp["verdict"], "STOP")
        self.assertTrue(resp["hard_stop"])
        self.assertTrue(any("signer" in r for r in resp["reasons"]))


class TestSellerAuditTier(unittest.TestCase):
    """
    The verified-merchant badge through forecast(): it lifts the thin-history gate
    but is EARNED, BOUNDED, and can NEVER override a STOP.

    Mutation notes:
      - let the badge override sanctioned/gouge -> those tests FAIL.
      - waive the Sybil gate with the badge -> test_badge_does_not_waive_sybil FAILS.
      - not apply the floor -> test_badge_lifts_thin_to_go FAILS.
    """
    import seller_audit as _SA
    MERCHANT = "0x" + "c" * 40

    class _Src:
        def __init__(self, rec):
            self.rec = rec

        def lookup(self, cp):
            return dict(self.rec)

    def _registry(self, record, *, ready="ready"):
        from readiness import score_readiness
        reg = self._SA.SellerRegistry()
        rd = score_readiness({"payment_challenge": True, "manifest_present": True,
                              "manifest_well_formed": True, "https": True,
                              "endpoint_reachable": True, "openapi": True,
                              "homepage_reachable": True}) if ready == "ready" else \
            {"grade": ready, "score": 60}
        audit = self._SA.run_audit(readiness=rd, record=record, peer_ratio=1.1)
        reg.issue(self.MERCHANT, audit, issued_at=1000)
        return reg

    def _req(self, **o):
        return dict({"counterparty": self.MERCHANT, "amount": "0.09",
                     "asset": "USDC", "chain": "base"}, **o)

    def test_badge_lifts_thin_to_go(self):
        rec = {"settlement_count": 8, "confirmed_settlement_count": 8,
               "distinct_payers": 5, "dispute_rate": 0.0}
        src, reg = self._Src(rec), self._registry({"settlement_count": 8,
            "confirmed_settlement_count": 8, "distinct_payers": 5, "dispute_rate": 0.0})
        self.assertEqual(bw.forecast(self._req(), src)[0]["verdict"], "HOLD")  # thin
        r, _ = bw.forecast(self._req(), src, seller_registry=reg, now=2000)
        self.assertEqual(r["verdict"], "GO")
        self.assertEqual(r["signals"]["seller_verified"], "B")

    def test_badge_does_not_waive_sybil(self):
        # only 1 distinct payer -> wash-trade gate stays, even verified -> HOLD
        rec = {"settlement_count": 8, "confirmed_settlement_count": 8,
               "distinct_payers": 1, "dispute_rate": 0.0}
        reg = self._registry(rec)
        r, _ = bw.forecast(self._req(), self._Src(rec), seller_registry=reg, now=2000)
        self.assertEqual(r["verdict"], "HOLD")

    def test_badge_cannot_override_sanctioned(self):
        rec = {"settlement_count": 40, "confirmed_settlement_count": 40,
               "distinct_payers": 6, "dispute_rate": 0.0}
        reg = self._registry(rec)
        r, _ = bw.forecast(self._req(), self._Src(dict(rec, sanctioned=True)),
                           seller_registry=reg, now=2000)
        self.assertEqual(r["verdict"], "STOP")
        self.assertTrue(r["hard_stop"])

    def test_badge_cannot_override_gouge(self):
        rec = {"settlement_count": 40, "confirmed_settlement_count": 40,
               "distinct_payers": 6, "dispute_rate": 0.0, "price_history": ["0.09"] * 6}
        reg = self._registry(rec)
        # 10x the merchant's own median -> STOP regardless of the badge
        r, _ = bw.forecast(self._req(amount="0.90"), self._Src(rec),
                           seller_registry=reg, now=2000)
        self.assertEqual(r["verdict"], "STOP")

    def test_revoked_badge_no_lift(self):
        rec = {"settlement_count": 8, "confirmed_settlement_count": 8,
               "distinct_payers": 5, "dispute_rate": 0.0}
        reg = self._registry(rec)
        reg.revoke(self.MERCHANT)
        r, _ = bw.forecast(self._req(), self._Src(rec), seller_registry=reg, now=2000)
        self.assertEqual(r["verdict"], "HOLD")

    def test_badge_ignored_when_live_disputes_spike(self):
        # badge issued while clean, but live disputes have since exceeded the audit
        # bar -> the stale badge does NOT lift the verdict.
        clean_rec = {"settlement_count": 8, "confirmed_settlement_count": 8,
                     "distinct_payers": 5, "dispute_rate": 0.0}
        reg = self._registry(clean_rec)
        drifted = dict(clean_rec, dispute_rate=0.20)      # 20% disputes now
        r, _ = bw.forecast(self._req(), self._Src(drifted),
                           seller_registry=reg, now=2000)
        self.assertNotEqual(r["verdict"], "GO")
        self.assertIsNone(r["signals"]["seller_verified"])

    def test_no_registry_is_unaffected(self):
        rec = {"settlement_count": 40, "confirmed_settlement_count": 40,
               "distinct_payers": 6, "dispute_rate": 0.0}
        r, _ = bw.forecast(self._req(), self._Src(rec))
        self.assertIsNone(r["signals"]["seller_verified"])


class TestForecastEndToEnd(unittest.TestCase):
    """forecast(): validate -> mock lookup -> decide -> receipt."""

    def setUp(self):
        self.src = bw.MockReputationSource()

    def test_known_good_go(self):
        payload = {
            "counterparty": "0xKNOWNGOOD000000000000000000000000000001",
            "amount": "0.09", "asset": "USDC", "chain": "base",
        }
        resp, err = bw.forecast(payload, self.src)
        self.assertIsNone(err)
        self.assertEqual(resp["verdict"], "GO")
        self.assertTrue(resp["receipt_id"].startswith("bw_"))

    def test_sanctioned_stop(self):
        payload = {
            "counterparty": "0xSANCTIONED00000000000000000000000000003",
            "amount": "0.09", "asset": "USDC", "chain": "base",
        }
        resp, err = bw.forecast(payload, self.src)
        self.assertEqual(resp["verdict"], "STOP")

    def test_graph_source_captive_sybil_holds_a_known_good(self):
        # forecast threads a payer-graph source; a captive-Sybil signal escalates
        # an otherwise-GO counterparty to HOLD. Mutation: not passing the signal
        # through forecast -> stays GO.
        import payer_graph as PG
        good = "0xKNOWNGOOD000000000000000000000000000001"
        # 4 payers, each pays ONLY `good` -> captive_sybil
        edges = [("0x%040x" % i, good) for i in range(1, 5)]
        gs = PG.PayerGraphSource(edges)
        payload = {"counterparty": good, "amount": "0.09", "asset": "USDC", "chain": "base"}
        self.assertEqual(bw.forecast(payload, self.src)[0]["verdict"], "GO")   # baseline
        resp, err = bw.forecast(payload, self.src, graph_source=gs)
        self.assertEqual(resp["verdict"], "HOLD")
        self.assertEqual(resp["signals"]["cross_counterparty"]["established_payers"], 0)

    def test_graph_source_failure_is_fail_open(self):
        class _Boom:
            def cross_signal(self, cp):
                raise RuntimeError("graph down")
        payload = {"counterparty": "0xKNOWNGOOD000000000000000000000000000001",
                   "amount": "0.09", "asset": "USDC", "chain": "base"}
        resp, err = bw.forecast(payload, self.src, graph_source=_Boom())
        self.assertIsNone(err)
        self.assertEqual(resp["verdict"], "GO")          # graph outage never blocks

    def test_unknown_counterparty_holds(self):
        payload = {
            "counterparty": "0xUNSEEN", "amount": "0.09",
            "asset": "USDC", "chain": "base",
        }
        resp, err = bw.forecast(payload, self.src)
        self.assertEqual(resp["verdict"], "HOLD")

    def test_thin_newbie_holds(self):
        payload = {
            "counterparty": "0xNEWBIE0000000000000000000000000000000002",
            "amount": "0.09", "asset": "USDC", "chain": "base",
        }
        resp, err = bw.forecast(payload, self.src)
        self.assertEqual(resp["verdict"], "HOLD")

    def test_receipt_unique_per_payment(self):
        # The receipt is the ledger's join key: it MUST be unique per payment,
        # even for two different counterparties with identical verdict content,
        # and even for repeated identical requests. (Regression: it used to be a
        # hash of verdict-only and collided across counterparties.)
        base = {"amount": "0.09", "asset": "USDC", "chain": "base"}
        a, _ = bw.forecast(dict(base, counterparty="0xAAA"), self.src)
        b, _ = bw.forecast(dict(base, counterparty="0xBBB"), self.src)
        c, _ = bw.forecast(dict(base, counterparty="0xAAA"), self.src)
        self.assertNotEqual(a["receipt_id"], b["receipt_id"])
        self.assertNotEqual(a["receipt_id"], c["receipt_id"])

    def test_response_shape_matches_contract(self):
        payload = {
            "counterparty": "0xKNOWNGOOD000000000000000000000000000001",
            "amount": "0.09", "asset": "USDC", "chain": "base",
        }
        resp, _ = bw.forecast(payload, self.src)
        for key in ("verdict", "score", "reasons", "signals", "receipt_id"):
            self.assertIn(key, resp)
        for key in ("counterparty_reputation", "price_anomaly",
                    "reversibility", "blast_radius"):
            self.assertIn(key, resp["signals"])
        # JSON-serializable (the wire format).
        json.dumps(resp)


class TestServerHardening(unittest.TestCase):
    """The HTTP layer must answer malformed requests, never drop the socket."""

    def setUp(self):
        import threading
        self.server = bw.BlackwallServer(port=0)
        # bind without serve_forever's blocking loop
        from http.server import ThreadingHTTPServer
        handler = type("_H", (bw._Handler,),
                       {"reputation_source": bw.MockReputationSource()})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _raw(self, headers, body=b""):
        import socket
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        s.sendall(headers.encode() + body)
        data = b""
        while True:
            c = s.recv(4096)
            if not c:
                break
            data += c
        s.close()
        return data.split(b"\r\n", 1)[0].decode("latin-1")

    def test_malformed_content_length_gets_400_not_dropped(self):
        status = self._raw(
            "POST /v1/forecast-payment HTTP/1.1\r\nHost: x\r\n"
            "Content-Length: abc\r\nConnection: close\r\n\r\n{}")
        self.assertIn("400", status)

    def test_non_json_gets_400(self):
        body = b"{not json"
        status = self._raw(
            "POST /v1/forecast-payment HTTP/1.1\r\nHost: x\r\n"
            "Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body), body)
        self.assertIn("400", status)

    def test_head_healthz_is_200_not_501(self):
        # REGRESSION: HEAD probes (UptimeRobot/crawlers) must not get 501 ("down").
        status = self._raw(
            "HEAD /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        self.assertIn("200", status)
        self.assertNotIn("501", status)


class TestFailClosedOnCrash(unittest.TestCase):
    """AUDIT (fuzzer finding): a raising reputation source must fail CLOSED gracefully
    -- a clean 503 (never a GO, never a dropped connection / leaked traceback), not a
    propagated exception."""

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer

        class _Boom:
            def lookup(self, cp):
                raise RuntimeError("store locked")
        self.stats = bw._Stats()
        handler = type("_H", (bw._Handler,),
                       {"reputation_source": _Boom(), "stats": self.stats})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_raising_source_gets_503_not_crash(self):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        c.request("POST", "/v1/forecast-payment",
                  body='{"counterparty":"0x0000000000000000000000000000000000000001",'
                       '"amount":"0.09","asset":"USDC","chain":"base"}',
                  headers={"Content-Type": "application/json"})
        r = c.getresponse(); body = r.read(); c.close()
        self.assertEqual(r.status, 503)                    # fail closed, cleanly
        self.assertNotIn(b"Traceback", body)               # no leaked internals
        self.assertEqual(self.stats.snapshot().get("errors"), 1)


class TestRateLimitAndStats(unittest.TestCase):
    """Per-client rate limit on POSTs + /stats counters.

    Mutation notes:
      - don't check the limiter -> test_post_limited FAILS.
      - limit GET too -> test_health_not_limited FAILS (health checkers must pass).
      - not counting rate_limited/verdicts -> test_stats FAILS.
    """
    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        from ratelimit import RateLimiter
        self.stats = bw._Stats()
        handler = type("_H", (bw._Handler,),
                       {"reputation_source": bw.MockReputationSource(),
                        "rate_limiter": RateLimiter(60, 60, burst=2),   # burst 2
                        "stats": self.stats})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _post(self):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        c.request("POST", "/v1/forecast-payment",
                  body='{"counterparty":"0x000000000000000000000000000000000000dEaD",'
                       '"amount":"0.01","asset":"USDC","chain":"base"}',
                  headers={"Content-Type": "application/json"})
        r = c.getresponse(); r.read(); c.close()
        return r.status

    def _get(self, path):
        import http.client
        import json as _json
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        c.request("GET", path)
        r = c.getresponse(); body = r.read(); c.close()
        return r.status, (_json.loads(body) if body else {})

    def test_post_limited_after_burst(self):
        self.assertEqual(self._post(), 200)   # burst token 1
        self.assertEqual(self._post(), 200)   # burst token 2
        self.assertEqual(self._post(), 429)   # bucket empty -> throttled

    def test_health_not_limited(self):
        for _ in range(5):
            self.assertEqual(self._get("/healthz")[0], 200)   # never throttled

    def test_stats_counts(self):
        self._post(); self._post(); self._post()               # 2 ok, 1 blocked
        status, snap = self._get("/stats")
        self.assertEqual(status, 200)
        self.assertEqual(snap.get("requests"), 3)
        self.assertEqual(snap.get("rate_limited"), 1)
        self.assertEqual(snap.get("verdict_hold"), 2)          # dEaD -> cold-start HOLD


class TestReadinessFailOpen(unittest.TestCase):
    """REGRESSION (audit MED): a raising readiness source must NOT break the core
    verdict -- forecast() must fail open at the consumer."""
    class _GoodRep:
        def lookup(self, cp):
            return {"settlement_count": 1000, "confirmed_settlement_count": 1000,
                    "distinct_payers": 50, "dispute_rate": 0.0, "price_history": ["1"] * 30}

    class _BoomReadiness:
        def check(self, url):
            raise RuntimeError("readiness oracle exploded")

    def test_raising_readiness_does_not_break_verdict(self):
        req = {"counterparty": "0x" + "1" * 40, "amount": "1.00",
               "asset": "0xUSDC", "chain": "base", "resource": "https://api.example.com/x"}
        resp, err = bw.forecast(dict(req), self._GoodRep(),
                                readiness_source=self._BoomReadiness())
        self.assertIsNone(err)
        self.assertIn(resp["verdict"], ("GO", "HOLD", "STOP"))   # a verdict, not a crash
        self.assertNotIn("endpoint_readiness", resp["signals"])  # readiness simply absent


class TestRobustPriceMedian(unittest.TestCase):
    """
    Wash-trade resistance: a counterparty paying ITSELF many times must not be
    able to anchor its own 'normal' price.

    Mutation notes:
      - count each observation instead of collapsing per payer -> test_wash_trade_defeated FAILS.
      - drop the min_payers gate -> test_thin_returns_none FAILS.
      - keep non-positive/garbage amounts -> test_garbage_dropped FAILS.
    """
    def test_wash_trade_defeated(self):
        # attacker self-settles 50x at 100 to inflate the FLAT median to ~100;
        # three legit payers paid 1. Flat median would bless a 100 overcharge.
        obs = [{"payer": "0xattacker", "amount": "100"} for _ in range(50)]
        obs += [{"payer": "0xlegit1", "amount": "1"},
                {"payer": "0xlegit2", "amount": "1"},
                {"payer": "0xlegit3", "amount": "1"}]
        flat = bw.price_anomaly_ratio("100", [o["amount"] for o in obs])
        robust = bw.price_anomaly_ratio("100", None, observations=obs)
        self.assertLess(flat, 2.0)        # flat median ~100 -> 100/100 ~ 1 (blessed)
        self.assertGreater(robust, 50.0)  # robust median ~1 -> 100/1 = 100 (caught)
        med, n = bw.robust_price_median(obs)
        self.assertEqual(n, 4)            # 4 distinct payers, not 53 observations
        self.assertEqual(med, Decimal("1"))

    def test_thin_returns_none(self):
        # fewer than MIN_DISTINCT_PAYERS distinct payers -> not trustworthy
        obs = [{"payer": "0xa", "amount": "5"}, {"payer": "0xb", "amount": "5"}]
        med, n = bw.robust_price_median(obs)
        self.assertIsNone(med)
        self.assertEqual(n, 2)
        # ...and price_anomaly_ratio falls back to the flat median
        r = bw.price_anomaly_ratio("10", ["5", "5"], observations=obs)
        self.assertEqual(r, 2.0)

    def test_per_payer_collapse(self):
        # one payer, 100 observations -> counts as ONE payer (below min) -> None
        obs = [{"payer": "0xsolo", "amount": "7"} for _ in range(100)]
        med, n = bw.robust_price_median(obs)
        self.assertIsNone(med)
        self.assertEqual(n, 1)

    def test_garbage_dropped(self):
        obs = [{"payer": "0xa", "amount": "5"}, {"payer": "0xb", "amount": "0"},
               {"payer": "0xc", "amount": "notnum"}, {"payer": None, "amount": "5"},
               {"payer": "0xd", "amount": "5"}, {"payer": "0xe", "amount": "5"}]
        med, n = bw.robust_price_median(obs)
        # only 0xa, 0xd, 0xe carry a usable positive amount -> 3 payers, median 5
        self.assertEqual(n, 3)
        self.assertEqual(med, Decimal("5"))

    def test_decide_payment_reports_price_basis(self):
        obs = [{"payer": "0x%d" % i, "amount": "1"} for i in range(5)]
        rec = {"settlement_count": 100, "confirmed_settlement_count": 100,
               "distinct_payers": 5, "dispute_rate": 0.0,
               "price_observations": obs, "price_history": ["1"] * 5}
        v = bw.decide_payment("1.00", rec, ["1"] * 5, counterparty="0x" + "1" * 40)
        self.assertEqual(v["signals"]["price_basis"], "payer-weighted")
        # without observations -> flat basis
        rec2 = dict(rec); rec2.pop("price_observations")
        v2 = bw.decide_payment("1.00", rec2, ["1"] * 5, counterparty="0x" + "1" * 40)
        self.assertEqual(v2["signals"]["price_basis"], "flat")


class TestFlatMedianFiltering(unittest.TestCase):
    """The FLAT median (price_median_and_basis) must drop the SAME junk that
    robust_price_median and flat_stop_trustworthy drop: non-positive, non-finite,
    and non-numeric entries. Otherwise the median that produces the anomaly RATIO
    judges a different distribution than the trustworthiness gate, and $0/garbage
    entries silently blind or distort the price-anomaly check.

    Mutation notes:
      - keep non-positive entries in the flat median -> test_zero_entries_do_not_
        suppress_anomaly FAILS (a gouge auto-GOes) and test_zero_deflation_no_
        false_stop FAILS (a legit payment false-STOPs).
      - keep non-numeric entries -> test_garbage_entry_does_not_crash FAILS (500).
    """
    GOOD = {"settlement_count": 1240, "dispute_rate": 0.002}

    def test_flat_median_drops_nonpositive(self):
        # $0 free-tier quotes mixed with the real $0.09 price. The flat median
        # must be the positive subset's median (0.09), NOT 0 (which nukes the
        # ratio to None and suppresses the anomaly entirely).
        m, basis = bw.price_median_and_basis(["0", "0", "0.09"], None)
        self.assertEqual(basis, "flat")
        self.assertEqual(m, Decimal("0.09"))

    def test_zero_entries_do_not_suppress_anomaly(self):
        # REGRESSION (gouge bypass): a counterparty whose history mixes $0
        # (free-tier) and $0.09 quotes must NOT get a clean GO on a $10 charge --
        # that is 111x its real median. Zeros pulling the flat median to 0 (ratio
        # None, anomaly 0.0) previously auto-approved the overcharge.
        v = bw.decide_payment("10.00", self.GOOD, ["0", "0", "0.09"],
                              counterparty="0xA")
        self.assertNotEqual(v["verdict"], "GO")
        self.assertGreater(v["signals"]["price_anomaly"], 0.0)

    def test_zero_deflation_no_false_stop(self):
        # REGRESSION (false STOP): zeros deflating a NON-zero flat median inflate
        # the ratio while flat_stop_trustworthy (which filters the zeros) sees a
        # tight positive subset and blesses the STOP. True median is 0.09, $0.36
        # is only 4x -> must HOLD, never STOP.
        v = bw.decide_payment("0.36", self.GOOD,
                              ["0", "0", "0", "0", "0.09", "0.09", "0.09", "0.09"],
                              counterparty="0xA")
        self.assertNotEqual(v["verdict"], "STOP")

    def test_garbage_entry_does_not_crash(self):
        # A non-numeric entry in agent-supplied quoted_price_history must be
        # dropped, not raise InvalidOperation up into a 500.
        v = bw.decide_payment("0.50", self.GOOD, ["abc", "0.09", "0.09"],
                              counterparty="0xA")
        self.assertIn(v["verdict"], ("GO", "HOLD", "STOP"))


class TestDefaultBillingAsset(unittest.TestCase):
    """
    The 402 challenge must advertise the RIGHT USDC contract for the network,
    so a funded signer signs an EIP-3009 authorization the facilitator can settle.

    Mutation notes:
      - ignore `explicit` (always derive) -> test_explicit_wins FAILS.
      - return base USDC for base-sepolia -> test_sepolia_default FAILS (this is
        the dangerous mutation: mainnet USDC advertised on a testnet deploy).
      - return sepolia USDC for mainnet -> test_mainnet_default FAILS.
    """
    BASE = "0xBASE"
    SEP = "0xSEPOLIA"

    def test_explicit_wins(self):
        self.assertEqual(
            bw.default_billing_asset("base-sepolia", "0xCUSTOM", self.BASE, self.SEP),
            "0xCUSTOM")

    def test_sepolia_default(self):
        self.assertEqual(
            bw.default_billing_asset("base-sepolia", None, self.BASE, self.SEP),
            self.SEP)

    def test_mainnet_default(self):
        self.assertEqual(
            bw.default_billing_asset("base", None, self.BASE, self.SEP), self.BASE)
        # unknown network is treated as mainnet (conservative, not testnet)
        self.assertEqual(
            bw.default_billing_asset("polygon", None, self.BASE, self.SEP), self.BASE)

    def test_network_normalized(self):
        # REGRESSION (audit HIGH): a case/whitespace slip on the network must NOT
        # silently advertise mainnet USDC on a testnet deploy.
        for variant in ("Base-Sepolia", "BASE-SEPOLIA", " base-sepolia", "base-sepolia\n"):
            self.assertEqual(
                bw.default_billing_asset(variant, None, self.BASE, self.SEP),
                self.SEP, variant)


class TestSanctionsEnabled(unittest.TestCase):
    """
    REGRESSION (audit, integrity): screening must only be enabled/advertised when
    the OFAC list actually loaded addresses. A missing/empty file enabling the
    wrapper would make the discovery descriptor advertise `screening:
    ["sanctions-ofac"]` while screening nothing -- claiming a check it doesn't do.

    Mutation notes:
      - `len(list) >= 0` (always True) -> test_empty_disables FAILS.
      - `return True` (ignore the list) -> test_empty_disables/test_none FAIL.
      - `return False` (never enable)  -> test_nonempty_enables FAILS.
    """

    def test_empty_disables(self):
        from sanctions import SanctionsList
        self.assertFalse(bw.sanctions_enabled(SanctionsList()))

    def test_none_disables(self):
        self.assertFalse(bw.sanctions_enabled(None))

    def test_nonempty_enables(self):
        from sanctions import SanctionsList
        sl = SanctionsList(["0x0330070fd38ec3bb94f58fa55d40368271e9e54a"])
        self.assertTrue(bw.sanctions_enabled(sl))


class TestComplianceFree(unittest.TestCase):
    """
    A sanctioned counterparty's STOP is served FREE (the compliance floor),
    bypassing the billing gate -- Blackwall is a SUPERSET of the free KYT
    baseline, so it must never charge to deliver an OFAC STOP. Price/reputation
    STOPs stay paid.

    Mutation notes:
      - return True always -> test_clean_not_free / test_no_sanctions FAIL.
      - return False always -> test_sanctioned_is_free FAILS.
      - drop the getattr(.., "sanctions") guard -> test_no_sanctions raises/FAILS.
      - ignore counterparty (screen something else) -> test_clean_not_free FAILS.
    """
    SANC = "0x0330070fd38ec3bb94f58fa55d40368271e9e54a"

    def _wrapped(self):
        from sanctions import SanctionsList, SanctionsScreeningSource
        class _Inner:
            def lookup(self, cp): return {}
        return SanctionsScreeningSource(_Inner(), SanctionsList([self.SANC]))

    def test_sanctioned_is_free(self):
        src = self._wrapped()
        self.assertTrue(bw.is_compliance_free(src, {"counterparty": self.SANC}))
        # case-insensitive: an uppercased sanctioned address is still free-STOP
        self.assertTrue(bw.is_compliance_free(src, {"counterparty": self.SANC.upper()}))

    def test_clean_not_free(self):
        src = self._wrapped()
        self.assertFalse(bw.is_compliance_free(
            src, {"counterparty": "0x00000000000000000000000000000000deadbeef"}))

    def test_no_sanctions_source(self):
        # A bare source with no sanctions wrapper must never short-circuit billing.
        class _Bare:
            def lookup(self, cp): return {}
        self.assertFalse(bw.is_compliance_free(_Bare(), {"counterparty": self.SANC}))

    def test_malformed_payload(self):
        src = self._wrapped()
        self.assertFalse(bw.is_compliance_free(src, None))
        self.assertFalse(bw.is_compliance_free(src, {}))  # no counterparty


class TestPerClassPriceSegmentation(unittest.TestCase):
    """
    Per-invoice-class price comparison: an amount is priced against the payee's
    LIKE-FOR-LIKE history when enough same-`resource` observations exist, else it
    pools (conservative fallback). Fixes the false-gouge on a first large invoice
    to a vendor whose overall history is small.

    Mutation notes:
      - ignore resource / always pool -> test_per_class_avoids_false_gouge FAILS.
      - use per-class with < MIN_CLASS_OBSERVATIONS -> test_select_pooled_when_sparse FAILS.
      - report a stale basis (not from the selected set) -> test_decide_per_class FAILS.
    """
    # 20 small "api" charges + 3 large "invoice" charges, all DISTINCT payers.
    OBS = ([{"payer": "0x%040x" % i, "amount": "50", "resource": "api"}
            for i in range(20)]
           + [{"payer": "0x%040x" % (100 + i), "amount": "5000",
               "resource": "invoice"} for i in range(3)])
    REC = {"settlement_count": 250, "dispute_rate": 0.0,
           "confirmed_settlement_count": 250, "distinct_payers": 23}

    def _rec(self):
        return dict(self.REC, price_observations=self.OBS)

    def test_select_per_class_when_enough(self):
        sel, basis = bw.select_class_observations(self.OBS, "invoice")
        self.assertEqual(basis, "per-class")
        self.assertEqual(len(sel), 3)

    def test_select_pooled_when_sparse(self):
        sel, basis = bw.select_class_observations(self.OBS, "wire")  # 0 members
        self.assertEqual(basis, "pooled")
        self.assertEqual(len(sel), len(self.OBS))

    def test_select_pooled_when_no_resource(self):
        _sel, basis = bw.select_class_observations(self.OBS, None)
        self.assertEqual(basis, "pooled")

    def test_tuples_never_match(self):
        _sel, basis = bw.select_class_observations(
            [("0xp1", "50"), ("0xp2", "50")], "api")
        self.assertEqual(basis, "pooled")

    def test_per_class_avoids_false_gouge(self):
        # $5000 invoice vs the $5000 invoice-class median -> ratio ~1 (NOT a gouge)
        r = bw.price_anomaly_ratio("5000", [], observations=self.OBS,
                                   resource="invoice")
        self.assertLess(r, 2.0)

    def test_pooled_flags_the_same_amount(self):
        # the same $5000 with NO class context -> pooled median ~$50 -> ~100x
        r = bw.price_anomaly_ratio("5000", [], observations=self.OBS, resource=None)
        self.assertGreater(r, 8.0)

    def test_decide_per_class_not_stop(self):
        v = bw.decide_payment("5000", self._rec(), [], counterparty="0xV",
                              resource="invoice", hold_above="100000")
        self.assertNotEqual(v["verdict"], "STOP")
        self.assertEqual(v["signals"]["price_basis"], "per-class")

    def test_decide_pooled_stops_gouge(self):
        v = bw.decide_payment("5000", self._rec(), [], counterparty="0xV",
                              resource=None, hold_above="100000")
        self.assertEqual(v["verdict"], "STOP")


class TestPeerGroupCrossCheck(unittest.TestCase):
    """
    Peer-group cross-check: is a counterparty priced far above COMPARABLE
    counterparties for the same resource class? Expensive != fraud -> HOLD, never
    STOP. Sybil-hardened: the market rate is a median across DISTINCT
    counterparties, so one actor can't define it.

    Mutation notes:
      - drop the min_counterparties gate -> test_index_needs_distinct_peers FAILS.
      - let peer_hold set STOP instead of HOLD -> test_peer_outlier_holds_not_stops FAILS.
      - ignore peer_median in the GO gate -> test_peer_outlier_holds_not_stops FAILS.
      - use amount instead of the cp's own median as reference -> test_at_market_go FAILS.
    """
    # counterparty's own class-"x" history: $500 from 3 distinct payers
    OBS = [{"payer": "0x%040x" % i, "amount": "500", "resource": "x"}
           for i in range(3)]
    REC = {"settlement_count": 250, "dispute_rate": 0.0,
           "confirmed_settlement_count": 250, "distinct_payers": 3}

    def _rec(self):
        return dict(self.REC, price_observations=self.OBS)

    # ---- pure pieces ----
    def test_peer_group_median_needs_min(self):
        self.assertIsNone(bw.peer_group_median(["1", "2"]))       # < 3 peers
        self.assertEqual(bw.peer_group_median(["1", "3", "2"]), Decimal("2"))

    def test_index_needs_distinct_peers(self):
        # 5 observations but ONE counterparty -> no market rate for the class
        one_cp = [{"counterparty": "0xA", "resource_class": "x", "amount": "500"}
                  for _ in range(5)]
        self.assertEqual(bw.build_peer_class_index(one_cp), {})

    def test_index_across_counterparties(self):
        obs = [{"counterparty": "0x%d" % i, "resource_class": "x", "amount": "100"}
               for i in range(3)]
        idx = bw.build_peer_class_index(obs)
        self.assertEqual(idx["x"], "100")

    def test_peer_anomaly_ratio_none_without_market(self):
        self.assertIsNone(bw.peer_anomaly_ratio("500", None))

    # ---- verdict integration ----
    def test_peer_outlier_holds_not_stops(self):
        # cp median $500 vs $100 market = 5x -> HOLD (would-be GO), NOT STOP
        v = bw.decide_payment("500", self._rec(), [], counterparty="0xV",
                              resource="x", hold_above="100000", peer_median="100")
        self.assertEqual(v["verdict"], "HOLD")
        self.assertAlmostEqual(v["signals"]["peer_price_ratio"], 5.0, places=1)
        self.assertTrue(any("peer-group market rate" in r for r in v["reasons"]))

    def test_at_market_go(self):
        # priced AT the market -> the peer check doesn't block a GO
        v = bw.decide_payment("500", self._rec(), [], counterparty="0xV",
                              resource="x", hold_above="100000", peer_median="500")
        self.assertEqual(v["verdict"], "GO")

    def test_no_peer_median_no_effect(self):
        v = bw.decide_payment("500", self._rec(), [], counterparty="0xV",
                              resource="x", hold_above="100000", peer_median=None)
        self.assertEqual(v["verdict"], "GO")
        self.assertIsNone(v["signals"]["peer_price_ratio"])

    def test_cold_start_uses_amount(self):
        # established rep but NO price history -> reference is the quoted amount
        rec = {"settlement_count": 250, "dispute_rate": 0.0,
               "confirmed_settlement_count": 250, "distinct_payers": 5}
        v = bw.decide_payment("300", rec, [], counterparty="0xV",
                              hold_above="100000", peer_median="100")  # 3x market
        self.assertEqual(v["verdict"], "HOLD")

    def test_forecast_threads_peer_index(self):
        class _Src:
            def lookup(self, cp):
                return {"settlement_count": 250, "dispute_rate": 0.0,
                        "confirmed_settlement_count": 250, "distinct_payers": 3,
                        "price_observations": TestPeerGroupCrossCheck.OBS}
        payload = {"counterparty": "0xV0000000000000000000000000000000000001",
                   "amount": "500", "asset": "USDC", "chain": "base",
                   "resource": "x", "resource_class": "weather-call"}
        resp, err = bw.forecast(payload, _Src(), hold_above="100000",
                                peer_index={"weather-call": "100"})
        self.assertIsNone(err)
        self.assertEqual(resp["verdict"], "HOLD")  # 5x the peer market


class TestCategoryPriceBaseline(unittest.TestCase):
    """
    Per-CATEGORY on-chain price baseline: is the QUOTED amount an order-of-magnitude
    outlier vs what its service category actually collects? Catches a cold-start
    gouge the per-payee gate is blind to. Advisory: HOLD, never STOP; fail-open.

    Mutation notes:
      - trip below an order of magnitude -> test_below_ratio_go FAILS (would false-flag).
      - let category_hold set STOP -> test_outlier_holds_not_stops FAILS.
      - use the cp's median instead of the quoted amount -> test_cold_start_uses_amount FAILS.
      - ignore category_median in the GO gate -> test_outlier_holds_not_stops FAILS.
    """
    # reputable enough to otherwise GO; NO own price history (cold-start on price)
    REC = {"settlement_count": 60, "confirmed_settlement_count": 60,
           "distinct_payers": 8, "dispute_rate": 0.0}

    def test_outlier_holds_not_stops(self):
        # quote 0.30 vs finance on-chain median 0.005 = 60x -> HOLD (would-be GO)
        v = bw.decide_payment("0.30", dict(self.REC), ["0.30"], counterparty="0xV",
                              category="finance", category_median="0.005")
        self.assertEqual(v["verdict"], "HOLD")
        self.assertAlmostEqual(v["signals"]["category_price_ratio"], 60.0, places=1)
        self.assertEqual(v["signals"]["category"], "finance")
        self.assertTrue(any("category" in r for r in v["reasons"]))

    def test_below_ratio_go(self):
        # 20x is premium-but-legit (under the 50x bar) -> not a category HOLD
        v = bw.decide_payment("0.10", dict(self.REC), ["0.10"], counterparty="0xV",
                              category="finance", category_median="0.005")
        self.assertEqual(v["verdict"], "GO")

    def test_cold_start_uses_amount(self):
        # reference is the QUOTED amount (no own median needed) -> 100x -> HOLD
        v = bw.decide_payment("0.50", dict(self.REC), [], counterparty="0xV",
                              category="finance", category_median="0.005")
        self.assertEqual(v["verdict"], "HOLD")

    def test_no_baseline_no_effect(self):
        v = bw.decide_payment("0.30", dict(self.REC), ["0.30"], counterparty="0xV",
                              category="finance", category_median=None)
        self.assertEqual(v["verdict"], "GO")
        self.assertIsNone(v["signals"]["category_price_ratio"])

    def test_forecast_threads_category_index(self):
        class _Src:
            def lookup(self, cp):
                return {"settlement_count": 60, "confirmed_settlement_count": 60,
                        "distinct_payers": 8, "dispute_rate": 0.0}
        payload = {"counterparty": "0xV0000000000000000000000000000000000001",
                   "amount": "0.30", "asset": "USDC", "chain": "base",
                   "resource": "https://api.foo/price/btc"}   # -> classifies 'finance'
        resp, err = bw.forecast(payload, _Src(), category_index={"finance": "0.005"})
        self.assertIsNone(err)
        self.assertEqual(resp["verdict"], "HOLD")             # 60x the finance median
        self.assertEqual(resp["signals"]["category"], "finance")


class TestPriceDivergence(unittest.TestCase):
    """
    Advertised-vs-settled divergence: a payee that lists cheap but historically settles
    far above its most-expensive listing (bait-and-switch) -> HOLD, never STOP, fail-open.

    Mutation notes:
      - ignore divergence_ratio in the GO gate -> test_bait_holds FAILS.
      - let it STOP -> test_bait_holds FAILS (asserts HOLD).
      - trip below the ratio -> test_under_threshold_go FAILS.
    """
    REC = {"settlement_count": 500, "dispute_rate": 0.0, "distinct_payers": 30}

    def test_bait_holds_not_stops(self):
        v = bw.decide_payment("0.09", dict(self.REC), ["0.09"], counterparty="0xV",
                              divergence_ratio="12.0")
        self.assertEqual(v["verdict"], "HOLD")
        self.assertEqual(v["signals"]["price_divergence_ratio"], 12.0)
        self.assertTrue(any("bait-and-switch" in r for r in v["reasons"]))

    def test_under_threshold_go(self):
        v = bw.decide_payment("0.09", dict(self.REC), ["0.09"], counterparty="0xV",
                              divergence_ratio="8.0")   # < 10x bar
        self.assertEqual(v["verdict"], "GO")

    def test_no_ratio_no_effect(self):
        v = bw.decide_payment("0.09", dict(self.REC), ["0.09"], counterparty="0xV")
        self.assertEqual(v["verdict"], "GO")
        self.assertIsNone(v["signals"]["price_divergence_ratio"])

    def test_garbage_ratio_fails_open(self):
        v = bw.decide_payment("0.09", dict(self.REC), ["0.09"], counterparty="0xV",
                              divergence_ratio="not-a-number")
        self.assertEqual(v["verdict"], "GO")   # unparseable -> no gate, no crash

    def test_forecast_threads_divergence_index(self):
        class _Src:
            def lookup(self, cp):
                return {"settlement_count": 500, "dispute_rate": 0.0, "distinct_payers": 30}
        cp = "0x" + "1" * 40
        payload = {"counterparty": cp, "amount": "0.09", "asset": "USDC", "chain": "base"}
        resp, err = bw.forecast(payload, _Src(), divergence_index={cp: "12.0"})
        self.assertIsNone(err)
        self.assertEqual(resp["verdict"], "HOLD")
        self.assertEqual(resp["signals"]["price_divergence_ratio"], 12.0)


class TestHardStopFlag(unittest.TestCase):
    """
    `hard_stop` distinguishes a non-negotiable block (sanctioned / known-bad /
    recipient-mismatch) from a judgment STOP (price gouge) -- so a consumer can map
    STOP -> hard block vs. human-overridable deny without sniffing reason strings.

    Mutation notes:
      - always return hard_stop=False -> test_sanctioned_is_hard FAILS.
      - set hard_stop on a price gouge -> test_price_gouge_not_hard FAILS.
      - set hard_stop on GO/HOLD -> test_go_and_hold_not_hard FAILS.
    """
    GOOD = {"settlement_count": 1240, "dispute_rate": 0.002}
    STABLE = ["0.09", "0.09", "0.088", "0.092"]

    def test_sanctioned_is_hard(self):
        v = bw.decide_payment("0.09", dict(self.GOOD, sanctioned=True),
                              self.STABLE, counterparty="0xA")
        self.assertEqual(v["verdict"], "STOP")
        self.assertTrue(v["hard_stop"])

    def test_known_bad_is_hard(self):
        v = bw.decide_payment("0.09", dict(self.GOOD, known_bad=True),
                              self.STABLE, counterparty="0xA")
        self.assertTrue(v["hard_stop"])

    def test_recipient_mismatch_is_hard(self):
        v = bw.decide_payment("0.09", self.GOOD, self.STABLE,
                              counterparty="0xATTACKER", expected_recipient="0xLEGIT")
        self.assertTrue(v["hard_stop"])

    def test_price_gouge_not_hard(self):
        # $5 vs a ~$0.09 median -> STOP on price, but NOT a hard stop
        v = bw.decide_payment("5.00", self.GOOD, self.STABLE, counterparty="0xA")
        self.assertEqual(v["verdict"], "STOP")
        self.assertFalse(v["hard_stop"])

    def test_go_and_hold_not_hard(self):
        go = bw.decide_payment("0.09", self.GOOD, self.STABLE, counterparty="0xA")
        self.assertEqual(go["verdict"], "GO")
        self.assertFalse(go["hard_stop"])
        hold = bw.decide_payment("0.09", {"settlement_count": 1, "dispute_rate": 0.0},
                                 self.STABLE, counterparty="0xA")
        self.assertEqual(hold["verdict"], "HOLD")
        self.assertFalse(hold["hard_stop"])


class TestConfigurableHoldThreshold(unittest.TestCase):
    """
    `hold_above` raises the amount at which a verdict escalates to HOLD, WITHOUT
    letting sanctions or price-anomaly through (treasury/AP auto-release).

    Mutation notes:
      - ignore hold_above (always module default) -> test_raised_allows_go FAILS.
      - let hold_above bypass the price gouge -> test_does_not_bypass_price FAILS.
      - let hold_above bypass sanctions -> test_does_not_bypass_sanctions FAILS.
    """
    GOOD = {"settlement_count": 1240, "dispute_rate": 0.002}
    BIG_HISTORY = ["5000", "5000", "4900", "5100"]

    def test_default_threshold_holds_large(self):
        v = bw.decide_payment("5000", self.GOOD, self.BIG_HISTORY, counterparty="0xA")
        self.assertEqual(v["verdict"], "HOLD")  # over the $10 default

    def test_raised_allows_go(self):
        v = bw.decide_payment("5000", self.GOOD, self.BIG_HISTORY,
                              counterparty="0xA", hold_above="10000")
        self.assertEqual(v["verdict"], "GO")

    def test_does_not_bypass_price(self):
        # raising the amount ceiling must NOT let a price gouge through
        v = bw.decide_payment("5000", self.GOOD, ["0.09", "0.09", "0.088"],
                              counterparty="0xA", hold_above="10000")
        self.assertEqual(v["verdict"], "STOP")

    def test_does_not_bypass_sanctions(self):
        rec = dict(self.GOOD, sanctioned=True)
        v = bw.decide_payment("5000", rec, self.BIG_HISTORY,
                              counterparty="0xA", hold_above="10000")
        self.assertEqual(v["verdict"], "STOP")


class TestComplianceFreeBypassesBilling(unittest.TestCase):
    """
    END-TO-END (regression): with billing ON, a sanctioned counterparty must get
    its STOP for FREE (200, no 402), while a clean high-stakes request is billed
    (402). Locks the wiring `billing is not None and not free_stop`.

    Mutation note: drop `and not free_stop` -> sanctioned high-stakes gets 402,
    test_sanctioned_high_stakes_free FAILS.
    """
    SANC = "0x0330070fd38ec3bb94f58fa55d40368271e9e54a"

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        from sanctions import SanctionsList, SanctionsScreeningSource
        from x402 import (BillingConfig, BillingGate, MockFacilitator,
                          PricingPolicy, BASE_USDC)
        src = SanctionsScreeningSource(bw.MockReputationSource(),
                                       SanctionsList([self.SANC]))
        billing = BillingGate(
            BillingConfig(price="0.001", pay_to="0x000000000000000000000000000000000000dEaD",
                          network="base", asset=BASE_USDC),
            facilitator=MockFacilitator(approve=True),
            pricing=PricingPolicy(free_below="10.00", bps="10",
                                  min_fee="0.001", max_fee="0.10"))
        handler = type("_H", (bw._Handler,),
                       {"reputation_source": src, "billing": billing})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _post(self, cp, amount):
        import json as _json
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        body = _json.dumps({"counterparty": cp, "amount": amount,
                            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                            "chain": "base"}).encode()
        req = Request("http://127.0.0.1:%d/v1/forecast-payment" % self.port,
                      data=body, headers={"Content-Type": "application/json"})
        try:
            return urlopen(req, timeout=3).status, None
        except HTTPError as e:
            return e.code, _json.loads(e.read() or b"{}")

    def test_sanctioned_high_stakes_free(self):
        status, _ = self._post(self.SANC, "50.00")
        self.assertEqual(status, 200)  # FREE STOP, not 402

    def test_sanctioned_uppercased_high_stakes_free(self):
        status, _ = self._post(self.SANC.upper(), "50.00")
        self.assertEqual(status, 200)

    def test_clean_high_stakes_billed(self):
        status, body = self._post(
            "0x00000000000000000000000000000000deadbeef", "50.00")
        self.assertEqual(status, 402)  # judgment product still pays

    def test_clean_low_stakes_value_free(self):
        status, _ = self._post(
            "0x00000000000000000000000000000000deadbeef", "5.00")
        self.assertEqual(status, 200)  # value-pricing free under $10


class TestPaidResponseCarriesSettlementHeader(unittest.TestCase):
    """
    END-TO-END (regression): a per-call PAID forecast (200) MUST return the
    settlement result in the v2 `PAYMENT-RESPONSE` header (base64 SettlementResponse
    with the on-chain tx hash), per specs/transports-v2/http.md -- that header is
    the canonical channel for returning the settlement to the client.

    Mutation note: drop the PAYMENT-RESPONSE emission -> this FAILS (the paying
    client can never learn its settlement tx hash). A FREE or session-served call
    carries NO settlement, so no PAYMENT-RESPONSE is emitted there.
    """
    PAY_TO = "0x000000000000000000000000000000000000dEaD"
    ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        from x402 import (BillingConfig, BillingGate, MockFacilitator, BASE_USDC)
        billing = BillingGate(
            BillingConfig(price="0.001", pay_to=self.PAY_TO, network="base",
                          asset=BASE_USDC),
            facilitator=MockFacilitator(approve=True, settle_ok=True))
        handler = type("_H", (bw._Handler,),
                       {"reputation_source": bw.MockReputationSource(),
                        "billing": billing})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _pay(self, value="1000", nonce="0xpaidnonce1"):
        import base64 as _b64
        import json as _json
        from urllib.request import Request, urlopen
        pay = {"x402Version": 2,
               "accepted": {"scheme": "exact", "network": "eip155:8453"},
               "payload": {"authorization": {"from": "0x" + "a" * 40,
                                             "to": self.PAY_TO, "value": value,
                                             "nonce": nonce, "validAfter": "0",
                                             "validBefore": "99999999999"},
                           "signature": "0xsig"}}
        sig = _b64.b64encode(_json.dumps(pay).encode()).decode()
        body = _json.dumps({"counterparty": "0x" + "c" * 40, "amount": "0.50",
                            "asset": self.ASSET, "chain": "base"}).encode()
        req = Request("http://127.0.0.1:%d/v1/forecast-payment" % self.port,
                      data=body, headers={"Content-Type": "application/json",
                                          "PAYMENT-SIGNATURE": sig})
        r = urlopen(req, timeout=3)
        return r.status, dict(r.headers), r.read()

    def test_paid_call_emits_payment_response_header(self):
        import base64 as _b64
        import json as _json
        status, headers, _ = self._pay()
        self.assertEqual(status, 200)
        pr = headers.get("PAYMENT-RESPONSE") or headers.get("Payment-Response")
        self.assertIsNotNone(pr, "paid 200 must carry a PAYMENT-RESPONSE header")
        settle = _json.loads(_b64.b64decode(pr))
        self.assertTrue(settle.get("success"))
        self.assertEqual(settle.get("transaction"), "0xmocktx")


class TestStatsEndpoint(unittest.TestCase):
    """GET /v1/stats: operator-only, token-gated funnel metrics."""

    def _serve(self, stats_token, ledger=None, watch_stats=None):
        import threading
        from http.server import ThreadingHTTPServer
        handler = type("_H", (bw._Handler,),
                       {"reputation_source": bw.MockReputationSource(),
                        "ledger": ledger, "stats_token": stats_token,
                        "watch_stats": watch_stats})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return port

    def _get(self, port, auth=None):
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        req = Request("http://127.0.0.1:%d/v1/stats" % port)
        if auth:
            req.add_header("Authorization", auth)
        try:
            r = urlopen(req, timeout=3)
            return r.status, json.loads(r.read())
        except HTTPError as e:
            return e.code, None

    def test_disabled_without_token_is_404(self):
        # mutation: exposing the endpoint when no operator token is configured
        self.assertEqual(self._get(self._serve(stats_token=None), "Bearer x")[0], 404)

    def test_wrong_or_missing_token_is_401(self):
        # mutation: accepting a missing or mismatched bearer token
        port = self._serve(stats_token="s3cret")
        self.assertEqual(self._get(port, None)[0], 401)
        self.assertEqual(self._get(port, "Bearer nope")[0], 401)

    def _raw_get(self, port, auth_bytes=None):
        """Send a request with an arbitrary (possibly non-ASCII) Authorization
        header via a raw socket, since urllib rejects non-token header values.
        Returns (status_int_or_None, body_str)."""
        import socket
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        hdr = (b"Authorization: Bearer " + auth_bytes + b"\r\n") if auth_bytes is not None else b""
        s.sendall(b"GET /v1/stats HTTP/1.1\r\nHost: x\r\n" + hdr + b"Connection: close\r\n\r\n")
        s.settimeout(3)
        data = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        s.close()
        if not data:
            return None, ""  # broken connection -> handler thread crashed
        line = data.split(b"\r\n", 1)[0].decode(errors="replace")
        try:
            status = int(line.split()[1])
        except (IndexError, ValueError):
            status = None
        body = data.split(b"\r\n\r\n", 1)[1].decode(errors="replace") if b"\r\n\r\n" in data else ""
        return status, body

    def test_non_ascii_bearer_is_clean_401_not_crash(self):
        # mutation: hmac.compare_digest on str raises TypeError for non-ASCII;
        # an attacker-supplied non-ASCII bearer must be a plain mismatch (401),
        # never an unhandled crash (empty/broken response + traceback to logs).
        port = self._serve(stats_token="asciitoken123")
        status, body = self._raw_get(port, "éattacker".encode("utf-8"))
        self.assertEqual(status, 401, "non-ASCII bearer must reject with 401, got %r" % status)
        self.assertNotIn("Traceback", body)

    def test_non_ascii_configured_token_still_authenticates(self):
        # a non-ASCII operator token must not brick the endpoint: the matching
        # token authenticates (200), a mismatch is 401 -- no TypeError either way.
        # (http.server decodes header bytes as latin-1, so the client must put
        # the token on the wire the same way for the bytes to round-trip; this
        # is inherent to HTTP header charset, not to the auth check.)
        port = self._serve(stats_token="sécret", ledger=None)
        status_ok, body = self._raw_get(port, "sécret".encode("iso-8859-1"))
        self.assertEqual(status_ok, 200, "matching non-ASCII token must auth, got %r" % status_ok)
        self.assertNotIn("Traceback", body)
        status_bad, _ = self._raw_get(port, b"wrong")
        self.assertEqual(status_bad, 401)

    def test_authed_returns_stats(self):
        import os
        import tempfile
        import ledger as L
        led = L.EventLedger(os.path.join(tempfile.mkdtemp(), "l.jsonl"))
        led.record_verdict("r1", "0xA", "0.09", "GO", payer="0xP1")
        led.record_verdict("r2", "0xB", "0.09", "STOP", payer="0xP2")
        code, body = self._get(self._serve(stats_token="s3cret", ledger=led), "Bearer s3cret")
        self.assertEqual(code, 200)
        self.assertEqual(body["verdicts_total"], 2)
        self.assertEqual(body["by_verdict"], {"GO": 1, "STOP": 1})
        self.assertEqual(body["distinct_payers"], 2)

    def test_stats_reports_watch_liveness_when_running(self):
        # The daemon watcher is otherwise invisible (web serves 200 either way).
        # /v1/stats must surface its liveness so an operator can confirm it's alive.
        ws = bw.WatchStats()
        ws.record_pass(2, "2026-07-08T12:00:00Z")   # one pass, 2 confirmed
        code, body = self._get(
            self._serve(stats_token="s3cret", watch_stats=ws), "Bearer s3cret")
        self.assertEqual(code, 200)
        self.assertTrue(body["settlement_watch_enabled"])
        self.assertEqual(body["settlement_watch_passes"], 1)
        self.assertEqual(body["settlement_watch_confirmations_total"], 2)
        self.assertEqual(body["settlement_watch_last_pass_ts"], "2026-07-08T12:00:00Z")

    def test_stats_reports_watch_disabled_when_off(self):
        # Mutation: claiming the flywheel is on when no watcher is wired.
        code, body = self._get(self._serve(stats_token="s3cret"), "Bearer s3cret")
        self.assertEqual(code, 200)
        self.assertFalse(body["settlement_watch_enabled"])


class TestCORS(unittest.TestCase):
    """The verdict endpoint is a public API; a browser client (e.g. the Farcaster
    mini-app) needs CORS or its cross-origin fetch is blocked at the preflight.
    Kills: no do_OPTIONS handler (501 preflight) / missing Access-Control-Allow-Origin."""

    def _serve(self):
        import threading
        from http.server import ThreadingHTTPServer
        handler = type("_H", (bw._Handler,),
                       {"reputation_source": bw.MockReputationSource()})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return port

    def test_options_preflight_allows_cross_origin_post(self):
        from urllib.request import Request, urlopen
        port = self._serve()
        req = Request("http://127.0.0.1:%d/v1/forecast-payment" % port,
                      method="OPTIONS")
        r = urlopen(req, timeout=3)
        self.assertIn(r.status, (200, 204))   # NOT 501 (the stdlib default)
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "*")
        self.assertIn("POST", r.headers.get("Access-Control-Allow-Methods", ""))
        self.assertIn("X-PAYMENT", r.headers.get("Access-Control-Allow-Headers", ""))

    def test_json_response_carries_cors_header(self):
        from urllib.request import urlopen
        port = self._serve()
        r = urlopen("http://127.0.0.1:%d/healthz" % port, timeout=3)
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "*")

    def test_preflight_allows_the_request_headers_the_service_actually_reads(self):
        """A browser wallet's cross-origin paid call sends the x402 REQUEST headers
        the server consumes: X-PAYMENT (single-shot), X-PAYMENT-SESSION (session
        reuse), and PAYMENT-SIGNATURE (v2 canonical). All three MUST be in
        Access-Control-Allow-Headers or the browser blocks the request at the
        preflight -- defeating the whole point of adding CORS.
        Kills: Allow-Headers advertising a phantom `X-SESSION` while omitting the
        real X-PAYMENT-SESSION / PAYMENT-SIGNATURE names."""
        from urllib.request import Request, urlopen
        port = self._serve()
        r = urlopen(Request("http://127.0.0.1:%d/v1/forecast-payment" % port,
                            method="OPTIONS"), timeout=3)
        allow = r.headers.get("Access-Control-Allow-Headers", "").lower()
        for h in ("x-payment", "x-payment-session", "payment-signature"):
            self.assertIn(h, allow,
                          "Allow-Headers must include %r (the service reads it)" % h)

    def test_preflight_exposes_the_response_headers_browser_js_must_read(self):
        """The paid-call RESPONSE carries x402 headers browser JS needs to read
        back (the settlement tx in PAYMENT-RESPONSE, the session balance in
        X-PAYMENT-SESSION-REMAINING). Without Access-Control-Expose-Headers the
        browser hides them from JS.
        Kills: no Access-Control-Expose-Headers on the CORS set."""
        from urllib.request import urlopen
        port = self._serve()
        r = urlopen("http://127.0.0.1:%d/healthz" % port, timeout=3)
        expose = r.headers.get("Access-Control-Expose-Headers", "").lower()
        for h in ("payment-response", "x-payment-session-remaining"):
            self.assertIn(h, expose,
                          "Expose-Headers must include %r (browser JS reads it)" % h)


class TestSignedReceiptForecast(unittest.TestCase):
    """forecast() attaches a third-party-verifiable Ed25519 signed_receipt."""

    def setUp(self):
        self.src = bw.MockReputationSource()
        self.payload = {"counterparty": "0xKNOWNGOOD000000000000000000000000000001",
                        "amount": "0.09", "asset": "USDC", "chain": "base"}

    def test_signed_receipt_present_and_verifies(self):
        import receipt_sig
        resp, err = bw.forecast(self.payload, self.src)
        self.assertIsNone(err)
        sr = resp.get("signed_receipt")
        self.assertIsNotNone(sr)
        # mutation: emitting a receipt that does not actually verify.
        self.assertTrue(receipt_sig.verify_signed_receipt(sr, receipt_sig.public_key_hex()))
        # mutation: signing an identity/decision that differs from what was returned.
        self.assertEqual(sr["verdict"], resp["verdict"])
        self.assertEqual(sr["counterparty"], self.payload["counterparty"])
        self.assertEqual(sr["receipt_id"], resp["receipt_id"])

    def test_signed_receipt_binds_verdict(self):
        # mutation: signing a subset that omits the verdict -- flipping it must break verify.
        import receipt_sig
        resp, _ = bw.forecast(self.payload, self.src)
        sr = dict(resp["signed_receipt"])
        sr["verdict"] = "STOP" if sr["verdict"] != "STOP" else "GO"
        self.assertFalse(receipt_sig.verify_signed_receipt(sr, receipt_sig.public_key_hex()))

    def test_fail_open_when_signing_unavailable(self):
        # mutation: letting a signing error break the verdict. Signing is a
        # best-effort add-on; the decision must still be returned.
        import receipt_sig
        from unittest import mock
        with mock.patch.object(receipt_sig, "build_signed_receipt",
                               side_effect=RuntimeError("boom")):
            resp, err = bw.forecast(self.payload, self.src)
        self.assertIsNone(err)
        self.assertEqual(resp["verdict"], "GO")
        self.assertNotIn("signed_receipt", resp)  # omitted, not crashed


class TestReceiptKeyEndpoint(unittest.TestCase):
    """GET /.well-known/blackwall-receipt-key.json publishes the verify key."""

    def _serve(self):
        import threading
        from http.server import ThreadingHTTPServer
        handler = type("_H", (bw._Handler,),
                       {"reputation_source": bw.MockReputationSource()})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return port

    def _get(self, port, path):
        from urllib.request import urlopen
        r = urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=3)
        return r.status, json.loads(r.read())

    def test_publishes_key_that_verifies_a_real_receipt(self):
        # The full third-party flow: fetch the published key over HTTP, then use
        # it to verify a receipt the server actually produced.
        # mutation: publishing a key that cannot verify our own receipts.
        import receipt_sig
        port = self._serve()
        status, body = self._get(port, "/.well-known/blackwall-receipt-key.json")
        self.assertEqual(status, 200)
        self.assertEqual(body["algo"], "ed25519")
        self.assertEqual(body["public_key"], receipt_sig.public_key_hex())
        # mutation: not surfacing whether the forgeable dev key is in use.
        self.assertEqual(body["dev_key"], receipt_sig.is_dev_key())
        resp, _ = bw.forecast(
            {"counterparty": "0xKNOWNGOOD000000000000000000000000000001",
             "amount": "0.09", "asset": "USDC", "chain": "base"},
            bw.MockReputationSource())
        self.assertTrue(receipt_sig.verify_signed_receipt(
            resp["signed_receipt"], body["public_key"]))


class TestVelocityAggregate(unittest.TestCase):
    """decide_payment velocity signal: cumulative flow to ONE payee escalates GO->HOLD
    -- the drain-by-many-small-payments a per-call verdict and a per-invoice cap miss."""

    def _good_record(self):
        return {"settlement_count": 100, "dispute_rate": 0.0,
                "confirmed_settlement_count": 100, "distinct_payers": 10}

    def test_baseline_goes_without_velocity(self):
        v = bw.decide_payment(Decimal("5"), self._good_record(), [], counterparty="0xGOOD")
        self.assertEqual(v["verdict"], "GO")

    def test_velocity_escalates_go_to_hold(self):
        # Kills: not gating GO on cumulative flow -- the whole feature.
        flow = {"total_amount": Decimal("98"), "count": 5}   # +this = 103 across 6 payments
        v = bw.decide_payment(Decimal("5"), self._good_record(), [],
                              counterparty="0xGOOD", velocity_flow=flow)
        self.assertEqual(v["verdict"], "HOLD")
        self.assertTrue(any("cumulative flow" in r for r in v["reasons"]))
        self.assertEqual(v["signals"]["counterparty_flow"]["count"], 6)

    def test_velocity_needs_the_pattern_not_just_total(self):
        # Kills: tripping on total alone -- a single big payment is the over_budget
        # case, not a velocity drain. High total + few payments must NOT trip velocity.
        flow = {"total_amount": Decimal("500"), "count": 1}   # +this = 505 but only 2 payments
        v = bw.decide_payment(Decimal("5"), self._good_record(), [],
                              counterparty="0xGOOD", velocity_flow=flow)
        self.assertEqual(v["verdict"], "GO")

    def test_under_threshold_goes(self):
        flow = {"total_amount": Decimal("10"), "count": 5}    # +this = 15 < threshold
        v = bw.decide_payment(Decimal("5"), self._good_record(), [],
                              counterparty="0xGOOD", velocity_flow=flow)
        self.assertEqual(v["verdict"], "GO")

    def test_no_velocity_flow_is_fail_open(self):
        # Kills: crashing / mis-signalling when no ledger supplies a flow.
        v = bw.decide_payment(Decimal("5"), self._good_record(), [],
                              counterparty="0xGOOD", velocity_flow=None)
        self.assertEqual(v["verdict"], "GO")
        self.assertIsNone(v["signals"]["counterparty_flow"])


class TestPayerOutflow(unittest.TestCase):
    """decide_payment payer-side fan-out (SYBIL complement): high TOTAL outflow AND
    high DISTINCT-payee count escalates GO->HOLD -- the distributed drain that
    splitting across fresh addresses (which defeats the per-counterparty velocity)
    actually makes MORE visible."""

    def _good_record(self):
        return {"settlement_count": 100, "dispute_rate": 0.0,
                "confirmed_settlement_count": 100, "distinct_payers": 10}

    def test_fanout_escalates_go_to_hold(self):
        # Kills: not gating GO on payer fan-out -- the whole sybil complement.
        # +this = 104 across a 5th distinct payee -> trips.
        flow = {"total_amount": Decimal("99"), "count": 4, "distinct_counterparties": 5}
        v = bw.decide_payment(Decimal("5"), self._good_record(), [],
                              counterparty="0xNEW", payer_flow=flow)
        self.assertEqual(v["verdict"], "HOLD")
        self.assertTrue(any("distinct counterparties" in r for r in v["reasons"]))
        self.assertEqual(v["signals"]["payer_outflow"]["distinct_counterparties"], 5)
        self.assertEqual(v["signals"]["payer_outflow"]["window_total"], "104")

    def test_high_spend_low_fanout_does_not_trip(self):
        # Kills: turning this into a raw spend cap. A legit agent paying a FEW known
        # vendors a lot has high outflow but LOW fan-out -- must NOT trip here (that's
        # the per-counterparty / over_budget case).
        flow = {"total_amount": Decimal("500"), "count": 50, "distinct_counterparties": 2}
        v = bw.decide_payment(Decimal("5"), self._good_record(), [],
                              counterparty="0xNEW", payer_flow=flow)
        self.assertEqual(v["verdict"], "GO")

    def test_high_fanout_low_total_does_not_trip(self):
        # Kills: tripping on fan-out alone. Many tiny payments to many payees under
        # the total threshold is normal micro-payment behavior, not a drain.
        flow = {"total_amount": Decimal("20"), "count": 10, "distinct_counterparties": 10}
        v = bw.decide_payment(Decimal("5"), self._good_record(), [],
                              counterparty="0xNEW", payer_flow=flow)
        self.assertEqual(v["verdict"], "GO")

    def test_no_payer_flow_is_fail_open(self):
        # Kills: crashing / mis-signalling when no ledger supplies a payer flow.
        v = bw.decide_payment(Decimal("5"), self._good_record(), [],
                              counterparty="0xNEW", payer_flow=None)
        self.assertEqual(v["verdict"], "GO")
        self.assertIsNone(v["signals"]["payer_outflow"])

    def test_trips_exactly_at_total_threshold(self):
        # Kills: off-by-one on the outflow threshold (>= -> >). At EXACTLY $100
        # window-total (99 history + 1 this payment) with 5 distinct payees the
        # gate must trip; a `>` mutation would let the boundary payment through.
        flow = {"total_amount": Decimal("99"), "count": 4, "distinct_counterparties": 5}
        v = bw.decide_payment(Decimal("1"), self._good_record(), [],
                              counterparty="0xNEW", payer_flow=flow)
        self.assertEqual(v["signals"]["payer_outflow"]["window_total"], "100")
        self.assertEqual(v["verdict"], "HOLD")

    def test_just_under_total_threshold_stays_go(self):
        # Kills: off-by-one the other way. Under $100 window-total with 5 payees
        # must NOT trip -- pins the boundary from below so HOLD is exactly at $100.
        flow = {"total_amount": Decimal("94.99"), "count": 4, "distinct_counterparties": 5}
        v = bw.decide_payment(Decimal("5"), self._good_record(), [],
                              counterparty="0xNEW", payer_flow=flow)
        self.assertEqual(v["signals"]["payer_outflow"]["window_total"], "99.99")
        self.assertEqual(v["verdict"], "GO")

    def test_trips_exactly_at_distinct_threshold(self):
        # Kills: off-by-one on the distinct-payee count. Exactly MIN distinct (5)
        # with over-threshold total must trip; 4 distinct must not.
        over = {"total_amount": Decimal("200"), "count": 10, "distinct_counterparties": 5}
        v = bw.decide_payment(Decimal("5"), self._good_record(), [],
                              counterparty="0xNEW", payer_flow=over)
        self.assertEqual(v["verdict"], "HOLD")
        under = {"total_amount": Decimal("200"), "count": 10, "distinct_counterparties": 4}
        v2 = bw.decide_payment(Decimal("5"), self._good_record(), [],
                               counterparty="0xNEW", payer_flow=under)
        self.assertEqual(v2["verdict"], "GO")

    def test_velocity_and_payer_are_independent_gates(self):
        # Neither trips alone here (velocity: few payments; payer: low fan-out), so GO.
        # Confirms the two escalators don't accidentally alias each other.
        vel = {"total_amount": Decimal("500"), "count": 1}
        pay = {"total_amount": Decimal("500"), "count": 50, "distinct_counterparties": 2}
        v = bw.decide_payment(Decimal("5"), self._good_record(), [],
                              counterparty="0xNEW", velocity_flow=vel, payer_flow=pay)
        self.assertEqual(v["verdict"], "GO")


class _FakeWatcher:
    """confirm_pending stub that stops the loop after `stop_after` calls and can
    be told to raise on specific call numbers (to prove fail-safe)."""
    def __init__(self, stop_after, stop_event, raise_on=None, ret=0):
        self.calls = 0
        self.limits = []
        self.stop_after = stop_after
        self.stop_event = stop_event
        self.raise_on = set(raise_on or ())
        self.ret = ret

    def confirm_pending(self, limit=None):
        self.calls += 1
        self.limits.append(limit)
        if self.calls >= self.stop_after:
            self.stop_event.set()   # let the loop exit after this pass
        if self.calls in self.raise_on:
            raise RuntimeError("chain indexer down")
        return self.ret


class TestRunWatchLoop(unittest.TestCase):
    """run_watch_loop: the settlement-watch background loop -- must keep running
    across bad passes and stop promptly when signalled."""

    def test_runs_until_stop_and_passes_batch(self):
        # Kills: not looping / not honoring the stop_event / not passing the batch
        # limit through to confirm_pending.
        ev = threading.Event()
        w = _FakeWatcher(stop_after=2, stop_event=ev)
        passes = bw.run_watch_loop(w, 1, 25, ev)
        self.assertEqual(w.calls, 2)
        self.assertEqual(passes, 2)
        self.assertTrue(all(l == 25 for l in w.limits))  # batch forwarded

    def test_exception_in_pass_is_swallowed_not_fatal(self):
        # Kills: letting a chain/indexer outage crash the loop (and the process it
        # runs beside). Pass 1 raises; the loop must continue to pass 2.
        ev = threading.Event()
        w = _FakeWatcher(stop_after=2, stop_event=ev, raise_on={1})
        passes = bw.run_watch_loop(w, 1, 25, ev)  # must NOT raise
        self.assertEqual(w.calls, 2)
        self.assertEqual(passes, 2)

    def test_already_stopped_runs_zero_passes(self):
        # Kills: running a pass even when told to stop before starting.
        ev = threading.Event()
        ev.set()
        w = _FakeWatcher(stop_after=99, stop_event=ev)
        passes = bw.run_watch_loop(w, 1, 25, ev)
        self.assertEqual(passes, 0)
        self.assertEqual(w.calls, 0)

    def test_failing_log_on_error_path_does_not_kill_loop(self):
        # Kills: leaving the except-branch log() UNGUARDED. The live deploy logs
        # to sys.stdout; a broken/closed stdout (broken pipe, stream closed during
        # shutdown or log-rotation) makes log() raise. If that raise escapes, the
        # daemon thread dies SILENTLY and settlement confirmation stops with no
        # crash the operator sees -- worse than a loud failure for a trustless
        # read-back. The loop must survive a logging failure on the ERROR path.
        ev = threading.Event()
        w = _FakeWatcher(stop_after=2, stop_event=ev, raise_on={1})

        def bad_log(_m):
            raise BrokenPipeError("stdout gone")

        passes = bw.run_watch_loop(w, 1, 25, ev, log=bad_log)  # must NOT raise
        self.assertEqual(w.calls, 2)
        self.assertEqual(passes, 2)

    def test_failing_log_on_success_path_does_not_kill_loop(self):
        # Kills: leaving the success-branch log() unguarded. A confirmation pass
        # succeeds (n>0) and logs; if that log() raises it is caught by the
        # except, which logs AGAIN and re-raises -> thread dies. The loop must
        # survive a logging failure on the SUCCESS path too.
        ev = threading.Event()
        w = _FakeWatcher(stop_after=2, stop_event=ev, ret=3)  # n>0 -> logs

        def bad_log(_m):
            raise BrokenPipeError("stdout gone")

        passes = bw.run_watch_loop(w, 1, 25, ev, log=bad_log)  # must NOT raise
        self.assertEqual(w.calls, 2)
        self.assertEqual(passes, 2)


class TestWatchStats(unittest.TestCase):
    """WatchStats: the settlement-watch liveness surfaced on /v1/stats."""

    def test_records_passes_and_confirmations(self):
        ws = bw.WatchStats()
        ws.record_pass(2, "t1")
        ws.record_pass(0, "t2")
        ws.record_pass(3, "t3")
        snap = ws.snapshot()
        self.assertEqual(snap["settlement_watch_passes"], 3)
        self.assertEqual(snap["settlement_watch_confirmations_total"], 5)  # 2+0+3
        self.assertEqual(snap["settlement_watch_last_pass_ts"], "t3")
        self.assertEqual(snap["settlement_watch_last_confirmed"], 3)
        self.assertTrue(snap["settlement_watch_enabled"])

    def test_error_pass_does_not_inflate_confirmations(self):
        # Kills: counting a failed pass as confirmations, or not surfacing the error.
        # NOTE: passes a NON-ZERO `confirmed` alongside the error so the test also
        # kills the mutation "add confirmed even when error is set" -- a
        # confirmed=0 error pass can't distinguish that mutation.
        ws = bw.WatchStats()
        ws.record_pass(2, "t1")
        ws.record_pass(5, "t2", error=RuntimeError("chain down"))  # nonzero + error
        snap = ws.snapshot()
        self.assertEqual(snap["settlement_watch_passes"], 2)
        self.assertEqual(snap["settlement_watch_confirmations_total"], 2)  # NOT 7
        self.assertEqual(snap["settlement_watch_last_confirmed"], 2)  # not clobbered
        self.assertIn("chain down", snap["settlement_watch_last_error"])

    def test_loop_updates_stats_each_pass(self):
        # Kills: the loop not feeding the liveness stat (flywheel invisible).
        ev = threading.Event()
        w = _FakeWatcher(stop_after=2, stop_event=ev, ret=1)
        ws = bw.WatchStats()
        ticks = iter(["ts1", "ts2", "ts3", "ts4"])
        bw.run_watch_loop(w, 1, 25, ev, stats=ws, now_fn=lambda: next(ticks))
        snap = ws.snapshot()
        self.assertEqual(snap["settlement_watch_passes"], 2)
        self.assertEqual(snap["settlement_watch_confirmations_total"], 2)  # 1+1
        self.assertEqual(snap["settlement_watch_last_pass_ts"], "ts2")

    def test_loop_records_error_pass_to_stats(self):
        ev = threading.Event()
        w = _FakeWatcher(stop_after=1, stop_event=ev, raise_on={1})
        ws = bw.WatchStats()
        bw.run_watch_loop(w, 1, 25, ev, stats=ws, now_fn=lambda: "t")
        snap = ws.snapshot()
        self.assertEqual(snap["settlement_watch_passes"], 1)
        self.assertEqual(snap["settlement_watch_confirmations_total"], 0)
        self.assertIsNotNone(snap["settlement_watch_last_error"])

    def test_failing_stats_does_not_kill_loop(self):
        # Kills: letting a stats-update error escape the fail-safe boundary.
        ev = threading.Event()
        w = _FakeWatcher(stop_after=2, stop_event=ev, ret=1)

        class BadStats:
            def record_pass(self, *a, **k):
                raise RuntimeError("stats broken")

        passes = bw.run_watch_loop(w, 1, 25, ev, stats=BadStats(),
                                   now_fn=lambda: "t")  # must NOT raise
        self.assertEqual(passes, 2)


class TestEnvInt(unittest.TestCase):
    """_env_int: tolerant int env parse -- a typo must not crash boot (esp. for a
    dormant feature's tuning vars)."""

    def setUp(self):
        import os
        self._os = os
        self._saved = os.environ.get("BW_TEST_INT")

    def tearDown(self):
        if self._saved is None:
            self._os.environ.pop("BW_TEST_INT", None)
        else:
            self._os.environ["BW_TEST_INT"] = self._saved

    def test_missing_uses_default(self):
        self._os.environ.pop("BW_TEST_INT", None)
        self.assertEqual(bw._env_int("BW_TEST_INT", 300), 300)

    def test_valid_parsed(self):
        self._os.environ["BW_TEST_INT"] = "42"
        self.assertEqual(bw._env_int("BW_TEST_INT", 300), 42)

    def test_garbage_falls_back_not_raises(self):
        # Kills: an eager int() that crashes boot on a non-numeric tuning var.
        self._os.environ["BW_TEST_INT"] = "not-a-number"
        self.assertEqual(bw._env_int("BW_TEST_INT", 25), 25)

    def test_empty_uses_default(self):
        self._os.environ["BW_TEST_INT"] = ""
        self.assertEqual(bw._env_int("BW_TEST_INT", 25), 25)


class TestSettlementWatchEnvFlag(unittest.TestCase):
    """BLACKWALL_SETTLEMENT_WATCH must follow the repo-wide _env_flag convention:
    "0"/"false"/"no"/"off"/unset => OFF. `bool(os.environ.get(...))` is the trap
    _env_flag exists to prevent (bool("0") is True), and it already 502'd this
    deploy once via BLACKWALL_INGEST -- so the OFF switch for a background thread
    that makes outbound chain calls and WRITES chain-confirmed outcomes into the
    verdict-feeding ledger must not repeat it.

    Observed through main()'s boot: with no --ledger, an ENABLED watch prints the
    "--settlement-watch set but no --ledger" warning and a DISABLED one is silent.
    Mutation: revert the default to bool(os.environ.get(...)) -> "0" boots the
    watcher and this fails."""

    _VARS = ("BLACKWALL_SETTLEMENT_WATCH", "BLACKWALL_LEDGER", "BLACKWALL_STORE",
             "BLACKWALL_SANCTIONS", "BLACKWALL_PAY_TO")

    def setUp(self):
        import os
        self._os = os
        self._saved = {k: os.environ.get(k) for k in self._VARS}
        for k in self._VARS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                self._os.environ.pop(k, None)
            else:
                self._os.environ[k] = v

    def _boot_stderr(self, value):
        """Run main() to the point of serving with BLACKWALL_SETTLEMENT_WATCH=value
        and return what it wrote to stderr. The server is stubbed out so nothing
        binds a socket and no watch thread can reach the network."""
        import contextlib
        import io

        class _NoServe:
            def __init__(self, **kwargs):
                pass

            def serve_forever(self):
                raise KeyboardInterrupt

            def shutdown(self):
                pass

        self._os.environ["BLACKWALL_SETTLEMENT_WATCH"] = value
        real = bw.BlackwallServer
        bw.BlackwallServer = _NoServe
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err), \
                    contextlib.redirect_stdout(io.StringIO()):
                bw.main(["--host", "127.0.0.1", "--port", "0"])
        finally:
            bw.BlackwallServer = real
        return err.getvalue()

    def test_falsey_values_do_not_enable_the_watcher(self):
        for v in ("0", "false", "no", "off", ""):
            self.assertNotIn("settlement-watch", self._boot_stderr(v),
                             "BLACKWALL_SETTLEMENT_WATCH=%r must leave the "
                             "watcher OFF" % v)

    def test_truthy_values_enable_the_watcher(self):
        for v in ("1", "true", "TRUE", "yes", "on"):
            self.assertIn("settlement-watch", self._boot_stderr(v),
                          "BLACKWALL_SETTLEMENT_WATCH=%r must enable the "
                          "watcher" % v)


class TestReceiptSigningBootWarning(unittest.TestCase):
    """Boot must be LOUD about the two ways public receipt signing is silently
    wrong, because nothing else is: forecast()'s signing is fail-open (a bad seed
    just drops `signed_receipt` with no error) while the discovery descriptor
    keeps advertising third-party-verifiable receipts either way.

      * seed UNSET  -> the built-in all-zero DEV seed signs. Its public key is
        published, so ANYONE can forge a "Black_Wall attested GO" receipt.
      * seed MALFORMED -> signing raises and is swallowed; no receipt is signed
        at all.

    And the warning must never echo the seed itself.
    Mutation: delete the boot check -> a dev-key/broken-seed deploy boots clean."""

    _VARS = ("BLACKWALL_SIGNING_SEED", "BLACKWALL_SETTLEMENT_WATCH",
             "BLACKWALL_LEDGER", "BLACKWALL_STORE", "BLACKWALL_SANCTIONS",
             "BLACKWALL_PAY_TO")

    def setUp(self):
        import os
        self._os = os
        self._saved = {k: os.environ.get(k) for k in self._VARS}
        for k in self._VARS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                self._os.environ.pop(k, None)
            else:
                self._os.environ[k] = v

    def _boot(self, seed=None):
        import contextlib
        import io

        class _NoServe:
            def __init__(self, **kwargs):
                pass

            def serve_forever(self):
                raise KeyboardInterrupt

            def shutdown(self):
                pass

        if seed is None:
            self._os.environ.pop("BLACKWALL_SIGNING_SEED", None)
        else:
            self._os.environ["BLACKWALL_SIGNING_SEED"] = seed
        real = bw.BlackwallServer
        bw.BlackwallServer = _NoServe
        err, out = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                bw.main(["--host", "127.0.0.1", "--port", "0"])
        finally:
            bw.BlackwallServer = real
        return err.getvalue(), out.getvalue()

    def test_unset_seed_warns_receipts_are_forgeable(self):
        err, _out = self._boot(None)
        self.assertIn("FORGEABLE", err)
        self.assertIn("BLACKWALL_SIGNING_SEED", err)

    def test_real_seed_is_quiet_and_reports_the_key_id(self):
        from receipt_sig import key_id, public_key_hex
        seed = "11" * 32
        err, out = self._boot(seed)
        self.assertNotIn("FORGEABLE", err)
        self.assertIn(key_id(public_key_hex(seed)), out)
        # the SECRET must never appear in any boot output
        self.assertNotIn(seed, err + out)

    def test_malformed_seed_warns_signing_disabled_without_echoing_it(self):
        bad = "deadbeef"          # valid hex, wrong length -> load_seed raises
        err, out = self._boot(bad)
        self.assertIn("signing DISABLED", err)
        self.assertNotIn(bad, err + out)


if __name__ == "__main__":
    unittest.main()


class TestEnvFlag(unittest.TestCase):
    """Regression: bool(os.environ.get()) treats "0" as True, so a flag set to "0"
    could never be disabled -- that 502'd the deploy (INGEST=0 stayed on)."""
    def test_zero_and_false_are_off(self):
        import os
        for v in ("0", "false", "no", "off", "", "FALSE"):
            os.environ["BLACKWALL_TESTFLAG"] = v
            self.assertFalse(bw._env_flag("BLACKWALL_TESTFLAG"), "%r should be off" % v)
        os.environ.pop("BLACKWALL_TESTFLAG", None)
        self.assertFalse(bw._env_flag("BLACKWALL_TESTFLAG"))   # unset -> off

    def test_truthy_values_are_on(self):
        import os
        for v in ("1", "true", "TRUE", "yes", "on"):
            os.environ["BLACKWALL_TESTFLAG"] = v
            self.assertTrue(bw._env_flag("BLACKWALL_TESTFLAG"), "%r should be on" % v)
        os.environ.pop("BLACKWALL_TESTFLAG", None)
