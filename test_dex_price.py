#!/usr/bin/env python3
"""
test_dex_price.py -- TDD for the DEX (Uniswap-v3) market-price read + market-vs-NAV peg
gate. Each test names the MUTATION it kills.
"""
import unittest

import dex_price as dp
from dex_price import (DexPriceSource, apply_market_peg, assess_execution,
                       assess_market_peg, dex_token_price,
                       encode_quote_exact_input_single)
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


class TestQuoterEncode(unittest.TestCase):
    def test_selector_and_five_words(self):
        data = encode_quote_exact_input_single(TOKEN, USDC_ETH, 1000000, 500)
        self.assertTrue(data.startswith(selector(
            "quoteExactInputSingle((address,address,uint256,uint24,uint160))")))
        self.assertEqual(len(data), 10 + 64 * 5)      # inline static tuple = 5 words

    def test_encode_never_raises_on_garbage(self):
        # AUDIT REGRESSION: ABI encoders must not crash (matches eth_call_data).
        for bad in (None, "", {}, float("inf"), float("nan"), b"x"):
            d = encode_quote_exact_input_single(bad, bad, bad, bad)
            self.assertEqual(len(d), 10 + 64 * 5)


class TestAssessExecution(unittest.TestCase):
    def test_on_peg_low_slippage(self):
        self.assertEqual(assess_execution(101.0, 100.0, 100.5)["grade"], "on_peg")

    def test_off_peg_on_nav(self):
        r = assess_execution(130.0, 100.0, 129.0)
        self.assertEqual(r["grade"], "off_peg")
        self.assertTrue(any("NAV" in x for x in r["reasons"]))

    def test_off_peg_on_high_slippage(self):
        # effective 120 vs mid 100 = 20% slippage -> thin liquidity flag even if NAV ok.
        # MUTATION: comparing only to NAV (not the mid) would miss size-based slippage.
        r = assess_execution(120.0, 118.0, 100.0)
        self.assertEqual(r["grade"], "off_peg")
        self.assertTrue(any("slippage" in x for x in r["reasons"]))

    def test_unknown_on_missing_price(self):
        self.assertEqual(assess_execution(None, 100.0, 100.0)["grade"], "unknown")


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


DEEP = 50_000 * 10 ** 6      # $50k USDC -> clears the dust floor
DUST = 100 * 10 ** 6         # $100 USDC -> below the floor


