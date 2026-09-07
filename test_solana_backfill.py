"""
Tests for solana_backfill.py.

The one that matters most is `TestCollectSignatures`. Solana history is paged
backwards and a short page is ambiguous -- genuinely the end, or a rate-limited
response. Believing a short page cost 29% of the corpus during sizing (66,209
signatures reported against a true 93,876) and announced nothing. Those tests
pin the confirm-then-believe behaviour and the refusal to return a truncated
list.

Each test names the mutation it kills.
"""
import unittest

import solana_backfill as S

MINT = S.SOLANA_USDC
PAYEE_ACCT = "PayeeTokenAccount1111111111111111111111111"
PAYEE_OWNER = "PayeeOwner11111111111111111111111111111111"
BUYER_ACCT = "BuyerTokenAccount1111111111111111111111111"
BUYER_OWNER = "BuyerOwner11111111111111111111111111111111"


def bal(idx, amount, owner, mint=MINT, decimals=6):
    return {"accountIndex": idx, "mint": mint, "owner": owner,
            "uiTokenAmount": {"amount": str(amount), "decimals": decimals,
                              "uiAmount": amount / float(10 ** decimals)}}


def transfer_tx(amount=10000, *, parsed_keys=True, err=None, decimals=6,
                mint=MINT, extra_pre=(), extra_post=()):
    """A buyer paying the payee `amount` base units."""
    keys = [BUYER_ACCT, PAYEE_ACCT]
    if parsed_keys:
        keys = [{"pubkey": k} for k in keys]
    return {"blockTime": 1770000000, "slot": 400000000,
            "transaction": {"message": {"accountKeys": keys}},
            "meta": {"err": err,
                     "preTokenBalances": [bal(0, 50000, BUYER_OWNER, mint, decimals),
                                          bal(1, 0, PAYEE_OWNER, mint, decimals)]
                                         + list(extra_pre),
                     "postTokenBalances": [bal(0, 50000 - amount, BUYER_OWNER, mint, decimals),
                                           bal(1, amount, PAYEE_OWNER, mint, decimals)]
                                          + list(extra_post)}}


class FakeRPC:
    """Scripted transport. `responses` maps method -> list of results or
    Exceptions, consumed in order."""

    def __init__(self, responses):
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls = []

    def __call__(self, method, params=None):
        self.calls.append((method, params))
        queue = self.responses.get(method)
        if not queue:
            raise AssertionError("unscripted call: %s %r" % (method, params))
        out = queue.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def sigs(n, start=0):
    return [{"signature": "sig%d" % (start + i), "blockTime": 1770000000 - i}
            for i in range(n)]


class TestUsdcDelta(unittest.TestCase):
    def test_inbound_delta_in_base_units(self):
        # Mutation: read uiAmount (a float) -> a decimal-converted amount picks
        # up representation noise, which volume_integrity reads as price
        # VARIETY, killing the one signal a backfill cannot fake.
        self.assertEqual(S.usdc_delta(transfer_tx(10000), PAYEE_ACCT), 10000)
        self.assertIsInstance(S.usdc_delta(transfer_tx(10000), PAYEE_ACCT), int)

    def test_legacy_account_keys_encoding(self):
        # Mutation: handle only the jsonParsed dict shape -> every transaction
        # served the legacy way parses as "no history", which is indistinguishable
        # from a payee that was never paid.
        self.assertEqual(S.usdc_delta(transfer_tx(7, parsed_keys=False), PAYEE_ACCT), 7)

    def test_failed_transaction_is_not_a_payment(self):
        # Mutation: ignore meta.err -> reverted transfers count as revenue.
        self.assertIsNone(S.usdc_delta(transfer_tx(10000, err={"x": 1}), PAYEE_ACCT))

    def test_other_mint_is_invisible(self):
        # Identifying the asset by MINT is what stops a lookalike token from
        # contributing history, same rule settlement_watch applies by contract.
        self.assertIsNone(S.usdc_delta(transfer_tx(10000, mint="OtherMint111"), PAYEE_ACCT))

    def test_unknown_account_returns_none(self):
        self.assertIsNone(S.usdc_delta(transfer_tx(), "NotInThisTx1111"))

    def test_outbound_is_negative_not_dropped(self):
        # usdc_delta reports direction; normalize_payment decides. Mutation:
        # abs() here -> outbound spending becomes inbound revenue.
        tx = transfer_tx(10000)
        self.assertEqual(S.usdc_delta(tx, BUYER_ACCT), -10000)

    def test_zero_delta_is_distinct_from_untouched(self):
        # Mutation: return None for 0 -> "touched and netted flat" becomes
        # indistinguishable from "never involved".
        tx = transfer_tx(0)
        self.assertEqual(S.usdc_delta(tx, PAYEE_ACCT), 0)
        self.assertIsNone(S.usdc_delta(tx, "Absent111"))


