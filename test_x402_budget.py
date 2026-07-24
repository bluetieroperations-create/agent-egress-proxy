"""
Unit tests for x402_budget pure security-boundary functions.

Run: python -m unittest test_x402_budget.py -v

These functions ARE the money-lock security boundary; tests are TDD-first and
mirror test_egress_proxy.py. Each class notes the mutation it kills. The money
edge cases (boundary at a limit, one atomic unit over, zero/negative amounts,
excess precision) are the analog of the dot-boundary edge cases in host_allowed.
"""
import base64
import json
import unittest

import x402_budget as xb


def b64(obj):
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


class TestToAtomic(unittest.TestCase):
    """
    to_atomic: decimal money string -> integer atomic units.

    Mutations killed:
      - Using float() anywhere -> the 0.10 / 0.07 exactness tests FAIL.
      - Dropping the excess-precision check -> test_reject_excess_precision FAILS.
      - Truncating instead of padding fractional -> test_partial_fraction FAILS.
    """

    def test_whole(self):
        self.assertEqual(xb.to_atomic("2", 6), 2_000_000)

    def test_dime_exact(self):
        self.assertEqual(xb.to_atomic("0.10", 6), 100_000)

    def test_seven_cents_exact(self):
        # 0.07 is unrepresentable in float; integer path must be exact.
        self.assertEqual(xb.to_atomic("0.07", 6), 70_000)

    def test_partial_fraction(self):
        self.assertEqual(xb.to_atomic("1.5", 6), 1_500_000)

    def test_leading_dot(self):
        self.assertEqual(xb.to_atomic(".5", 2), 50)

    def test_full_precision(self):
        self.assertEqual(xb.to_atomic("0.000001", 6), 1)

    def test_zero(self):
        self.assertEqual(xb.to_atomic("0", 6), 0)

    def test_decimals_zero(self):
        self.assertEqual(xb.to_atomic("42", 0), 42)

    def test_negative(self):
        self.assertEqual(xb.to_atomic("-0.10", 6), -100_000)

    # ---- rejections ----
    def test_reject_excess_precision(self):
        # More precision than the asset supports -> refuse (no silent truncation).
        with self.assertRaises(ValueError):
            xb.to_atomic("0.0000001", 6)

    def test_reject_non_string(self):
        with self.assertRaises(ValueError):
            xb.to_atomic(0.1, 6)

    def test_reject_garbage(self):
        for bad in ("abc", "1.2.3", "1,000", "$1", "", "  "):
            with self.assertRaises(ValueError):
                xb.to_atomic(bad, 6)


class TestFromAtomic(unittest.TestCase):
    def test_roundtrip(self):
        # from_atomic(to_atomic(s)) returns s in canonical form (already-canonical
        # inputs round-trip exactly).
        for s in ("0", "0.1", "0.07", "2", "1.5", "0.000001"):
            self.assertEqual(xb.from_atomic(xb.to_atomic(s, 6), 6), s)

    def test_known(self):
        self.assertEqual(xb.from_atomic(100_000, 6), "0.1")
        self.assertEqual(xb.from_atomic(2_000_000, 6), "2")
        self.assertEqual(xb.from_atomic(1, 6), "0.000001")
        self.assertEqual(xb.from_atomic(0, 6), "0")

    def test_decimals_zero(self):
        self.assertEqual(xb.from_atomic(42, 0), "42")


class TestParseAtomicInt(unittest.TestCase):
    """parse_atomic_int: an already-atomic amount string/int -> int | None."""

    def test_string(self):
        self.assertEqual(xb.parse_atomic_int("20000"), 20000)

    def test_int(self):
        self.assertEqual(xb.parse_atomic_int(20000), 20000)

    def test_zero(self):
        self.assertEqual(xb.parse_atomic_int("0"), 0)

    def test_reject_negative_int(self):
        self.assertIsNone(xb.parse_atomic_int(-5))

    def test_reject_decimal(self):
        self.assertIsNone(xb.parse_atomic_int("1.5"))

    def test_reject_bool(self):
        # bool is an int subclass; it must not sneak through as 0/1.
        self.assertIsNone(xb.parse_atomic_int(True))

    def test_reject_garbage(self):
        for bad in ("", "  ", "0x10", "1e3", "-1", "4a3", None, 1.0):
            self.assertIsNone(xb.parse_atomic_int(bad))


