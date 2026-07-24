import unittest

from agentdata.core.enrich import StubEnricher
from agentdata.core.product import Context
from agentdata.data_sources.property_stub import StubPropertySource
from agentdata.products.property_intel import PropertyIntel


class _Cfg:
    resource_base = "https://api.test"
    asset = "USDC"


def ctx():
    return Context(cache=None, enricher=StubEnricher(),
                   sources={"property": StubPropertySource()},
                   logstore=None, config=_Cfg())


class TestPropertyIntel(unittest.TestCase):
    def setUp(self):
        self.p = PropertyIntel()

    def test_normalization_shares_cache_key(self):
        k1 = self.p.cache_key({"address": "123 Main St, Austin TX"})
        k2 = self.p.cache_key({"address": "  123  main st  austin tx "})
        self.assertEqual(k1, k2)

    def test_fetch_enrich_deterministic(self):
        c = ctx()
        payload = self.p.validate_input({"address": "123 Main St, Austin TX"})
        r1 = self.p.enrich(self.p.fetch(payload, c), payload, c)
        r2 = self.p.enrich(self.p.fetch(payload, c), payload, c)
        self.assertEqual(r1.data, r2.data)          # stable
        self.assertTrue(r1.data["found"])
        self.assertIn("summary", r1.data)
        self.assertIn("recommendation", r1.data)
        self.assertIn("confidence", r1.meta)
        self.assertEqual(r1.meta["sources"], ["property_stub"])

    def test_flags_present(self):
        c = ctx()
        payload = self.p.validate_input({"address": "42 Galaxy Way, Reno NV"})
        r = self.p.enrich(self.p.fetch(payload, c), payload, c)
        for f in ("open_permits", "has_code_violations",
                  "unpermitted_work_suspected", "high_flood_risk"):
            self.assertIn(f, r.data["flags"])
            self.assertIsInstance(r.data["flags"][f], bool)

    def test_tool_schema(self):
        s = self.p.tool_schema()
        self.assertEqual(s["name"], "property_lookup")
        self.assertIn("address", s["inputSchema"]["properties"])
        self.assertEqual(s["inputSchema"]["required"], ["address"])

    def test_price_atomic(self):
        self.assertEqual(self.p.price_atomic(6), 200_000)


if __name__ == "__main__":
    unittest.main()
