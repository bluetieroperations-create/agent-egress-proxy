"""
test_demo_flywheel.py -- guard that the verdict->outcome->reputation->verdict loop
actually turns: cold-start HOLD -> earns GO from its own settled payments -> loses it
when recent outcomes go bad. All through the real forecast + ledger path.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "clients"))
import demo_flywheel as F


class TestFlywheel(unittest.TestCase):
    def setUp(self):
        self.r = F.run_flywheel()

    def test_loop_turns(self):
        verdicts = [v for _, v, _ in self.r["stages"]]
        self.assertEqual(verdicts, ["HOLD", "GO", "HOLD"])

    def test_reputation_built_from_the_loop(self):
        # after 25 settled payments: real history + distinct payers, no disputes.
        _, verdict, rec = self.r["stages"][1]
        self.assertEqual(verdict, "GO")
        self.assertEqual(rec["settlement_count"], 25)
        self.assertEqual(rec["distinct_payers"], 5)

    def test_going_bad_reflected(self):
        _, verdict, rec = self.r["stages"][2]
        self.assertEqual(verdict, "HOLD")
        self.assertGreaterEqual(rec["recent_dispute_rate"], 0.30)

    def test_real_signature_when_available(self):
        # if eth-account is installed, the EIP-3009 signer must recover to the payer.
        if self.r["signed"]:
            self.assertTrue(self.r["signature_ok"])


if __name__ == "__main__":
    unittest.main()