class TestResourceSplit(unittest.TestCase):
    def test_full_url(self):
        self.assertEqual(xb.resource_split("https://api.example.com/tool/search"),
                         ("api.example.com", "/tool/search"))

    def test_url_with_port(self):
        self.assertEqual(xb.resource_split("https://api.example.com:8443/x"),
                         ("api.example.com", "/x"))

    def test_url_no_path(self):
        self.assertEqual(xb.resource_split("https://api.example.com"),
                         ("api.example.com", "/"))

    def test_bare_path(self):
        self.assertEqual(xb.resource_split("/tool/search"),
                         (None, "/tool/search"))

    def test_none(self):
        self.assertEqual(xb.resource_split(None), (None, "/"))

    def test_case_folds_host(self):
        self.assertEqual(xb.resource_split("https://API.Example.COM/p")[0],
                         "api.example.com")


class TestParseX402Offer(unittest.TestCase):
    """
    parse_x402_offer: 402 body -> Offer | None (fail closed).

    Mutations killed:
      - Treating a non-atomic amount as valid -> test_reject_decimal_amount FAILS.
      - Not ignoring unknown fields -> test_extra_fields_ignored FAILS.
      - Accepting empty accepts[] -> test_reject_empty_accepts FAILS.
    """

    def _body(self, **over):
        offer = {"scheme": "exact", "network": "base",
                 "maxAmountRequired": "20000",
                 "asset": "0xUSDC", "payTo": "0xSELLER",
                 "resource": "https://api.example.com/tool/search"}
        offer.update(over)
        return json.dumps({"x402Version": 1, "accepts": [offer]}).encode()

    def test_valid(self):
        o = xb.parse_x402_offer(self._body())
        self.assertEqual(o.amount_atomic, 20000)
        self.assertEqual(o.asset, "0xusdc")
        self.assertEqual(o.network, "base")
        self.assertEqual(o.host, "api.example.com")
        self.assertEqual(o.path, "/tool/search")

    def test_extra_fields_ignored(self):
        o = xb.parse_x402_offer(self._body(mimeType="x", extra={"a": 1}))
        self.assertEqual(o.amount_atomic, 20000)

    def test_prefer_network(self):
        doc = {"x402Version": 1, "accepts": [
            {"network": "base", "maxAmountRequired": "1", "resource": "/a"},
            {"network": "solana", "maxAmountRequired": "2", "resource": "/a"},
        ]}
        o = xb.parse_x402_offer(json.dumps(doc).encode(), prefer_network="solana")
        self.assertEqual(o.amount_atomic, 2)

    def test_host_hint_for_bare_path(self):
        o = xb.parse_x402_offer(self._body(resource="/tool/x"),
                                host_hint="API.example.com")
        self.assertEqual(o.host, "api.example.com")
        self.assertEqual(o.path, "/tool/x")

    # ---- rejections ----
    def test_reject_decimal_amount(self):
        self.assertIsNone(xb.parse_x402_offer(self._body(maxAmountRequired="0.02")))

    def test_reject_empty_accepts(self):
        self.assertIsNone(xb.parse_x402_offer(b'{"x402Version":1,"accepts":[]}'))

    def test_reject_no_accepts(self):
        self.assertIsNone(xb.parse_x402_offer(b'{"x402Version":1}'))

    def test_reject_non_json(self):
        self.assertIsNone(xb.parse_x402_offer(b"not json <html>"))

    def test_reject_non_dict(self):
        self.assertIsNone(xb.parse_x402_offer(b'[1,2,3]'))

    def test_reject_oversize(self):
        big = b'{"accepts":[' + b'{"maxAmountRequired":"1"},' * 5000 + b'{}]}'
        self.assertIsNone(xb.parse_x402_offer(big))

    def test_reject_none(self):
        self.assertIsNone(xb.parse_x402_offer(None))

    def test_reject_bad_utf8(self):
        self.assertIsNone(xb.parse_x402_offer(b'\xff\xfe{"accepts":[]}'))


