"""
Tests for ap_gate.py -- the treasury/AP payout gate.

Each test states the mutation it kills. Run:
    python -m unittest test_ap_gate.py -v
"""
import unittest

import ap_gate
from sanctions import SanctionsList, SanctionsScreeningSource

SANC = "0x0330070fd38ec3bb94f58fa55d40368271e9e54a"
ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _strong_record(unit_price):
    """A payee that looks great: 250 clean settlements from 40 distinct payers,
    stable price history at `unit_price`. (Uses the engine's real record keys:
    settlement_count / confirmed_settlement_count / dispute_rate.)"""
    return {"settlement_count": 250, "confirmed_settlement_count": 250,
            "dispute_rate": 0.0, "distinct_payers": 40,
            "price_history": [unit_price] * 20,
            "price_observations": [{"payer": f"0x{i:040x}", "amount": unit_price}
                                   for i in range(40)]}


class _StrongRep:
    """Strong payee with a small (~$5) price history -- a $5 payout matches and
    can auto-release; a wildly larger amount reads as a price gouge."""
    def lookup(self, cp):
        return _strong_record(5.0)


class _StrongRepBig:
    """Strong payee whose history is large ($5k) invoices -- so a $5k payout is
    in-line (not a gouge) but still trips the $10 amount-escalation floor."""
    def lookup(self, cp):
        return _strong_record(5000.0)


class _Unknown:
    """An unseen payee -> thin history -> HOLD."""
    def lookup(self, cp):
        return {}


class TestPayoutAction(unittest.TestCase):
    """
    Mutation notes:
      - map STOP to release/review -> test_stop_blocks FAILS (the load-bearing one).
      - map HOLD to release -> test_hold_reviews FAILS.
      - default unknown verdict to RELEASE -> test_unknown_reviews FAILS.
    """
    def test_go_releases(self):
        self.assertEqual(ap_gate.payout_action("GO"), ap_gate.RELEASE)

    def test_hold_reviews(self):
        self.assertEqual(ap_gate.payout_action("HOLD"), ap_gate.REVIEW)

    def test_stop_blocks(self):
        self.assertEqual(ap_gate.payout_action("STOP"), ap_gate.BLOCK)

    def test_unknown_reviews(self):
        # never auto-release on an unclear signal -- fail toward a human
        self.assertEqual(ap_gate.payout_action(None), ap_gate.REVIEW)
        self.assertEqual(ap_gate.payout_action("???"), ap_gate.REVIEW)


class TestPayoutPayload(unittest.TestCase):
    """
    Mutation notes:
      - drop invoice_id->resource mapping -> test_maps_invoice FAILS.
      - map payee to something other than counterparty -> test_maps_payee FAILS.
    """
    def test_maps_payee_and_invoice(self):
        p = ap_gate.payout_payload({
            "payee": SANC, "amount": "100.00", "asset": ASSET, "chain": "base",
            "payer": "0xabc", "invoice_id": "INV-42"})
        self.assertEqual(p["counterparty"], SANC)
        self.assertEqual(p["resource"], "INV-42")
        self.assertEqual(p["payer"], "0xabc")

    def test_optional_fields_absent(self):
        p = ap_gate.payout_payload({
            "payee": SANC, "amount": "100.00", "asset": ASSET, "chain": "base"})
        self.assertNotIn("resource", p)
        self.assertNotIn("payer", p)


