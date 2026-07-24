import base64
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from agentdata.core import payment as pay
from agentdata.core.config import Config
from agentdata.core.platform import Platform
from agentdata.core.product import Registry
from agentdata.core.server import make_handler
from agentdata.core.storage import LogStore, MemoryCache
from agentdata.data_sources.property_stub import StubPropertySource
from agentdata.products.property_intel import PropertyIntel


def x_payment(value=200_000, network="base"):
    doc = {"scheme": "exact", "network": network,
           "payload": {"authorization": {"value": str(value)}}}
    return base64.b64encode(json.dumps(doc).encode()).decode()


class TestHttpRoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        cfg = Config(require_payment=True, db_path="", log_path=os.devnull,
                     network="base", asset="USDC", pay_to="0xSELLER", port=0)
        reg = Registry()
        reg.register(PropertyIntel())
        platform = Platform(cfg, reg, cache=MemoryCache(),
                            sources={"property": StubPropertySource()},
                            logstore=LogStore(os.devnull),
                            facilitator=pay.MockFacilitator())
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(platform))
        cls.port = cls.httpd.server_address[1]
        cls.t = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def test_catalog(self):
        with urllib.request.urlopen(self._url("/")) as r:
            body = json.loads(r.read())
        self.assertEqual(body["products"][0]["endpoint"], "/v1/property/lookup")

    def test_402_then_paid(self):
        # 1. no payment -> 402
        req = urllib.request.Request(
            self._url("/v1/property/lookup"),
            data=json.dumps({"address": "123 Main St, Austin TX"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 402)
        offer = json.loads(cm.exception.read())
        self.assertEqual(offer["accepts"][0]["maxAmountRequired"], "200000")

        # 2. with payment -> 200 + data
        req2 = urllib.request.Request(
            self._url("/v1/property/lookup"),
            data=json.dumps({"address": "123 Main St, Austin TX"}).encode(),
            headers={"Content-Type": "application/json", "X-PAYMENT": x_payment()},
            method="POST")
        with urllib.request.urlopen(req2) as r:
            self.assertEqual(r.status, 200)
            self.assertTrue(r.headers.get("X-PAYMENT-RESPONSE"))
            body = json.loads(r.read())
        self.assertTrue(body["data"]["found"])
        self.assertIn("recommendation", body["data"])


if __name__ == "__main__":
    unittest.main()