class TestDexPriceSource(unittest.TestCase):
    def _price_via(self, token_is_token0):
        sqrtp = _sqrt_for(200.0, 18 if token_is_token0 else 6,
                          6 if token_is_token0 else 18, token_is_token0)
        token0 = TOKEN if token_is_token0 else USDC_ETH
        chain = _Chain({
            (POOL, "slot0()"): _word(sqrtp) + "00" * 32 * 2,   # sqrt + extra words
            (POOL, "token0()"): _addr_word(token0),
            (TOKEN, "decimals()"): _word(18),
            (USDC_ETH, "balanceOf(address)"): _word(DEEP),     # pool has real liquidity
        })
        src = DexPriceSource(eth_call=chain)
        return src.price(TOKEN, "ethereum", pool=POOL)

    def test_explicit_pool_token0(self):
        self.assertAlmostEqual(self._price_via(True), 200.0, places=1)

    def test_explicit_pool_token1(self):
        self.assertAlmostEqual(self._price_via(False), 200.0, places=1)

    def test_explicit_dust_pool_rejected(self):
        # MUTATION: not depth-checking an explicit pool lets a manipulated thin pool set
        # the price -> a false off-peg. A dust pool must yield None.
        sqrtp = _sqrt_for(200.0, 18, 6, True)
        chain = _Chain({
            (POOL, "slot0()"): _word(sqrtp) + "00" * 64,
            (POOL, "token0()"): _addr_word(TOKEN),
            (TOKEN, "decimals()"): _word(18),
            (USDC_ETH, "balanceOf(address)"): _word(DUST),
        })
        self.assertIsNone(DexPriceSource(eth_call=chain).price(TOKEN, "ethereum", pool=POOL))

    def test_pool_discovery_via_factory(self):
        sqrtp = _sqrt_for(50.0, 18, 6, True)
        factory = dp.DEX_CONFIG["ethereum"]["factory"]
        chain = _Chain({
            (factory, "getPool(address,address,uint24)"): _addr_word(POOL),
            (POOL, "slot0()"): _word(sqrtp) + "00" * 64,
            (POOL, "token0()"): _addr_word(TOKEN),
            (TOKEN, "decimals()"): _word(18),
            (USDC_ETH, "balanceOf(address)"): _word(DEEP),
        })
        self.assertAlmostEqual(DexPriceSource(eth_call=chain).price(TOKEN, "ethereum"),
                               50.0, places=1)

    def test_discovery_picks_deepest_pool(self):
        # MUTATION: first-found (not deepest) would pick the thin 0.05% pool and its
        # wrong price. The 0.3% pool here is deeper -> its price (60) must win over the
        # 0.05% pool's (40).
        factory = dp.DEX_CONFIG["ethereum"]["factory"]
        POOL_THIN = "0x" + "77" * 20
        POOL_DEEP = "0x" + "88" * 20
        sel_getpool = selector("getPool(address,address,uint24)")

        def chain(to, data):
            tl = to.lower()
            if tl == factory and data[:10] == sel_getpool:
                fee = int(data[-64:], 16)
                return _addr_word(POOL_THIN if fee == 500 else
                                  (POOL_DEEP if fee == 3000 else "0x" + "00" * 20))
            if data[:10] == selector("balanceOf(address)"):
                who = "0x" + data[10 + 24:10 + 64]
                return _word(DUST if who.lower() == POOL_THIN.lower() else DEEP)
            if data[:10] == selector("slot0()"):
                px = 40.0 if tl == POOL_THIN.lower() else 60.0
                return _word(_sqrt_for(px, 18, 6, True)) + "00" * 64
            if data[:10] == selector("token0()"):
                return _addr_word(TOKEN)
            if data[:10] == selector("decimals()"):
                return _word(18)
            return None
        self.assertAlmostEqual(DexPriceSource(eth_call=chain).price(TOKEN, "ethereum"),
                               60.0, places=1)

    def test_executable_price_via_quoter(self):
        # Buy $1000 of a $200 token (18dp) -> 5 tokens out -> effective $200; spot 200.
        factory = dp.DEX_CONFIG["ethereum"]["factory"]
        quoter = dp.DEX_CONFIG["ethereum"]["quoter"]
        sqrtp = _sqrt_for(200.0, 18, 6, True)
        chain = _Chain({
            (factory, "getPool(address,address,uint24)"): _addr_word(POOL),
            (USDC_ETH, "balanceOf(address)"): _word(DEEP),
            (TOKEN, "decimals()"): _word(18),
            (quoter, "quoteExactInputSingle((address,address,uint256,uint24,uint160))"):
                _word(5 * 10 ** 18) + "00" * 32 * 3,        # amountOut + 3 extra words
            (POOL, "slot0()"): _word(sqrtp) + "00" * 64,
            (POOL, "token0()"): _addr_word(TOKEN),
        })
        r = DexPriceSource(eth_call=chain).executable_price(TOKEN, "ethereum", 1000)
        self.assertAlmostEqual(r["effective_price"], 200.0, places=1)
        self.assertAlmostEqual(r["slippage"], 0.0, places=3)

    def test_executable_price_dust_pool_none(self):
        factory = dp.DEX_CONFIG["ethereum"]["factory"]
        chain = _Chain({
            (factory, "getPool(address,address,uint24)"): _addr_word(POOL),
            (USDC_ETH, "balanceOf(address)"): _word(DUST),   # below floor
        })
        self.assertIsNone(DexPriceSource(eth_call=chain).executable_price(TOKEN, "ethereum", 1000))

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
