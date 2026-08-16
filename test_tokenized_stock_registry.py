#!/usr/bin/env python3
"""
test_tokenized_stock_registry.py -- TDD for the tokenized-stock discovery registry.
Each test names the MUTATION it kills.
"""
import unittest

import tokenized_stock_registry as tsr
from tokenized_stock_registry import (TokenizedStockRegistry, build_registry,
                                       load_backed, normalize_address,
                                       parse_backed_assets, parse_seed)

EVM = "0xF758E87CA18824b767aa4F3ed58C188d3bABE428"      # mixed-case (checksummed)
SOL = "Xs3uhDYpQGfkeZ7rrzQgmozWQHC5uDLUa6iAN65qHM1"     # base58, case-significant

BACKED_SAMPLE = {"data": {"assets": {"nodes": [
    {"id": "1", "name": "Backed NVDA", "symbol": "NVDAx", "isin": "XS123",
     "underlyingSymbol": "NVDA", "isTradingHalted": False,
     "deployments": [{"network": "Ethereum", "address": EVM},
                     {"network": "Solana", "address": SOL}]},
    {"id": "2", "symbol": "NOADDR", "deployments": []},           # dropped: no address
    "junk-node",                                                  # dropped: not a dict
]}}}


class TestNormalize(unittest.TestCase):
    def test_evm_lowercased(self):
        # MUTATION: not lowercasing EVM -> reputation/lookup splits across cases.
        self.assertEqual(normalize_address(EVM), EVM.lower())

    def test_solana_case_preserved(self):
        # MUTATION: lowercasing a base58 Solana address CORRUPTS it (case-significant).
        self.assertEqual(normalize_address(SOL, "solana"), SOL)

    def test_empty_none(self):
        self.assertIsNone(normalize_address(""))
        self.assertIsNone(normalize_address(None))


class TestParseBacked(unittest.TestCase):
    def test_parses_nested_envelope(self):
        # MUTATION: a shallow-only `nodes` lookup would miss data.assets.nodes.
        recs = parse_backed_assets(BACKED_SAMPLE)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["symbol"], "NVDAx")
        self.assertEqual(r["underlying_symbol"], "NVDA")
        self.assertEqual(len(r["deployments"]), 2)

    def test_accepts_bare_list(self):
        recs = parse_backed_assets([{"symbol": "AAPLx",
                                     "deployments": [{"network": "base", "address": EVM}]}])
        self.assertEqual(recs[0]["symbol"], "AAPLx")

    def test_drops_addressless_and_junk(self):
        # MUTATION: keeping addressless nodes yields un-lookuppable records.
        recs = parse_backed_assets(BACKED_SAMPLE)
        self.assertTrue(all(r["deployments"] for r in recs))

    def test_never_raises_on_garbage(self):
        self.assertEqual(parse_backed_assets(None), [])
        self.assertEqual(parse_backed_assets(42), [])


class TestRegistry(unittest.TestCase):
    def _reg(self):
        return TokenizedStockRegistry().add(parse_backed_assets(BACKED_SAMPLE))

    def test_lookup_evm_case_insensitive(self):
        # MUTATION: case-sensitive EVM lookup would miss the checksummed request.
        reg = self._reg()
        self.assertIsNotNone(reg.lookup(EVM, "ethereum"))
        self.assertIsNotNone(reg.lookup(EVM.lower(), "ethereum"))
        self.assertEqual(reg.lookup(EVM, "ethereum")["symbol"], "NVDAx")

    def test_lookup_solana_exact(self):
        reg = self._reg()
        self.assertIsNotNone(reg.lookup(SOL, "solana"))
        # a lowercased Solana address must NOT match (it's a different/invalid key).
        self.assertIsNone(reg.lookup(SOL.lower(), "solana"))

    def test_lookup_any_chain(self):
        self.assertIsNotNone(self._reg().lookup(EVM))     # chain omitted -> any

    def test_by_ticker(self):
        self.assertEqual(self._reg().by_ticker("nvdax")["isin"], "XS123")

    def test_unknown_is_none(self):
        self.assertIsNone(self._reg().lookup("0x" + "00" * 20, "ethereum"))
        self.assertFalse(self._reg().is_known("0x" + "00" * 20))

    def test_seed_overrides_feed(self):
        # MUTATION: if the feed overrode the seed (wrong order), an operator couldn't
        # correct a bad ingested record. Seed is added last -> wins on same key.
        reg = TokenizedStockRegistry()
        reg.add(parse_backed_assets(BACKED_SAMPLE))
        reg.add(parse_seed([{"issuer": "ondo", "symbol": "NVDAx-CORRECTED",
                             "deployments": [{"network": "ethereum", "address": EVM}]}]))
        self.assertEqual(reg.lookup(EVM, "ethereum")["issuer"], "ondo")


class TestSeed(unittest.TestCase):
    def test_static_seed_empty_by_default(self):
        # MUTATION/guard: shipping FABRICATED addresses would mis-identify tokens.
        self.assertEqual(tsr.STATIC_SEED, [])

    def test_seed_drops_addressless_entry(self):
        self.assertEqual(parse_seed([{"issuer": "x", "symbol": "Y", "deployments": []}]), [])


class TestLoad(unittest.TestCase):
    def test_load_backed_via_injected_get(self):
        recs = load_backed(get=lambda url: BACKED_SAMPLE)
        self.assertEqual(recs[0]["symbol"], "NVDAx")

    def test_load_backed_failsoft(self):
        # MUTATION: an unguarded fetch error would crash registry assembly.
        def boom(url):
            raise RuntimeError("network down")
        self.assertEqual(load_backed(get=boom), [])

    def test_build_registry_merges_feed_and_seed(self):
        reg = build_registry(
            get=lambda url: BACKED_SAMPLE,
            seed=[{"issuer": "dinari", "symbol": "TSLAd",
                   "deployments": [{"network": "base", "address": "0x" + "ab" * 20}]}])
        self.assertIsNotNone(reg.by_ticker("NVDAx"))
        self.assertIsNotNone(reg.by_ticker("TSLAd"))


if __name__ == "__main__":
    unittest.main()
