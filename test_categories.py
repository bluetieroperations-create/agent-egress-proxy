"""
Tests for categories.py -- the shared service-category classifier. Each test states
the mutation it kills.
"""
import unittest

import categories as C


class TestClassifyResource(unittest.TestCase):
    """
    Mutation notes:
      - match path only (ignore host) -> test_by_host FAILS.
      - reorder rules so commerce loses to ai on a ".ai" host -> test_precedence FAILS.
    """
    def test_by_host(self):
        self.assertEqual(C.classify_resource("https://api.deepseek.ai/v1/chat"), "ai-agents")

    def test_by_path(self):
        self.assertEqual(C.classify_resource("https://x/api/chain/tx/0xabc"), "onchain")
        self.assertEqual(C.classify_resource("https://x/x402/gift-cards"), "commerce")

    def test_precedence_commerce_over_ai(self):
        # a checkout hosted on an .ai domain is COMMERCE, not ai-agents (order matters)
        self.assertEqual(C.classify_resource("https://shop.foo.ai/checkout"), "commerce")

    def test_unknown_is_other(self):
        self.assertEqual(C.classify_resource("https://mystery.example/v1/thing"),
                         C.CATEGORY_UNCLASSIFIED)

    def test_garbage_input_is_other(self):
        for bad in (None, "", "not a url", 123):
            self.assertEqual(C.classify_resource(bad if isinstance(bad, str) else str(bad)),
                             C.CATEGORY_UNCLASSIFIED)


class TestClassifyCategory(unittest.TestCase):
    """
    Mutation notes:
      - let 'other' win ties over a real category -> test_tie_prefers_real FAILS.
      - pick least-common instead of dominant -> test_dominant FAILS.
    """
    def test_dominant(self):
        cat = C.classify_category(["https://x/price/btc", "https://x/market/eth",
                                   "https://x/v1/mystery"])
        self.assertEqual(cat, "finance")

    def test_tie_prefers_real(self):
        self.assertEqual(C.classify_category(["https://x/v1/mystery", "https://x/chat"]),
                         "ai-agents")

    def test_empty_is_other(self):
        self.assertEqual(C.classify_category([]), C.CATEGORY_UNCLASSIFIED)
        self.assertEqual(C.classify_category(None), C.CATEGORY_UNCLASSIFIED)


if __name__ == "__main__":
    unittest.main()
