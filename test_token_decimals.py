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
        #
        # The caller value is still used where it is the ONLY source: an asset the
        # table does not list and no resolver is installed for
        # (test_explicit_decimals_used_for_an_asset_we_cannot_identify).
        PS.set_onchain_resolver(TD.OnChainDecimals(
            rpc_url="http://node", table={}, transport=lambda u, b: {"result": word(8)}))
        self.assertEqual(PS.resolve_decimals({"asset": "0x" + "9" * 40}, 18), 8)
        # and the disagreement is reported rather than silently resolved
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