class TestPayer(unittest.TestCase):
    def test_payer_is_the_owner_wallet_not_the_token_account(self):
        # THE JOIN KEY. Blackwall counts DISTINCT PAYERS and volume_integrity
        # reads these as `buyers`. Mutation: return the token account -> one
        # wallet counts separately per mint, understating concentration and
        # inflating apparent buyer diversity.
        self.assertEqual(S.payer_of(transfer_tx(), PAYEE_ACCT), BUYER_OWNER)

    def test_ambiguous_multi_payer_returns_none(self):
        # Mutation: return the first faller -> a guessed payer becomes a
        # fabricated edge in the payment graph.
        tx = transfer_tx(10000,
                         extra_pre=[bal(2, 900, "SecondPayer111")],
                         extra_post=[bal(2, 100, "SecondPayer111")])
        self.assertIsNone(S.payer_of(tx, PAYEE_ACCT))

    def test_failed_transaction_has_no_payer(self):
        self.assertIsNone(S.payer_of(transfer_tx(err={"x": 1}), PAYEE_ACCT))


class TestNormalize(unittest.TestCase):
    def test_inbound_record(self):
        r = S.normalize_payment(transfer_tx(12345), "sigA", PAYEE_ACCT)
        self.assertEqual(r["amount_raw"], 12345)
        self.assertEqual(r["decimals"], 6)
        self.assertAlmostEqual(r["amount"], 0.012345)
        self.assertEqual(r["payer"], BUYER_OWNER)
        self.assertEqual(r["signature"], "sigA")

    def test_outbound_is_not_a_payment(self):
        # Mutation: keep outbound -> a payee's own spending inflates its volume.
        self.assertIsNone(S.normalize_payment(transfer_tx(10000), "s", BUYER_ACCT))

    def test_decimals_read_per_transaction(self):
        # Mutation: hardcode 6 -> a mint with different decimals is silently
        # mis-scaled, which shows up as an absurd price rather than an error.
        r = S.normalize_payment(transfer_tx(500, decimals=9), "s", PAYEE_ACCT)
        self.assertEqual(r["decimals"], 9)
        self.assertAlmostEqual(r["amount"], 5e-07)


class TestCollectSignatures(unittest.TestCase):
    def test_short_page_with_history_behind_it_does_not_end_the_walk(self):
        # THE BUG THIS MODULE EXISTS FOR, and the case that actually proves it:
        # a SHORT page followed by MORE DATA. That is what a rate-limited page
        # looks like. Mutation: believe the short page -> the walk returns 4 of
        # 7 signatures and reports success. Measured cost of exactly this during
        # sizing: 66,209 signatures against a true 93,876.
        #
        # A short page that really is the end is covered separately below; a
        # test using only that case CANNOT catch this mutation, because
        # believing the final short page yields the same answer.
        rpc = FakeRPC({"getSignaturesForAddress":
                       [sigs(3), sigs(1, 3), sigs(3, 4), []]})
        got = S.collect_signatures(rpc, PAYEE_ACCT, limit=3)
        self.assertEqual([g["signature"] for g in got],
                         ["sig0", "sig1", "sig2", "sig3", "sig4", "sig5", "sig6"])

    def test_short_page_that_really_is_the_end_terminates(self):
        # The other half: confirming must not turn a finished walk into an
        # endless one. Mutation: ignore the empty confirmation and keep paging.
        rpc = FakeRPC({"getSignaturesForAddress": [sigs(3), sigs(2, 3), []]})
        got = S.collect_signatures(rpc, PAYEE_ACCT, limit=3)
        self.assertEqual(len(got), 5)
        self.assertEqual(len(rpc.calls), 3)

    def test_full_page_pages_on(self):
        rpc = FakeRPC({"getSignaturesForAddress": [sigs(2), sigs(2, 2), []]})
        self.assertEqual(len(S.collect_signatures(rpc, PAYEE_ACCT, limit=2)), 4)

    def test_empty_first_page_is_a_clean_zero(self):
        rpc = FakeRPC({"getSignaturesForAddress": [[]]})
        self.assertEqual(S.collect_signatures(rpc, PAYEE_ACCT), [])

    def test_transport_failure_raises_rather_than_truncating(self):
        # Mutation: return what we have -> a partial corpus presents as complete
        # and silently corrupts every statistic computed from it.
        rpc = FakeRPC({"getSignaturesForAddress": [sigs(2), RuntimeError("429")]})
        with self.assertRaises(S.IncompleteHistory):
            S.collect_signatures(rpc, PAYEE_ACCT, limit=2)

    def test_failure_while_confirming_also_raises(self):
        # The confirm call is not best-effort: if it fails we do not know
        # whether history remains, and guessing "no" is the silent-loss bug.
        rpc = FakeRPC({"getSignaturesForAddress": [sigs(1), RuntimeError("429")]})
        with self.assertRaises(S.IncompleteHistory):
            S.collect_signatures(rpc, PAYEE_ACCT, limit=3)

    def test_page_cap_raises_instead_of_returning_partial(self):
        rpc = FakeRPC({"getSignaturesForAddress": [sigs(2)] * 4})
        with self.assertRaises(S.IncompleteHistory):
            S.collect_signatures(rpc, PAYEE_ACCT, limit=2, max_pages=3)

    def test_repeated_page_is_deduped(self):
        # If a page's last entry carries no signature the cursor goes None and
        # the RPC re-serves the newest page. Mutation: drop the dedupe -> the
        # same signatures are counted twice and every volume figure inflates.
        first = sigs(2)
        first[-1] = {"blockTime": 1}          # no "signature" -> cursor is None
        rpc = FakeRPC({"getSignaturesForAddress": [first, sigs(2), []]})
        got = S.collect_signatures(rpc, PAYEE_ACCT, limit=2)
        seen = [g.get("signature") for g in got if g.get("signature")]
        self.assertEqual(sorted(seen), ["sig0", "sig1"])

    def test_before_cursor_advances(self):
        # Mutation: forget `before` -> the same page is served forever and the
        # walk either loops to the cap or returns one page repeated.
        rpc = FakeRPC({"getSignaturesForAddress": [sigs(2), sigs(2, 2), []]})
        S.collect_signatures(rpc, PAYEE_ACCT, limit=2)
        self.assertEqual(rpc.calls[1][1][1].get("before"), "sig1")