class TestExtractPaymentAmount(unittest.TestCase):
    """extract_payment_amount: base64 X-PAYMENT -> Payment | None."""

    def test_canonical_location(self):
        h = b64({"x402Version": 1, "scheme": "exact", "network": "base",
                 "payload": {"authorization": {"value": "20000"},
                             "signature": "0xabc"}})
        p = xb.extract_payment_amount(h)
        self.assertEqual(p.amount_atomic, 20000)
        self.assertEqual(p.network, "base")

    def test_toplevel_value(self):
        p = xb.extract_payment_amount(b64({"value": "500", "network": "base"}))
        self.assertEqual(p.amount_atomic, 500)

    def test_missing_padding_tolerated(self):
        h = b64({"payload": {"authorization": {"value": "7"}}}).rstrip("=")
        p = xb.extract_payment_amount(h)
        self.assertEqual(p.amount_atomic, 7)

    # ---- rejections ----
    def test_reject_bad_base64(self):
        self.assertIsNone(xb.extract_payment_amount("!!!not base64!!!"))

    def test_reject_no_amount(self):
        self.assertIsNone(xb.extract_payment_amount(b64({"payload": {}})))

    def test_reject_decimal_amount(self):
        self.assertIsNone(xb.extract_payment_amount(
            b64({"value": "0.02"})))

    def test_reject_empty(self):
        self.assertIsNone(xb.extract_payment_amount(""))
        self.assertIsNone(xb.extract_payment_amount(None))

    def test_reject_non_dict_json(self):
        self.assertIsNone(xb.extract_payment_amount(
            base64.b64encode(b'"just a string"').decode()))


class TestPolicyLoading(unittest.TestCase):
    def _doc(self):
        return {
            "asset": "USDC", "unit_decimals": 6,
            "networks": ["Base"],
            "limits": {"per_call_max": "0.10", "per_session_total": "2.00",
                       "per_day_total": "10.00", "prompt_above": "0.50"},
            "per_host": {"API.example.com": {"per_call_max": "0.25"}},
            "per_tool": {"/tool/search": {"per_call_max": "0.02"}},
        }

    def test_decimals_converted_once(self):
        pol = xb.policy_from_dict(self._doc())
        self.assertEqual(pol.global_limits["per_call_max"], 100_000)
        self.assertEqual(pol.global_limits["per_session_total"], 2_000_000)
        self.assertEqual(pol.per_host["api.example.com"]["per_call_max"], 250_000)
        self.assertEqual(pol.per_tool["/tool/search"]["per_call_max"], 20_000)

    def test_networks_normalized(self):
        pol = xb.policy_from_dict(self._doc())
        self.assertIn("base", pol.networks)

    def test_bad_amount_fails_closed(self):
        d = self._doc()
        d["limits"]["per_call_max"] = "ten dollars"
        self.assertIsNone(xb.policy_from_dict(d))

    def test_non_dict_fails_closed(self):
        self.assertIsNone(xb.policy_from_dict([1, 2, 3]))
        self.assertIsNone(xb.policy_from_dict(None))

    def test_missing_file_fails_closed(self):
        self.assertIsNone(xb.load_policy("/no/such/policy.json"))


