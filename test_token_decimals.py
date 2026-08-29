"""Tests for the on-chain decimals resolver. Each names the mutation it kills."""

import threading
import unittest

import payload_sim as PS
import token_decimals as TD


def word(value):
    return "0x" + format(value, "064x")


class TestDecode(unittest.TestCase):
    def test_common_values(self):
        self.assertEqual(TD.decode_decimals(word(6)), 6)
        self.assertEqual(TD.decode_decimals(word(18)), 18)
        self.assertEqual(TD.decode_decimals(word(0)), 0)

    def test_empty_result_is_none(self):
        # kills: treating a reverted call or a non-token address as 0 decimals,
        # which would scale every amount by 10^0 and silently pass
        self.assertIsNone(TD.decode_decimals("0x"))
        self.assertIsNone(TD.decode_decimals(""))
        self.assertIsNone(TD.decode_decimals(None))

    def test_implausible_value_rejected(self):
        # kills: feeding an arbitrary contract's answer into 10**n. A hostile
        # token returning 2^256-1 must not reach the scaling math.
        self.assertIsNone(TD.decode_decimals(word(2 ** 256 - 1)))
        self.assertIsNone(TD.decode_decimals(word(37)))

    def test_boundary_is_inclusive(self):
        # kills: an off-by-one that rejects a legitimate high-precision token
        self.assertEqual(TD.decode_decimals(word(36)), 36)

    def test_garbage_is_none(self):
        # kills: raising on a malformed hex string
        self.assertIsNone(TD.decode_decimals("0xnothex"))


class TestEnabled(unittest.TestCase):
    def test_off_by_default(self):
        # kills: network on the hot path without the operator opting in
        self.assertFalse(TD.enabled({}))

    def test_recognised_truthy_values(self):
        for v in ("1", "true", "yes", "on", "TRUE"):
            self.assertTrue(TD.enabled({TD.ENV_FLAG: v}), v)

    def test_other_values_are_off(self):
        # kills: treating "0"/"false" as enabled
        for v in ("0", "false", "no", ""):
            self.assertFalse(TD.enabled({TD.ENV_FLAG: v}), v)