class TestTokenAccount(unittest.TestCase):
    def test_resolves_pubkey(self):
        rpc = FakeRPC({"getTokenAccountsByOwner": [{"value": [{"pubkey": PAYEE_ACCT}]}]})
        self.assertEqual(S.token_account(rpc, PAYEE_OWNER), PAYEE_ACCT)

    def test_no_account_is_none_not_an_error(self):
        # 7 of 44 live Solana x402 payees advertise a USDC price and have never
        # received USDC. That is a finding, not a failure.
        rpc = FakeRPC({"getTokenAccountsByOwner": [{"value": []}]})
        self.assertIsNone(S.token_account(rpc, PAYEE_OWNER))


class TestPayeePayments(unittest.TestCase):
    def _rpc(self, n_in=2, n_out=1):
        """n_in inbound payments followed by n_out reverted transactions."""
        reverted = {"meta": {"err": {"e": 1}},
                    "transaction": {"message": {"accountKeys": []}}}
        return FakeRPC({
            "getTokenAccountsByOwner": [{"value": [{"pubkey": PAYEE_ACCT}]}],
            "getSignaturesForAddress": [sigs(n_in + n_out), []],
            "getTransaction": [transfer_tx(10000)] * n_in + [reverted] * n_out})

    def test_counts_payments_and_skips(self):
        rpc = self._rpc(n_in=2, n_out=1)
        r = S.payee_payments(rpc, PAYEE_OWNER)
        self.assertEqual(r["signatures"], 3)
        self.assertEqual(len(r["payments"]), 2)
        self.assertEqual(r["skipped"], 1)

    def test_unpaid_payee_short_circuits(self):
        # Mutation: walk signatures anyway -> a pointless page walk on an
        # account that does not exist.
        rpc = FakeRPC({"getTokenAccountsByOwner": [{"value": []}]})
        r = S.payee_payments(rpc, PAYEE_OWNER)
        self.assertEqual(r, {"owner": PAYEE_OWNER, "account": None,
                             "signatures": 0, "payments": [], "skipped": 0,
                             "failed": []})


    def test_one_unfetchable_signature_does_not_discard_the_walk(self):
        # The largest observed payee is 46,682 signatures; re-running it costs
        # 65 minutes. Mutation: let the exception propagate -> one transient
        # failure throws away every row already collected.
        rpc = FakeRPC({
            "getTokenAccountsByOwner": [{"value": [{"pubkey": PAYEE_ACCT}]}],
            "getSignaturesForAddress": [sigs(3), []],
            "getTransaction": [transfer_tx(10000), RuntimeError("429"),
                               transfer_tx(20000)]})
        r = S.payee_payments(rpc, PAYEE_OWNER)
        self.assertEqual(len(r["payments"]), 2)
        self.assertEqual(r["failed"], ["sig1"])

    def test_failures_are_not_counted_as_skips(self):
        # A skip is a known non-payment; a failure is an unknown. Mutation:
        # fold `failed` into `skipped` -> the yield rate silently absorbs
        # transport losses and the corpus looks complete.
        rpc = FakeRPC({
            "getTokenAccountsByOwner": [{"value": [{"pubkey": PAYEE_ACCT}]}],
            "getSignaturesForAddress": [sigs(2), []],
            "getTransaction": [RuntimeError("boom"), transfer_tx(1)]})
        r = S.payee_payments(rpc, PAYEE_OWNER)
        self.assertEqual(r["skipped"], 0)
        self.assertEqual(len(r["failed"]), 1)


