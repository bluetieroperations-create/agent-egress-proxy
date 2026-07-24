import base64
import json
import unittest

from agentdata.core import payment as pay
from agentdata.core.config import Config
from agentdata.products.property_intel import PropertyIntel


def x_payment(value, network="base"):
    doc = {"x402Version": 1, "scheme": "exact", "network": network,
           "payload": {"authorization": {"value": str(value)}}}
    return base64.b64encode(json.dumps(doc).encode()).decode()


class TestPayment(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(network="base", asset="USDC", asset_decimals=6,
                          pay_to="0xSELLER")
        self.req = pay.build_requirements(
            PropertyIntel(), self.cfg, "https://api/x/v1/property/lookup")

    def test_requirements_price(self):
        self.assertEqual(self.req.max_amount_required, "200000")   # $0.20
        self.assertEqual(self.req.network, "base")
        self.assertEqual(self.req.pay_to, "0xSELLER")

    def test_offer_body_shape(self):
        body = pay.offer_body(self.req, error="need-payment")
        self.assertEqual(body["x402Version"], 1)
        self.assertEqual(body["accepts"][0]["maxAmountRequired"], "200000")
        self.assertEqual(body["error"], "need-payment")

    def test_parse_header(self):
        doc = pay.parse_payment_header(x_payment(200_000))
        self.assertEqual(doc["payload"]["authorization"]["value"], "200000")
        self.assertIsNone(pay.parse_payment_header("!!!bad"))
        self.assertIsNone(pay.parse_payment_header(None))

    def test_mock_verify_ok(self):
        f = pay.MockFacilitator()
        v = f.verify(pay.parse_payment_header(x_payment(200_000)), self.req)
        self.assertTrue(v.valid)
        self.assertEqual(v.amount_atomic, 200_000)

    def test_mock_verify_insufficient(self):
        f = pay.MockFacilitator()
        v = f.verify(pay.parse_payment_header(x_payment(100_000)), self.req)
        self.assertFalse(v.valid)
        self.assertEqual(v.reason, "insufficient-amount")

    def test_mock_verify_wrong_network(self):
        f = pay.MockFacilitator()
        v = f.verify(pay.parse_payment_header(x_payment(200_000, "solana")), self.req)
        self.assertFalse(v.valid)
        self.assertEqual(v.reason, "network-mismatch")

    def test_mock_settle_deterministic(self):
        f = pay.MockFacilitator()
        doc = pay.parse_payment_header(x_payment(200_000))
        s1 = f.settle(doc, self.req)
        s2 = f.settle(doc, self.req)
        self.assertTrue(s1.settled)
        self.assertEqual(s1.tx, s2.tx)             # reproducible, no RNG/clock


if __name__ == "__main__":
    unittest.main()