class TestScreenPayout(unittest.TestCase):
    """
    End-to-end: payout -> verdict -> AP action.

    Mutation notes:
      - don't route sanctioned to BLOCK -> test_sanctioned_blocks FAILS.
      - auto-release on validation error -> test_invalid_fails_closed FAILS.
      - set requires_human on non-REVIEW -> test_release_no_human FAILS.
    """
    def _payout(self, payee, amount="100.00"):
        return {"payee": payee, "amount": amount, "asset": ASSET, "chain": "base",
                "invoice_id": "INV-1"}

    def test_sanctioned_blocks(self):
        src = SanctionsScreeningSource(_StrongRep(), SanctionsList([SANC]))
        d = ap_gate.screen_payout(self._payout(SANC), src)
        self.assertEqual(d["action"], ap_gate.BLOCK)  # even with perfect rep
        self.assertFalse(d["requires_human"])
        self.assertIsNotNone(d["receipt_id"])         # audit trail present

    def test_strong_payee_small_releases(self):
        # strong payee + amount matching history + under the $10 escalation floor
        src = SanctionsScreeningSource(_StrongRep(), SanctionsList())
        d = ap_gate.screen_payout(
            self._payout("0x00000000000000000000000000000000deadbeef",
                         amount="5.00"), src)
        self.assertEqual(d["action"], ap_gate.RELEASE)
        self.assertFalse(d["requires_human"])

    def test_large_but_inline_payout_escalates_to_review(self):
        # FINDING: even a large payout that is IN-LINE with the payee's history
        # (not a gouge) still routes to a human, because the engine's $10
        # auto-approve floor (HOLD_AMOUNT_THRESHOLD) escalates any amount > $10.
        # Safe default, but the AP auto-release path needs a higher, configurable
        # threshold for treasury use (documented in the brief).
        src = SanctionsScreeningSource(_StrongRepBig(), SanctionsList())
        d = ap_gate.screen_payout(
            self._payout("0x00000000000000000000000000000000deadbeef",
                         amount="5000.00"), src)
        self.assertEqual(d["action"], ap_gate.REVIEW)
        self.assertTrue(d["requires_human"])

    def test_release_no_human(self):
        src = SanctionsScreeningSource(_StrongRep(), SanctionsList())
        d = ap_gate.screen_payout(
            self._payout("0x00000000000000000000000000000000deadbeef",
                         amount="5.00"), src)
        self.assertEqual(d["requires_human"], d["action"] == ap_gate.REVIEW)

    def test_unknown_payee_reviews(self):
        d = ap_gate.screen_payout(
            self._payout("0xUNSEEN0000000000000000000000000000000001"), _Unknown())
        self.assertEqual(d["action"], ap_gate.REVIEW)
        self.assertTrue(d["requires_human"])

    def test_price_gouge_blocks(self):
        # strong payee but a 100x-over-median amount -> STOP -> BLOCK
        src = SanctionsScreeningSource(_StrongRep(), SanctionsList())
        d = ap_gate.screen_payout(
            self._payout("0x00000000000000000000000000000000deadbeef",
                         amount="10000.00"), src)
        self.assertEqual(d["action"], ap_gate.BLOCK)

    def test_hold_above_lets_large_inline_release(self):
        # raising the auto-release ceiling lets a large, IN-LINE payout to a
        # trusted vendor auto-release instead of routing to a human
        src = SanctionsScreeningSource(_StrongRepBig(), SanctionsList())
        d = ap_gate.screen_payout(
            self._payout("0x00000000000000000000000000000000deadbeef",
                         amount="5000.00"), src, hold_above="10000")
        self.assertEqual(d["action"], ap_gate.RELEASE)

    def test_hold_above_still_blocks_gouge(self):
        # a raised ceiling must NOT let a price gouge through
        src = SanctionsScreeningSource(_StrongRep(), SanctionsList())  # $5 history
        d = ap_gate.screen_payout(
            self._payout("0x00000000000000000000000000000000deadbeef",
                         amount="5000.00"), src, hold_above="10000")
        self.assertEqual(d["action"], ap_gate.BLOCK)  # 1000x over median

    def test_invalid_fails_closed(self):
        # missing payee -> forecast validation error -> REVIEW, never RELEASE
        src = SanctionsScreeningSource(_StrongRep(), SanctionsList())
        d = ap_gate.screen_payout(
            {"amount": "100.00", "asset": ASSET, "chain": "base"}, src)
        self.assertEqual(d["action"], ap_gate.REVIEW)
        self.assertTrue(d["requires_human"])
        self.assertIsNotNone(d["error"])


if __name__ == "__main__":
    unittest.main()
