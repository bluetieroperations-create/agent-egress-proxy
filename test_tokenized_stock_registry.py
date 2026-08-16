#!/usr/bin/env python3
"""
test_tokenized_stock_registry.py -- TDD for the tokenized-stock discovery registry.
Each test names the MUTATION it kills.
"""
import unittest

import tokenized_stock_registry as tsr
from tokenized_stock_registry import (TokenizedStockRegistry, apply_asset_registry,
                                       build_registry, load_backed, normalize_address,
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

    def test_paginates_until_no_next(self):
        # MUTATION: stopping after page 1 (no pagination) would miss pages 2..N.
        pages = {
            1: {"nodes": [{"symbol": "P1", "deployments": [{"network": "base", "address": EVM}]}],
                "page": {"currentPage": 1, "hasNextPage": True}},
            2: {"nodes": [{"symbol": "P2", "deployments": [{"network": "base", "address": "0x" + "22" * 20}]}],
                "page": {"currentPage": 2, "hasNextPage": False}},
        }

        def get(url):
            n = int(url.split("page=")[1])
            return pages[n]
        recs = load_backed(get=get)
        self.assertEqual({r["symbol"] for r in recs}, {"P1", "P2"})

    def test_pagination_failsoft_midwalk(self):
        # MUTATION: a mid-walk fetch error must KEEP earlier pages, not drop everything.
        def get(url):
            n = int(url.split("page=")[1])
            if n == 1:
                return {"nodes": [{"symbol": "P1", "deployments": [{"network": "base", "address": EVM}]}],
                        "page": {"currentPage": 1, "hasNextPage": True}}
            raise RuntimeError("page 2 down")
        recs = load_backed(get=get)
        self.assertEqual([r["symbol"] for r in recs], ["P1"])

    def test_pagination_respects_max_pages(self):
        calls = []

        def get(url):
            calls.append(url)
            return {"nodes": [{"symbol": "X", "deployments": [{"network": "base", "address": EVM}]}],
                    "page": {"hasNextPage": True}}       # never-ending feed
        load_backed(get=get, max_pages=3)
        self.assertEqual(len(calls), 3)

    def test_build_registry_merges_feed_and_seed(self):
        reg = build_registry(
            get=lambda url: BACKED_SAMPLE,
            seed=[{"issuer": "dinari", "symbol": "TSLAd",
                   "deployments": [{"network": "base", "address": "0x" + "ab" * 20}]}])
        self.assertIsNotNone(reg.by_ticker("NVDAx"))
        self.assertIsNotNone(reg.by_ticker("TSLAd"))


class TestApplyAssetRegistry(unittest.TestCase):
    REC = {"issuer": "backed", "symbol": "NVDAx", "underlying_symbol": "NVDA",
           "isin": "XS123", "standard": None, "trading_halted": False}

    def _go(self):
        return {"verdict": "GO", "reasons": [], "signals": {}}

    def test_annotates_signals_and_reason(self):
        out = apply_asset_registry(self._go(), self.REC)
        self.assertEqual(out["signals"]["rwa_asset"]["symbol"], "NVDAx")
        self.assertEqual(out["signals"]["rwa_asset"]["underlying_symbol"], "NVDA")
        self.assertTrue(any("NVDAx" in r for r in out["reasons"]))

    def test_not_halted_leaves_go(self):
        self.assertEqual(apply_asset_registry(self._go(), self.REC)["verdict"], "GO")

    def test_trading_halted_escalates_go_to_hold(self):
        # MUTATION: not gating on trading_halted lets an agent buy a halted security.
        rec = dict(self.REC, trading_halted=True)
        out = apply_asset_registry(self._go(), rec)
        self.assertEqual(out["verdict"], "HOLD")
        self.assertTrue(any("HALTED" in r for r in out["reasons"]))

    def test_halted_never_touches_stop(self):
        rec = dict(self.REC, trading_halted=True)
        stop = {"verdict": "STOP", "reasons": [], "signals": {}, "hard_stop": True}
        self.assertEqual(apply_asset_registry(stop, rec)["verdict"], "STOP")

    def test_none_record_noop_nonmutating(self):
        base = self._go()
        self.assertEqual(apply_asset_registry(base, None), base)
        self.assertNotIn("rwa_asset", base["signals"])


class TestForecastEnrichment(unittest.TestCase):
    def test_known_token_annotates_forecast(self):
        import blackwall
        reg = TokenizedStockRegistry().add(parse_backed_assets(BACKED_SAMPLE))
        payload = {"counterparty": "0xKNOWNGOOD000000000000000000000000000001",
                   "amount": "0.09", "asset": "USDC", "chain": "base",
                   "resource": "https://api.example.com/buy",
                   "acquires": {"token": EVM, "chain": "ethereum"}}
        out = blackwall.forecast(payload, blackwall.MockReputationSource(),
                                 stock_registry=reg)[0]
        self.assertEqual(out["signals"]["rwa_asset"]["symbol"], "NVDAx")

    def test_halted_token_holds_forecast(self):
        import blackwall
        reg = TokenizedStockRegistry().add(parse_seed(
            [{"symbol": "HALTx", "issuer": "backed", "trading_halted": True,
              "deployments": [{"network": "base", "address": EVM}]}]))
        payload = {"counterparty": "0xKNOWNGOOD000000000000000000000000000001",
                   "amount": "0.09", "asset": "USDC", "chain": "base",
                   "resource": "https://api.example.com/buy",
                   "acquires": {"token": EVM, "chain": "base"}}
        out = blackwall.forecast(payload, blackwall.MockReputationSource(),
                                 stock_registry=reg)[0]
        self.assertEqual(out["verdict"], "HOLD")


if __name__ == "__main__":
    unittest.main()
