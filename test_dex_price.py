#!/usr/bin/env python3
"""
test_dex_price.py -- TDD for the DEX (Uniswap-v3) market-price read + market-vs-NAV peg
gate. Each test names the MUTATION it kills.
"""
import unittest

import dex_price as dp
from dex_price import (DexPriceSource, apply_market_peg, assess_market_peg,
                       dex_token_price)
from rwa_readiness import selector

Q96 = 2 ** 96
TOKEN = "0x" + "22" * 20
USDC_ETH = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
POOL = "0x" + "99" * 20


def _sqrt_for(price, dec0, dec1, token_is_token0):
    """Construct a sqrtPriceX96 that yields `price` for the token, to test decoding."""
    # t1_per_t0 (human) = price if token is token0, else 1/price
    t1_per_t0 = price if token_is_token0 else (1.0 / price)
    raw = t1_per_t0 / (10 ** (dec0 - dec1))
    return int((raw ** 0.5) * Q96)


class TestPriceMath(unittest.TestCase):
    def test_one_to_one_equal_decimals(self):
        self.assertAlmostEqual(dex_token_price(Q96, 18, 18, True), 1.0, places=6)

    def test_token_is_token0(self):
        s = _sqrt_for(150.0, 18, 6, True)      # token(18) is token0, quote USDC(6) token1
        self.assertAlmostEqual(dex_token_price(s, 18, 6, True), 150.0, places=2)

    def test_token_is_token1(self):
        # MUTATION: not taking the reciprocal when token is token1 flips the price.
        s = _sqrt_for(150.0, 6, 18, False)     # quote USDC(6) token0, token(18) token1
        self.assertAlmostEqual(dex_token_price(s, 6, 18, False), 150.0, places=2)

    def test_zero_and_bad_none(self):
        self.assertIsNone(dex_token_price(0, 18, 6, True))
        self.assertIsNone(dex_token_price(-1, 18, 6, True))
        self.assertIsNone(dex_token_price("x", 18, 6, True))

    def test_nonfinite_inputs_no_crash(self):
        # AUDIT REGRESSION: int(inf) raises OverflowError -- must be caught -> None.
        self.assertIsNone(dex_token_price(float("inf"), 18, 6, True))
        self.assertIsNone(dex_token_price(float("nan"), 18, 6, True))
        self.assertIsNone(dex_token_price(1 << 160, None, 6, True))   # bad decimals


class TestAssessMarketPeg(unittest.TestCase):
    def test_on_peg(self):
        self.assertEqual(assess_market_peg(101.0, 100.0)["grade"], "on_peg")

    def test_off_peg_discount(self):
        # MUTATION: only flagging premiums (ratio>1) would miss a distressed discount.
        r = assess_market_peg(80.0, 100.0)
        self.assertEqual(r["grade"], "off_peg")
        self.assertTrue(any("below" in x for x in r["reasons"]))

    def test_off_peg_premium(self):
        r = assess_market_peg(130.0, 100.0)
        self.assertEqual(r["grade"], "off_peg")
        self.assertTrue(any("above" in x for x in r["reasons"]))

    def test_unknown_on_missing(self):
        self.assertEqual(assess_market_peg(None, 100.0)["grade"], "unknown")
        self.assertEqual(assess_market_peg(100.0, 0)["grade"], "unknown")


class TestApplyMarketPeg(unittest.TestCase):
    def _go(self):
        return {"verdict": "GO", "reasons": [], "signals": {}}

    def test_off_peg_holds(self):
        out = apply_market_peg(self._go(), assess_market_peg(70.0, 100.0))
        self.assertEqual(out["verdict"], "HOLD")
        self.assertEqual(out["signals"]["dex_market"]["standard"], "dex-market")

    def test_on_peg_stays_go(self):
        self.assertEqual(apply_market_peg(self._go(),
                         assess_market_peg(100.0, 100.0))["verdict"], "GO")

    def test_never_touches_stop(self):
        stop = {"verdict": "STOP", "reasons": [], "signals": {}}
        self.assertEqual(apply_market_peg(stop, assess_market_peg(50.0, 100.0))["verdict"],
                         "STOP")

    def test_none_noop_nonmutating(self):
        base = self._go()
        self.assertEqual(apply_market_peg(base, None), base)
        self.assertNotIn("dex_market", base["signals"])


class _Chain:
    """Injected eth_call routing (to, selector[, first-address-arg]) -> canned result."""

    def __init__(self, table):
        self.table = {(k[0].lower(), selector(k[1])): v for k, v in table.items()}

    def __call__(self, to, data):
        return self.table.get((to.lower(), data[:10]))


def _word(n):
    return "0x" + format(n, "064x")


def _addr_word(a):
    return "0x" + "0" * 24 + a[2:].lower()


class TestDexPriceSource(unittest.TestCase):
    def _price_via(self, token_is_token0):
        sqrtp = _sqrt_for(200.0, 18 if token_is_token0 else 6,
                          6 if token_is_token0 else 18, token_is_token0)
        token0 = TOKEN if token_is_token0 else USDC_ETH
        chain = _Chain({
            (POOL, "slot0()"): _word(sqrtp) + "00" * 32 * 2,   # sqrt + extra words
            (POOL, "token0()"): _addr_word(token0),
            (TOKEN, "decimals()"): _word(18),
        })
        src = DexPriceSource(eth_call=chain)
        return src.price(TOKEN, "ethereum", pool=POOL)

    def test_explicit_pool_token0(self):
        self.assertAlmostEqual(self._price_via(True), 200.0, places=1)

    def test_explicit_pool_token1(self):
        self.assertAlmostEqual(self._price_via(False), 200.0, places=1)

    def test_pool_discovery_via_factory(self):
        sqrtp = _sqrt_for(50.0, 18, 6, True)
        factory = dp.DEX_CONFIG["ethereum"]["factory"]
        chain = _Chain({
            (factory, "getPool(address,address,uint24)"): _addr_word(POOL),
            (POOL, "slot0()"): _word(sqrtp) + "00" * 64,
            (POOL, "token0()"): _addr_word(TOKEN),
            (TOKEN, "decimals()"): _word(18),
        })
        self.assertAlmostEqual(DexPriceSource(eth_call=chain).price(TOKEN, "ethereum"),
                               50.0, places=1)

    def test_no_pool_found_none(self):
        # factory returns zero address for all fee tiers -> None.
        self.assertIsNone(DexPriceSource(eth_call=_Chain({})).price(TOKEN, "ethereum"))

    def test_unsupported_chain_none(self):
        self.assertIsNone(DexPriceSource(eth_call=_Chain({})).price(TOKEN, "dogechain"))

    def test_non_evm_token_none(self):
        self.assertIsNone(DexPriceSource(eth_call=_Chain({})).price("AAPLx", "ethereum"))

    def test_transport_raises_fail_open(self):
        def boom(to, data):
            raise RuntimeError("rpc down")
        self.assertIsNone(DexPriceSource(eth_call=boom).price(TOKEN, "ethereum", pool=POOL))


if __name__ == "__main__":
    unittest.main()
