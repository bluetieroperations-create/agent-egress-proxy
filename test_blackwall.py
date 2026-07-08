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
        # 8x the median -> STOP, not merely HOLD.
        v = bw.decide_payment("0.72", self.GOOD, self.STABLE_HISTORY,
                              counterparty="0xA")
        self.assertEqual(v["verdict"], "STOP")

    def test_hold_thin_history(self):
        thin = {"settlement_count": 3, "dispute_rate": 0.0}
        v = bw.decide_payment("0.09", thin, ["0.09", "0.09"], counterparty="0xA")
        self.assertEqual(v["verdict"], "HOLD")

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

    def _serve(self, stats_token, ledger=None):
        import threading
        from http.server import ThreadingHTTPServer
        handler = type("_H", (bw._Handler,),
                       {"reputation_source": bw.MockReputationSource(),
                        "ledger": ledger, "stats_token": stats_token})
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


if __name__ == "__main__":
    unittest.main()