class TestLookup(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def transport(url, body):
            self.calls.append(body["params"][0]["to"])
            return {"result": word(18)}

        self.r = TD.OnChainDecimals(rpc_url="http://node", transport=transport,
                                    table={"0xknown": 6})

    def test_static_table_wins_without_a_call(self):
        # kills: hitting the network for an audited, on-chain-verified entry --
        # that is a needless query leak and latency on every payment
        self.assertEqual(self.r.lookup("0xKNOWN"), 6)
        self.assertEqual(self.calls, [])

    def test_unknown_token_is_read_on_chain(self):
        self.assertEqual(self.r.lookup("0x" + "9" * 40), 18)
        self.assertEqual(len(self.calls), 1)

    def test_cached_forever(self):
        # kills: a TTL. decimals() is immutable, so re-asking only re-leaks the
        # query -- this must cost ONE call per token ever, not one per payment.
        for _ in range(5):
            self.r.lookup("0x" + "9" * 40)
        self.assertEqual(len(self.calls), 1)

    def test_a_miss_is_cached_too(self):
        # kills: re-querying a non-token address on every payment
        r = TD.OnChainDecimals(rpc_url="http://node", table={},
                               transport=lambda u, b: {"result": "0x"})
        self.assertIsNone(r.lookup("0x" + "d" * 40))
        self.assertIsNone(r.lookup("0x" + "d" * 40))
        self.assertEqual(r.upstream_calls, 1)

    def test_non_evm_asset_is_not_queried(self):
        # kills: sending a Solana mint to eth_call -- it cannot answer and the
        # query leaks for nothing
        self.assertIsNone(self.r.lookup("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"))
        self.assertEqual(self.calls, [])

    def test_no_rpc_url_is_none_not_an_error(self):
        # kills: raising when the resolver is half-configured
        r = TD.OnChainDecimals(rpc_url=None, table={})
        self.assertIsNone(r.lookup("0x" + "9" * 40))

    def test_transport_failure_fails_open(self):
        # kills: an unreachable node propagating an exception into the verdict
        def boom(url, body):
            raise OSError("unreachable")
        r = TD.OnChainDecimals(rpc_url="http://node", transport=boom, table={})
        self.assertIsNone(r.lookup("0x" + "9" * 40))

    def test_rpc_error_response_fails_open(self):
        # kills: reading `result` out of an error response
        r = TD.OnChainDecimals(rpc_url="http://node", table={},
                               transport=lambda u, b: {"error": {"message": "nope"}})
        self.assertIsNone(r.lookup("0x" + "9" * 40))

    def test_concurrent_lookups_coalesce(self):
        # kills: N concurrent payments in the same asset costing N disclosures --
        # the same audit finding that motivated SingleFlight in rpc_node
        started = threading.Barrier(8)

        def slow(url, body):
            self.calls.append(body["params"][0]["to"])
            return {"result": word(18)}

        r = TD.OnChainDecimals(rpc_url="http://node", transport=slow, table={})
        out = []

        def run():
            started.wait()
            out.append(r.lookup("0x" + "7" * 40))

        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(set(out), {18})
        self.assertEqual(r.upstream_calls, 1)


class TestResolverFactory(unittest.TestCase):
    def test_returns_none_when_disabled(self):
        # kills: constructing a live resolver without opt-in
        self.assertIsNone(TD.resolver(transport=lambda u, b: {}, env={}))

    def test_returns_none_without_an_rpc(self):
        # kills: a resolver that is enabled but has nowhere to ask
        self.assertIsNone(TD.resolver(env={TD.ENV_FLAG: "1"}))

    def test_builds_when_enabled_and_configured(self):
        r = TD.resolver(env={TD.ENV_FLAG: "1", TD.ENV_RPC: "http://node"})
        self.assertIsInstance(r, TD.OnChainDecimals)


class TestPayloadSimIntegration(unittest.TestCase):
    def tearDown(self):
        PS.set_onchain_resolver(None)

    def test_unknown_asset_resolves_when_installed(self):
        # kills: the residual the audit left open -- an unlisted token was safe
        # but never verified
        PS.set_onchain_resolver(TD.OnChainDecimals(
            rpc_url="http://node", table={}, transport=lambda u, b: {"result": word(8)}))
        self.assertEqual(PS.resolve_decimals({"asset": "0x" + "9" * 40}), 8)

    def test_default_is_still_offline(self):
        # kills: the resolver becoming active without being installed
        self.assertIsNone(PS.resolve_decimals({"asset": "0x" + "9" * 40}))

    def test_the_chain_beats_a_caller_assertion(self):
        # kills: a caller-supplied value overriding what the TOKEN ITSELF reports.
        #
        # REVISED (audit 2026-08-29): this asserted the opposite -- that the caller
        # wins over the on-chain read -- framed as "a value the caller already
        # knows". But `methodDetails.decimals` comes from the 402 CHALLENGE, which
        # is authored by the PAYEE: the party this gate exists to screen. A
        # `decimals()` read is the token contract's own answer. For a check whose
        # whole guarantee is that the screened party cannot re-scale it, ground
        # truth must beat an assertion. The same inversion let a request downgrade
        # a hard STOP to HOLD -- see
        # TestRequestSuppliedDecimalsCannotOverrideKnownAsset in test_payload_sim.
        PS.set_onchain_resolver(TD.OnChainDecimals(
            rpc_url="http://node", table={}, transport=lambda u, b: {"result": word(8)}))
        self.assertEqual(PS.resolve_decimals({"asset": "0x" + "9" * 40}, 18), 8)
        self.assertTrue(PS.decimals_conflict({"asset": "0x" + "9" * 40}, 18))

    def test_a_resolver_that_raises_fails_open(self):
        # kills: a broken resolver crashing the verdict path
        class Boom:
            def lookup(self, a):
                raise RuntimeError("bad")
        PS.set_onchain_resolver(Boom())
        self.assertIsNone(PS.resolve_decimals({"asset": "0x" + "9" * 40}))


if __name__ == "__main__":
    unittest.main()


class TestSolana(unittest.TestCase):
    """SPL mint accounts are an 82-byte layout with decimals at offset 44 --
    verified live against USDC on mainnet-beta."""

    def _mint(self, decimals, length=82):
        import base64
        raw = bytearray(length)
        if length > 44:
            raw[44] = decimals
        return base64.b64encode(bytes(raw)).decode()

    def test_decodes_mint_decimals(self):
        self.assertEqual(TD.decode_spl_decimals(self._mint(6)), 6)
        self.assertEqual(TD.decode_spl_decimals(self._mint(9)), 9)

    def test_rejects_short_account(self):
        # kills: reading offset 44 of a TOKEN ACCOUNT (165 bytes) or any other
        # account -- that byte is part of somebody's balance, not decimals
        self.assertIsNone(TD.decode_spl_decimals(self._mint(6, length=40)))

    def test_rejects_implausible(self):
        # kills: a corrupt byte reaching 10**n
        self.assertIsNone(TD.decode_spl_decimals(self._mint(200)))

    def test_garbage_is_none(self):
        self.assertIsNone(TD.decode_spl_decimals("not base64!!"))
        self.assertIsNone(TD.decode_spl_decimals(None))
        self.assertIsNone(TD.decode_spl_decimals(""))

    def test_lookup_routes_base58_to_solana(self):
        # kills: sending a base58 mint to eth_call, which cannot answer it
        seen = []

        def transport(url, body):
            seen.append(body["method"])
            return {"result": {"value": {"data": [self._mint(6), "base64"]}}}

        r = TD.OnChainDecimals(rpc_url="http://evm", solana_rpc="http://sol",
                               transport=transport, table={})
        self.assertEqual(r.lookup("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"), 6)
        self.assertEqual(seen, ["getAccountInfo"])

    def test_base58_case_is_preserved(self):
        # kills: lowercasing the mint before the call -- base58 is case-SENSITIVE
        # and a lowercased mint is a different (nonexistent) account
        sent = []

        def transport(url, body):
            sent.append(body["params"][0])
            return {"result": {"value": {"data": [self._mint(6), "base64"]}}}

        r = TD.OnChainDecimals(solana_rpc="http://sol", transport=transport, table={})
        mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        r.lookup(mint)
        self.assertEqual(sent, [mint])

    def test_no_solana_rpc_means_unknown(self):
        # kills: attempting a Solana lookup with nowhere to ask
        r = TD.OnChainDecimals(rpc_url="http://evm", transport=lambda u, b: {}, table={})
        self.assertIsNone(r.lookup("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"))


class TestZeroDecimalsVerification(unittest.TestCase):
    """A contract with a catch-all fallback returning zeros is indistinguishable
    from a genuine 0-decimal token, and reading 0 for an 18-decimal asset
    mis-scales by 10^18."""

    def _resolver(self, supply_result):
        calls = []

        def transport(url, body):
            sel = body["params"][0]["data"]
            calls.append(sel)
            if sel == TD.DECIMALS_SELECTOR:
                return {"result": "0x" + "0" * 64}
            return {"result": supply_result}

        return TD.OnChainDecimals(rpc_url="http://n", transport=transport,
                                  table={}), calls

    def test_zero_confirmed_by_total_supply(self):
        # kills: rejecting every 0-decimal token, which are legitimate
        r, calls = self._resolver("0x" + "0" * 63 + "1")
        self.assertEqual(r.lookup("0x" + "a" * 40), 0)
        self.assertIn(TD.TOTAL_SUPPLY_SELECTOR, calls)

    def test_zero_rejected_when_not_a_token(self):
        # kills: accepting 0 from a contract that merely returns zeros --
        # scaling an 18-decimal amount by 10^0 is a 10^18 error
        r, _ = self._resolver("0x")
        self.assertIsNone(r.lookup("0x" + "a" * 40))

    def test_non_zero_needs_no_second_call(self):
        # kills: paying for an extra round trip on the common path
        calls = []

        def transport(url, body):
            calls.append(body["params"][0]["data"])
            return {"result": "0x" + "0" * 62 + "12"}

        r = TD.OnChainDecimals(rpc_url="http://n", transport=transport, table={})
        self.assertEqual(r.lookup("0x" + "a" * 40), 18)
        self.assertEqual(calls, [TD.DECIMALS_SELECTOR])


class TestCrossAssetPricingCapability(unittest.TestCase):
    """The capability the decimals work actually buys, pinned so it cannot
    silently regress.

    MEASURED 2026-08-29: x402 never transmits `decimals` (0 of 298 live accepts
    entries), so every client must assume one. The ecosystem assumes 6. But 33%
    of live entries use a non-6dp asset -- including Nansen and CoinMarketCap,
    both charging $0.01 in an 18-decimal BSC stablecoin. Read at 6dp that is
    $10,000,000,000, and any spending cap or price gate rejects a one-cent API
    call.
    """

    USD1 = "0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d"   # 18dp, verified on-chain
    USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"   # 6dp

    def test_eighteen_decimal_asset_resolves(self):
        # kills: dropping the BSC entries from the table, which silently returns
        # the whole class of assets to "unknown"
        self.assertEqual(PS.resolve_decimals({"asset": self.USD1}), 18)

    def test_naive_and_correct_reads_differ_by_ten_to_the_twelve(self):
        # kills: any change that makes the 6dp assumption look harmless. This is
        # the exact magnitude that turns $0.01 into $10,000,000,000.
        atomic = 10 ** 10  # 0.01 USD1 in atomic units
        naive = atomic / 10 ** 6
        real = atomic / 10 ** PS.resolve_decimals({"asset": self.USD1})
        self.assertEqual(naive / real, 10 ** 12)

    def test_a_real_one_cent_payment_is_not_blocked(self):
        # kills: the capability itself. A correctly-scaled 0.01 payment against a
        # payee whose median is 0.01 must clear; under the naive read the price
        # gate sees 10^12x the median and STOPs a legitimate API call.
        import blackwall

        class Rep:
            def lookup(self, addr):
                return {"settlement_count": 40, "distinct_payers": 12,
                        "dispute_rate": 0.0,
                        "price_history": ["0.01", "0.01", "0.012", "0.009"]}

        body = {"amount": "0.01", "asset": self.USD1, "chain": "eip155:56",
                "counterparty": "0x" + "a" * 40}
        resp, err = blackwall.forecast(body, reputation_source=Rep(), hold_above=100)
        self.assertIsNone(err)
        self.assertEqual(resp["verdict"], "GO")

    def test_the_naive_read_would_have_been_stopped(self):
        # kills: a change that makes both reads agree, which would mean the
        # decimals resolution had stopped mattering
        import blackwall

        class Rep:
            def lookup(self, addr):
                return {"settlement_count": 40, "distinct_payers": 12,
                        "dispute_rate": 0.0,
                        "price_history": ["0.01", "0.01", "0.012", "0.009"]}

        body = {"amount": "10000000000", "asset": self.USD1, "chain": "eip155:56",
                "counterparty": "0x" + "a" * 40}
        resp, err = blackwall.forecast(body, reputation_source=Rep(), hold_above=100)
        self.assertEqual(resp["verdict"], "STOP")


class TestChainRouting(unittest.TestCase):
    """AUDIT BUG (2026-08-29): one rpc_url served EVERY asset regardless of chain.
    Asked about a Celo token while configured with a BSC node it queried BSC, got
    a plausible 18, and cached it FOREVER -- poisoning every future payment in
    that asset. Live entries span 12+ chains, so this was not theoretical."""

    CELO = "0xceba9300f2b948710d2653dd7b07f33a8b32118c"

    def setUp(self):
        self.calls = []

        def transport(url, body):
            self.calls.append((url, body["params"][0]["to"]))
            return {"result": word(18)}

        self.transport = transport

    def test_chain_of(self):
        self.assertEqual(TD.chain_of("eip155:8453"), "8453")
        self.assertEqual(TD.chain_of("8453"), "8453")

    def test_non_evm_namespace_is_not_a_chain_id(self):
        # kills: treating "solana:EtWTRABZ" as chain "EtWTRABZ" and sending an
        # eth_call to whatever endpoint happens to be configured
        self.assertIsNone(TD.chain_of("solana:EtWTRABZa"))
        self.assertIsNone(TD.chain_of("algorand:wGHE2Pw"))
        self.assertIsNone(TD.chain_of(None))

    def test_parse_rpc_map(self):
        self.assertEqual(TD.parse_rpc_map("8453=https://a,56=https://b"),
                         {"8453": "https://a", "56": "https://b"})
        self.assertEqual(TD.parse_rpc_map("garbage,=,x="), {})
        self.assertEqual(TD.parse_rpc_map(None), {})

    def test_refuses_to_ask_a_different_chain(self):
        # kills the bug itself: an unconfigured chain must resolve to unknown,
        # NOT be answered by whatever endpoint is to hand
        r = TD.OnChainDecimals(rpc_urls={"56": "https://bsc"},
                               transport=self.transport, table={})
        self.assertIsNone(r.lookup(self.CELO, "eip155:42220"))
        self.assertEqual(self.calls, [])

    def test_uses_the_endpoint_for_the_right_chain(self):
        r = TD.OnChainDecimals(rpc_urls={"56": "https://bsc", "8453": "https://base"},
                               transport=self.transport, table={})
        r.lookup("0x" + "a" * 40, "eip155:8453")
        self.assertEqual(self.calls[0][0], "https://base")

    def test_same_address_on_two_chains_is_not_conflated(self):
        # kills: a cache keyed on the asset alone. The same address is a
        # different token on a different chain, and answers are cached forever,
        # so one key would let one chain's answer serve the other's.
        r = TD.OnChainDecimals(rpc_urls={"56": "https://bsc", "8453": "https://base"},
                               transport=self.transport, table={})
        r.lookup("0x" + "b" * 40, "eip155:56")
        r.lookup("0x" + "b" * 40, "eip155:8453")
        self.assertEqual(r.upstream_calls, 2)

    def test_legacy_single_endpoint_needs_its_chain_stated(self):
        # kills: silently reusing a bare rpc_url for a named chain it may not
        # speak for
        r = TD.OnChainDecimals(rpc_url="https://bsc", chain="eip155:56",
                               transport=self.transport, table={})
        self.assertEqual(r.lookup("0x" + "a" * 40, "eip155:56"), 18)
        self.calls.clear()
        self.assertIsNone(r.lookup("0x" + "a" * 40, "eip155:8453"))
        self.assertEqual(self.calls, [])

    def test_no_network_still_works_for_a_single_chain_setup(self):
        # kills: breaking a deliberate one-chain deployment that passes no network
        r = TD.OnChainDecimals(rpc_url="https://only", transport=self.transport, table={})
        self.assertEqual(r.lookup("0x" + "a" * 40), 18)

    def test_table_still_wins_without_any_endpoint(self):
        # kills: requiring an RPC for assets we already know offline
        r = TD.OnChainDecimals(table={"0xknown": 6}, transport=self.transport)
        self.assertEqual(r.lookup("0xKNOWN", "eip155:42220"), 6)
        self.assertEqual(self.calls, [])