class TestBudgetDecide(unittest.TestCase):
    """
    budget_decide: THE money gate. Enforcement truth.

    Mutations killed:
      - `>` vs `>=` at per_call_max -> the exact-boundary tests FAIL.
      - Dropping the no-limit fail-closed default -> test_no_limit_blocks FAILS.
      - Counting a blocked payment against the ledger -> covered in TestLedger.
      - Skipping the asset/network mismatch guard -> those tests FAIL.
    """

    def setUp(self):
        self.pol = xb.policy_from_dict({
            "unit_decimals": 6,
            "networks": ["base"],
            "limits": {"per_call_max": "0.10", "per_session_total": "2.00",
                       "per_day_total": "10.00", "prompt_above": "0.50"},
            "per_host": {"api.example.com": {"per_day_total": "5.00"}},
            "per_tool": {"/tool/search": {"per_call_max": "0.02"}},
        })

    def offer(self, amount, resource="https://api.example.com/other",
              network="base", asset="0xusdc"):
        return xb.parse_x402_offer(json.dumps({"accepts": [{
            "maxAmountRequired": str(amount), "network": network,
            "asset": asset, "payTo": "0xs", "resource": resource}]}).encode())

    def test_under_budget_allows(self):
        d = xb.budget_decide(self.offer(50_000), {}, self.pol)  # $0.05
        self.assertEqual(d.action, xb.ALLOW)

    def test_exact_per_call_boundary_allows(self):
        # exactly at $0.10 -> allowed (limit is inclusive; `>` not `>=`).
        d = xb.budget_decide(self.offer(100_000), {}, self.pol)
        self.assertEqual(d.action, xb.ALLOW)

    def test_one_unit_over_per_call_blocks(self):
        d = xb.budget_decide(self.offer(100_001), {}, self.pol)
        self.assertEqual((d.action, d.reason), (xb.BLOCK, "over-per-call"))

    def test_per_tool_overrides_host_and_global(self):
        # /tool/search caps at $0.02; $0.05 would pass global but fails tool.
        o = self.offer(50_000, resource="https://api.example.com/tool/search")
        d = xb.budget_decide(o, {}, self.pol)
        self.assertEqual((d.action, d.reason), (xb.BLOCK, "over-per-call"))

    def test_session_cap_crossed(self):
        # $0.08 with $1.95 already spent -> crosses $2.00 session cap.
        snap = {"session": 1_950_000}
        d = xb.budget_decide(self.offer(80_000), snap, self.pol)
        self.assertEqual((d.action, d.reason), (xb.BLOCK, "over-session"))

    def test_session_cap_exact_boundary_allows(self):
        snap = {"session": 1_950_000}   # +0.05 = exactly 2.00
        d = xb.budget_decide(self.offer(50_000), snap, self.pol)
        self.assertEqual(d.action, xb.ALLOW)

    def test_host_day_cap(self):
        snap = {"host": {"api.example.com": 4_990_000}}  # $4.99 of $5 host/day
        d = xb.budget_decide(self.offer(20_000), snap, self.pol)  # +$0.02
        self.assertEqual((d.action, d.reason), (xb.BLOCK, "over-host-day"))

    def test_prompt_band(self):
        # raise per_call so $0.60 passes the hard cap but trips prompt_above 0.50
        pol = xb.policy_from_dict({
            "unit_decimals": 6,
            "limits": {"per_call_max": "1.00", "prompt_above": "0.50"}})
        d = xb.budget_decide(self.offer(600_000), {}, pol)
        self.assertEqual((d.action, d.reason), (xb.PROMPT, "above-prompt-threshold"))

    def test_zero_amount_allows(self):
        d = xb.budget_decide(self.offer(0), {}, self.pol)
        self.assertEqual(d.action, xb.ALLOW)

    def test_asset_mismatch_blocks(self):
        pol = xb.policy_from_dict({
            "unit_decimals": 6, "asset_addresses": ["0xusdc"],
            "limits": {"per_call_max": "1.00"}})
        d = xb.budget_decide(self.offer(1, asset="0xDAI"), {}, pol)
        self.assertEqual((d.action, d.reason), (xb.BLOCK, "asset-mismatch"))

    def test_network_mismatch_blocks(self):
        d = xb.budget_decide(self.offer(1, network="ethereum"), {}, self.pol)
        self.assertEqual((d.action, d.reason), (xb.BLOCK, "network-mismatch"))

    def test_unknown_offer_blocks(self):
        d = xb.budget_decide(None, {}, self.pol)
        self.assertEqual((d.action, d.reason), (xb.BLOCK, "unknown-offer"))

    def test_no_policy_blocks(self):
        d = xb.budget_decide(self.offer(1), {}, None)
        self.assertEqual((d.action, d.reason), (xb.BLOCK, "no-policy"))

    def test_no_limit_blocks_by_default(self):
        # A resource with no applicable per_call_max, block_if_unlimited default.
        pol = xb.policy_from_dict({"unit_decimals": 6, "limits": {}})
        d = xb.budget_decide(self.offer(1), {}, pol)
        self.assertEqual((d.action, d.reason), (xb.BLOCK, "no-limit-set"))

    def test_no_limit_allows_when_opted_out(self):
        pol = xb.policy_from_dict({"unit_decimals": 6, "limits": {},
                                   "block_if_unlimited": False})
        d = xb.budget_decide(self.offer(1), {}, pol)
        self.assertEqual(d.action, xb.ALLOW)


