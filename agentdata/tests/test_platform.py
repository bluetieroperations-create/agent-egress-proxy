import base64
import json
import os
import tempfile
import unittest

from agentdata.core import payment as pay
from agentdata.core.config import Config
from agentdata.core.platform import Platform
from agentdata.core.product import Registry
from agentdata.core.storage import LogStore, MemoryCache
from agentdata.data_sources.property_stub import StubPropertySource
from agentdata.products.property_intel import PropertyIntel


def x_payment(value=200_000, network="base"):
    doc = {"scheme": "exact", "network": network,
           "payload": {"authorization": {"value": str(value)}}}
    return base64.b64encode(json.dumps(doc).encode()).decode()


def make_platform(require_payment=True, logpath=None):
    cfg = Config(require_payment=require_payment, db_path="",
                 log_path=logpath or os.devnull, network="base",
                 asset="USDC", pay_to="0xSELLER")
    reg = Registry()
    reg.register(PropertyIntel())
    return Platform(cfg, reg, cache=MemoryCache(),
                    sources={"property": StubPropertySource()},
                    logstore=LogStore(cfg.log_path),
                    facilitator=pay.MockFacilitator())


class TestPipeline(unittest.TestCase):
    def test_402_without_payment(self):
        p = make_platform()
        out = p.serve_call("property", "lookup", {"address": "123 Main St, Austin TX"})
        self.assertEqual(out.status, 402)
        self.assertEqual(out.kind, "payment_required")
        self.assertEqual(out.body["accepts"][0]["maxAmountRequired"], "200000")

    def test_paid_call_returns_data(self):
        p = make_platform()
        out = p.serve_call("property", "lookup",
                           {"address": "123 Main St, Austin TX"}, x_payment())
        self.assertEqual(out.status, 200)
        self.assertTrue(out.paid_ok)
        self.assertIn("X-PAYMENT-RESPONSE", out.headers)
        data = out.body["data"]
        self.assertTrue(data["found"])
        self.assertIn("summary", data)
        self.assertIn("flags", data)
        self.assertIn(data["recommendation"], ("proceed", "note", "review"))
        self.assertFalse(out.body["meta"]["cache_hit"])

    def test_second_call_hits_cache(self):
        p = make_platform()
        args = {"address": "500 Congress Ave, Austin TX"}
        p.serve_call("property", "lookup", args, x_payment())
        out2 = p.serve_call("property", "lookup", args, x_payment())
        self.assertTrue(out2.body["meta"]["cache_hit"])

    def test_insufficient_payment_402(self):
        p = make_platform()
        out = p.serve_call("property", "lookup",
                           {"address": "1 Foo Rd, Dallas TX"}, x_payment(100_000))
        self.assertEqual(out.status, 402)
        self.assertEqual(out.body["error"], "insufficient-amount")

    def test_free_mode_skips_gate(self):
        p = make_platform(require_payment=False)
        out = p.serve_call("property", "lookup", {"address": "9 Elm St, Reno NV"})
        self.assertEqual(out.status, 200)
        self.assertNotIn("X-PAYMENT-RESPONSE", out.headers)

    def test_bad_input_400(self):
        p = make_platform(require_payment=False)
        out = p.serve_call("property", "lookup", {"address": "x"})
        self.assertEqual(out.status, 400)

    def test_unknown_product_404(self):
        p = make_platform(require_payment=False)
        out = p.serve_call("nope", "lookup", {})
        self.assertEqual(out.status, 404)

    def test_sale_is_logged(self):
        with tempfile.NamedTemporaryFile("r", suffix=".log", delete=False) as tf:
            path = tf.name
        try:
            p = make_platform(logpath=path)
            p.serve_call("property", "lookup",
                         {"address": "77 Sunset Blvd, LA CA"}, x_payment())
            with open(path) as f:
                lines = [json.loads(l) for l in f if l.strip()]
            self.assertTrue(any(r["decision"] == "served" and r["tx"]
                                for r in lines))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
