"""
Tests for the x402 service descriptor (discovery / listing).

Run: python -m unittest test_discovery.py -v
"""
import json
import threading
import time
import unittest
import urllib.request

import discovery as D
from blackwall import BlackwallServer

PAY_TO = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


class TestBuildDescriptor(unittest.TestCase):
    def test_unpriced_when_no_billing(self):
        d = D.build_descriptor()
        self.assertEqual(d["name"], "Blackwall")
        self.assertFalse(d["custody"])  # verdict, not custody
        self.assertIsNone(d["resources"][0]["accepts"])

    def test_priced_when_billing(self):
        d = D.build_descriptor(pay_to=PAY_TO, price="0.001")
        acc = d["resources"][0]["accepts"][0]
        self.assertEqual(acc["payTo"], PAY_TO)
        self.assertEqual(acc["maxAmountRequired"], "0.001")
        self.assertEqual(acc["network"], "base")

    def test_human_price(self):
        self.assertEqual(D.human_price(1000, 6), "0.001")
        self.assertIsNone(D.human_price(None))

    def test_advertises_verdicts_and_mcp(self):
        d = D.build_descriptor()
        self.assertEqual(d["resources"][0]["outputVerdicts"], ["GO", "HOLD", "STOP"])
        self.assertEqual(d["mcp"]["tool"], "forecast_payment")

    def test_json_serializable(self):
        json.dumps(D.build_descriptor(pay_to=PAY_TO, price="0.001"))

    def test_endpoint_readiness_advertised_only_when_on(self):
        # Mutation: advertise readiness unconditionally -> this FAILS.
        on = D.build_descriptor(endpoint_readiness=True)
        self.assertIn("endpoint-readiness", on["signals"])
        off = D.build_descriptor()
        self.assertNotIn("endpoint-readiness", off["signals"])


class TestDiscoveryEndpoint(unittest.TestCase):
    def setUp(self):
        self.srv = BlackwallServer(port=0)
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()
        time.sleep(0.3)
        self.base = "http://127.0.0.1:%d" % self.srv.port

    def tearDown(self):
        self.srv.shutdown()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=3) as r:
            return r.status, json.load(r)

    def test_well_known_x402(self):
        s, d = self._get("/.well-known/x402")
        self.assertEqual(s, 200)
        self.assertEqual(d["name"], "Blackwall")

    def test_v1_discovery_alias(self):
        s, d = self._get("/v1/discovery")
        self.assertEqual(s, 200)
        self.assertIn("resources", d)


if __name__ == "__main__":
    unittest.main()