class TestLedger(unittest.TestCase):
    """Ledger: settled-spend accounting, day-windowed, deterministic (now injected)."""

    def test_records_and_snapshots(self):
        led = xb.Ledger()
        led.record(20_000, "api.example.com", "/tool/search", ts=1000)
        led.record(30_000, "api.example.com", "/tool/other", ts=1001)
        snap = led.snapshot(now=1002)
        self.assertEqual(snap["session"], 50_000)
        self.assertEqual(snap["day"], 50_000)
        self.assertEqual(snap["host"]["api.example.com"], 50_000)
        self.assertEqual(snap["tool"]["/tool/search"], 20_000)

    def test_day_window_excludes_old(self):
        led = xb.Ledger()
        led.record(1_000_000, "h", "/p", ts=0)                 # long ago
        led.record(20_000, "h", "/p", ts=100_000)              # within window
        snap = led.snapshot(now=100_000)
        # session is cumulative (both), day window excludes the ts=0 entry
        self.assertEqual(snap["session"], 1_020_000)
        self.assertEqual(snap["day"], 20_000)

    def test_blocked_payment_not_recorded(self):
        # Discipline check: the gate never calls record(); only settlement does.
        led = xb.Ledger()
        offer = xb.parse_x402_offer(json.dumps({"accepts": [{
            "maxAmountRequired": "999999", "network": "base",
            "resource": "https://h/p"}]}).encode())
        pol = xb.policy_from_dict({"unit_decimals": 6,
                                   "limits": {"per_call_max": "0.10"}})
        d = xb.budget_decide(offer, led.snapshot(now=1), pol)
        self.assertEqual(d.action, xb.BLOCK)
        # ledger untouched by a decision
        self.assertEqual(led.snapshot(now=1)["session"], 0)

    def test_record_rejects_negative(self):
        led = xb.Ledger()
        with self.assertRaises(ValueError):
            led.record(-1, "h", "/p", ts=1)

    def test_prune_preserves_session_total(self):
        led = xb.Ledger()
        led.record(100, "h", "/p", ts=0)
        led.record(200, "h", "/p", ts=100_000)
        led.prune(now=100_000)                 # drops the ts=0 entry
        snap = led.snapshot(now=100_000)
        self.assertEqual(snap["session"], 300)  # cumulative total preserved
        self.assertEqual(snap["day"], 200)      # windowed reflects the prune


if __name__ == "__main__":
    unittest.main()
