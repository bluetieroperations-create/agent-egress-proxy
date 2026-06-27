"""
Tests for HttpFacilitator driven against the reference facilitator sim over
REAL HTTP (loopback) -- exercises the production facilitator adapter path.

Run: python -m unittest test_facilitator.py -v
"""
import base64
import json
import unittest

import x402 as X
from facilitator_sim import FacilitatorSim

PAY_TO = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def payment(value="1000", nonce="0xn"):
    p = {"scheme": "exact", "network": "base",
         "payload": {"authorization": {"to": PAY_TO, "value": value, "nonce": nonce}}}
    return p


def encode(p):
    return base64.b64encode(json.dumps(p).encode()).decode()


class TestHttpFacilitator(unittest.TestCase):
    def _gate(self, approve=True, settle_ok=True):
        sim = FacilitatorSim(approve=approve, settle_ok=settle_ok).start_background()
        self.addCleanup(sim.shutdown)
        cfg = X.BillingConfig(price="0.001", pay_to=PAY_TO)
        return X.BillingGate(cfg, facilitator=X.HttpFacilitator(sim.url))

    def test_verify_and_settle_ok(self):
        gate = self._gate(approve=True, settle_ok=True)
        r = gate.check("https://r", x_payment=encode(payment(nonce="0x1")))
        self.assertTrue(r.paid)
        self.assertEqual(r.settlement, "0xsimsettle")

    def test_facilitator_rejects_signature(self):
        gate = self._gate(approve=False)
        r = gate.check("https://r", x_payment=encode(payment(nonce="0x2")))
        self.assertFalse(r.paid)
        self.assertIn("sim-rejected", r.body["error"])

    def test_settlement_failure_releases_nonce(self):
        gate = self._gate(approve=True, settle_ok=False)
        pay = encode(payment(nonce="0x3"))
        r = gate.check("https://r", x_payment=pay)
        self.assertFalse(r.paid)
        self.assertIn("settlement failed", r.body["error"])
        # nonce released -> a retry against a now-working facilitator succeeds.
        ok_gate = self._gate(approve=True, settle_ok=True)
        # reuse the same gate's nonce ledger by re-pointing facilitator is not
        # possible here; assert release on the SAME gate via direct ledger check:
        self.assertTrue(gate.nonces.add("0x3") is True)  # was released, addable

    def test_facilitator_unreachable_fails_closed(self):
        cfg = X.BillingConfig(price="0.001", pay_to=PAY_TO)
        gate = X.BillingGate(cfg, facilitator=X.HttpFacilitator("http://127.0.0.1:1"))
        r = gate.check("https://r", x_payment=encode(payment(nonce="0x4")))
        self.assertFalse(r.paid)
        self.assertIn("unreachable", r.body["error"])


if __name__ == "__main__":
    unittest.main()