class TestToGraph(unittest.TestCase):
    def _results(self):
        return [{"owner": "payeeA", "payments": [
            {"amount": 0.01, "payer": "b1"}, {"amount": 0.01, "payer": "b2"},
            {"amount": 50.0, "payer": "b1"}]},
            {"owner": "payeeB", "payments": [{"amount": 0.02, "payer": "b2"}]}]

    def test_builds_the_shape_screen_expects(self):
        amounts, buyers, pays_to = S.to_graph(self._results())
        self.assertEqual(sorted(amounts["payeeA"]), [0.01, 0.01, 50.0])
        self.assertEqual(buyers["payeeA"], {"b1", "b2"})
        self.assertEqual(pays_to["b2"], {"payeeA", "payeeB"})

    def test_band_filter_is_opt_in(self):
        # volume_integrity cannot see how its input was filtered and will report
        # a share of whatever denominator it gets, so filtering must be the
        # caller's explicit choice.
        amounts, _, _ = S.to_graph(self._results(), max_amount=20.0)
        self.assertEqual(amounts["payeeA"], [0.01, 0.01])
        unfiltered, _, _ = S.to_graph(self._results())
        self.assertEqual(len(unfiltered["payeeA"]), 3)

    def test_screened_output_is_accepted_by_volume_integrity(self):
        # The whole point of the shape: Solana data must drop straight into the
        # existing screen with no adapter.
        import volume_integrity as V
        amounts, buyers, pays_to = S.to_graph(self._results())
        res = V.screen(amounts, buyers, pays_to)
        self.assertEqual({r["payee"] for r in res}, {"payeeA", "payeeB"})

    def test_no_funders_map_is_produced(self):
        # Mutation: return an empty funders dict alongside -> screen_payee scores
        # 2-of-2 as though three signals were checked. Absent evidence must never
        # count as evidence.
        self.assertEqual(len(S.to_graph(self._results())), 3)

    def test_paid_payee_with_no_payer_still_records_the_amount(self):
        r = [{"owner": "p", "payments": [{"amount": 0.5, "payer": None}]}]
        amounts, buyers, _ = S.to_graph(r)
        self.assertEqual(amounts["p"], [0.5])
        self.assertNotIn("p", buyers)


class TestRPC(unittest.TestCase):
    class _Resp:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            import json
            return json.dumps(self.payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_rpc_level_error_is_retried_not_returned(self):
        # "Too many requests for a specific RPC call" arrives as HTTP 200 with an
        # error body. Mutation: treat a 200 as success -> a rate limit becomes
        # missing history that nothing reports.
        calls = []

        def opener(req, timeout=None):
            calls.append(req.full_url)
            if len(calls) < 3:
                return self._Resp({"error": {"message": "Too many requests"}})
            return self._Resp({"result": "ok"})

        rpc = S.make_rpc(opener=opener, sleep=lambda s: None)
        self.assertEqual(rpc("getSlot"), "ok")
        self.assertEqual(len(calls), 3)

    def test_endpoints_rotate_across_attempts(self):
        # They rate-limit independently, so retrying the same host wastes the
        # retry budget on the host that just refused.
        seen = []

        def opener(req, timeout=None):
            seen.append(req.full_url)
            return self._Resp({"error": {"message": "429"}})

        rpc = S.make_rpc(opener=opener, sleep=lambda s: None, tries=3)
        with self.assertRaises(Exception):
            rpc("getSlot")
        self.assertEqual(len(set(seen)), 3)

    def test_exhausted_retries_raise_the_last_error(self):
        def opener(req, timeout=None):
            raise OSError("connection reset")

        rpc = S.make_rpc(opener=opener, sleep=lambda s: None, tries=2)
        with self.assertRaises(OSError):
            rpc("getSlot")


if __name__ == "__main__":
    unittest.main()
