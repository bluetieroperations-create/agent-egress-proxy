"""
Unit tests for blackwall pure decision-boundary functions + the forecast path.

Run: python -m unittest test_blackwall.py -v

The decision functions (parse_amount, price_anomaly_ratio, anomaly_score,
reputation_score, decide_payment) ARE the boundary; tests are TDD-first. Each
class notes the mutation it kills.
"""
import json
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


if __name__ == "__main__":
    unittest.main()
