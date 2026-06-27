"""
Offline unit tests for reputation_onchain.derive_record (the pure part).

Run: python -m unittest test_reputation_onchain.py -v

The HTTP fetch is isolated in OnchainReputationSource._default_get; derivation
is pure and tested here from canned Blockscout JSON (no network). One test also
drives the seam: a derived record must drop into blackwall.forecast() unchanged.
"""
import unittest

import blackwall as bw
import reputation_onchain as ro

USDC = ro.BASE_USDC


def _xfer(symbol, addr, value, decimals="6", to="0xCP"):
    return {"token": {"symbol": symbol, "address": addr},
            "total": {"value": value, "decimals": decimals},
            "from": {"hash": "0xSender"}, "to": {"hash": to}}


class TestDeriveRecord(unittest.TestCase):
    """
    derive_record: raw Blockscout JSON -> Blackwall record.

    Mutation notes:
      - Fabricate dispute_rate=0 instead of None -> test_dispute_unavailable FAILS
        (on-chain cannot see disputes; must not pretend it can).
      - Drop the USDC token filter -> test_only_usdc_counted FAILS (would count
        unrelated tokens as settlements).
      - Forget decimals adjustment -> test_amounts_decimal_adjusted FAILS.
    """

    def test_dispute_unavailable_is_none_not_zero(self):
        rec = ro.derive_record({"transactions_count": "10",
                                "token_transfers_count": "5"},
                               {"items": []}, "0xCP")
        self.assertIsNone(rec["dispute_rate"])
        self.assertEqual(rec["_meta"]["data_completeness"]["dispute_rate"],
                         "UNAVAILABLE-on-chain")

    def test_only_usdc_counted(self):
        transfers = {"items": [
            _xfer("USDC", USDC, "90000"),       # 0.09 USDC inbound
            _xfer("FLOCK", "0xother", "100"),   # unrelated token -> ignored
            _xfer("USDC", USDC, "120000"),      # 0.12 USDC inbound
        ]}
        rec = ro.derive_record({"transactions_count": "3",
                                "token_transfers_count": "3"},
                               transfers, "0xCP")
        self.assertEqual(rec["settlement_count"], 2)
        self.assertEqual(rec["price_history"], ["0.09", "0.12"])

    def test_amounts_decimal_adjusted(self):
        rec = ro.derive_record({}, {"items": [_xfer("USDC", USDC, "1000000")]},
                               "0xCP")
        self.assertEqual(rec["price_history"], ["1"])  # 1_000_000 / 1e6

    def test_sanctioned_from_supplied_set(self):
        rec = ro.derive_record({}, {"items": []}, "0xBADWALLET",
                               sanctioned={"0xbadwallet"})
        self.assertTrue(rec["sanctioned"])

    def test_lower_bound_flagged_when_truncated(self):
        items = [_xfer("USDC", USDC, "90000") for _ in range(50)]
        rec = ro.derive_record({}, {"items": items,
                                    "next_page_params": {"x": 1}}, "0xCP")
        self.assertTrue(rec["_meta"]["settlement_count_is_lower_bound"])
        self.assertTrue(rec["_meta"]["sampled_truncated"])


class TestSeamCompatibility(unittest.TestCase):
    """A derived record must be consumable by the verdict path unchanged."""

    def test_record_drives_forecast(self):
        items = [_xfer("USDC", USDC, "90000") for _ in range(25)]  # >= thin gate
        rec = ro.derive_record({"transactions_count": "500",
                                "token_transfers_count": "300"},
                               {"items": items}, "0xCP")
        # decide_payment reads only known keys; _meta is ignored.
        v = bw.decide_payment("0.09", rec, rec["price_history"], counterparty="0xCP")
        self.assertIn(v["verdict"], ("GO", "HOLD", "STOP"))
        # 25 sampled USDC settlements, stable ~0.09 price, micro amount.
        self.assertEqual(v["verdict"], "GO")

    def test_volume_only_record_holds(self):
        # transfers failed -> no settlements seen -> thin -> HOLD (safe default).
        rec = ro.derive_record({"transactions_count": "9999",
                                "token_transfers_count": "9999"},
                               {}, "0xCP")
        v = bw.decide_payment("0.09", rec, rec["price_history"], counterparty="0xCP")
        self.assertEqual(v["verdict"], "HOLD")


if __name__ == "__main__":
    unittest.main()
