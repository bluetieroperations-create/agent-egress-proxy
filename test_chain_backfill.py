"""
Tests for chain_backfill.py -- seed reputation from public Base USDC history.
Fake page transport (Blockscout-shaped items); each test states its mutation.
"""
import tempfile
import unittest

import chain_backfill as B

USDC = B.BASE_USDC
PAYEE = "0x" + "a" * 40


def _item(to, frm, value="90000", dec=6, tx=None, ts="2026-07-01T00:00:00Z",
          token=USDC):
    return {"token": {"address_hash": token},
            "total": {"value": value, "decimals": dec},
            "to": {"hash": to}, "from": {"hash": frm},
            "transaction_hash": tx or ("0x" + "1" * 64), "timestamp": ts}


def _pager(pages_by_addr):
    """pages_by_addr: addr(lower) -> list of (items, next_params). Fake pages by
    call-count (ignores the opaque next_params, like a real cursor would drive it)."""
    counts = {}

    def fetch(address, params):
        a = address.lower()
        i = counts.get(a, 0)
        counts[a] = i + 1
        seq = pages_by_addr.get(a, [])
        return seq[i] if i < len(seq) else ([], None)
    return fetch


class TestCollectPaged(unittest.TestCase):
    """Mutation notes: not following next_params -> only page 1 collected; not
    honoring max_pages -> a runaway pager isn't bounded; returning a bare list
    again -> truncation becomes invisible, the defect that shipped a corpus
    holding 0.8% of its top payee."""

    def test_walks_all_pages(self):
        fetch = _pager({PAYEE.lower(): [([1, 2], {"c": 1}), ([3], {"c": 2}), ([4], None)]})
        self.assertEqual(B.collect_paged(fetch, PAYEE, max_pages=10), ([1, 2, 3, 4], False))

    def test_stops_at_max_pages_AND_SAYS_SO(self):
        """THE FIX. Hitting the cap with more history available must report
        truncated=True. MUTATION: returning `False` unconditionally, or dropping
        the flag -- either restores a silent cap that a caller reads as a
        complete history."""
        fetch = _pager({PAYEE.lower(): [([1], {"c": 1}), ([2], {"c": 2}), ([3], {"c": 3})]})
        self.assertEqual(B.collect_paged(fetch, PAYEE, max_pages=2), ([1, 2], True))

    def test_stops_on_no_next(self):
        fetch = _pager({PAYEE.lower(): [([1], None), ([2], None)]})
        self.assertEqual(B.collect_paged(fetch, PAYEE, max_pages=10), ([1], False))

    def test_exhausting_history_EXACTLY_at_the_cap_is_not_truncation(self):
        """THE BOUNDARY, and the one a naive `truncated = pages >= max_pages`
        gets wrong. The last page inside the cap says 'no more', so the walk is
        COMPLETE -- flagging it would cry wolf on every payee whose history
        happens to divide evenly, and a warning that fires on healthy runs is one
        operators learn to ignore.
        MUTATION: computing truncation from the page count instead of from
        whether the pager still has a next cursor."""
        fetch = _pager({PAYEE.lower(): [([1], {"c": 1}), ([2], None)]})
        self.assertEqual(B.collect_paged(fetch, PAYEE, max_pages=2), ([1, 2], False))

    def test_strict_raises_instead_of_truncating(self):
        """MUTATION: honoring `strict` only in the docstring. A full-depth pull
        needs the run to FAIL rather than quietly write a window."""
        fetch = _pager({PAYEE.lower(): [([1], {"c": 1}), ([2], {"c": 2})]})
        with self.assertRaises(B.IncompleteHistory):
            B.collect_paged(fetch, PAYEE, max_pages=1, strict=True)

    def test_zero_pages_is_NOT_a_claim_of_completeness(self):
        """max_pages=0 fetches nothing, so we have not confirmed anything. The
        honest answer is truncated=True; returning False would assert an empty
        history for a payee never queried -- the same "claim completeness without
        evidence" defect this whole change exists to remove.
        MUTATION: `truncated = bool(params)`, which reads False here because no
        page was ever fetched."""
        fetch = _pager({PAYEE.lower(): [([1], {"c": 1})]})
        self.assertEqual(B.collect_paged(fetch, PAYEE, max_pages=0), ([], True))

    def test_strict_does_NOT_raise_on_a_complete_walk(self):
        """RESTRAINT CONTROL. MUTATION: raising whenever strict is set, which
        would make the flag unusable -- every run would fail."""
        fetch = _pager({PAYEE.lower(): [([1], None)]})
        self.assertEqual(B.collect_paged(fetch, PAYEE, max_pages=5, strict=True),
                         ([1], False))


class TestPayeeTransfers(unittest.TestCase):
    """Mutation notes: not filtering inbound -> an outbound row sneaks in; not
    normalizing USDC-by-contract -> a lookalike token pollutes."""

    def test_inbound_usdc_only(self):
        fetch = _pager({PAYEE.lower(): [([
            _item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64),
            _item("0x" + "c" * 40, PAYEE, tx="0x" + "2" * 64),          # OUTBOUND -> drop
            _item(PAYEE, "0x" + "d" * 40, tx="0x" + "3" * 64, token="0x" + "e" * 40),  # non-USDC -> drop
        ], None)]})
        rows, truncated = B.payee_transfers(fetch, PAYEE)
        self.assertEqual(len(rows), 1)
        self.assertTrue(B.addresses_equal(rows[0]["to"], PAYEE))
        self.assertEqual(str(rows[0]["amount"]), "0.09")
        self.assertFalse(truncated)

    def test_truncation_survives_the_dedup_and_filter_pass(self):
        """The flag has to travel with the rows through inbound-filtering and
        dedup, not be recomputed from them. MUTATION: returning a bare list from
        payee_transfers, or hardcoding False -- backfill would then never mark a
        capped payee."""
        fetch = _pager({PAYEE.lower(): [
            ([_item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64)], {"c": 1}),
            ([_item(PAYEE, "0x" + "c" * 40, tx="0x" + "2" * 64)], {"c": 2}),
        ]})
        rows, truncated = B.payee_transfers(fetch, PAYEE, max_pages=1)
        self.assertEqual(len(rows), 1)
        self.assertTrue(truncated)


class TestCliExitCode(unittest.TestCase):
    """A scheduled run must be actionable from its EXIT CODE, without anyone
    reading stdout. Both ways a corpus comes back incomplete -- the page cap and
    a fail-soft transport error -- used to exit 0."""

    def _run(self, pages, extra=()):
        import tempfile
        d = tempfile.mkdtemp()
        with open(d + "/payees.txt", "w") as f:
            f.write(PAYEE + "\n")
        real = B.BlockscoutPager
        class _Stub:
            def __init__(self, **kw):
                self.fetch = _pager(pages)
        B.BlockscoutPager = _Stub
        try:
            return B.main(["--payees-file", d + "/payees.txt",
                           "--store", d + "/s.db", "--max-pages", "1", *extra])
        finally:
            B.BlockscoutPager = real

    def test_complete_run_exits_zero(self):
        """RESTRAINT CONTROL: a healthy run must stay 0, or the exit code says
        nothing. MUTATION: returning 1 unconditionally."""
        self.assertEqual(self._run({PAYEE.lower(): [
            ([_item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64)], None)]}), 0)

    def test_truncated_run_exits_nonzero(self):
        """MUTATION: dropping the exit-code branch, which is how a nightly
        refresh silently ships a windowed corpus."""
        self.assertEqual(self._run({PAYEE.lower(): [
            ([_item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64)], {"c": 1})]}), 1)

    def test_a_run_that_could_not_FETCH_also_exits_nonzero(self):
        """Found by running the CLI, not by reading it: backfill is fail-soft per
        payee, so an all-429 run returned errors=N and exited 0 -- a corpus with
        no data, reported as success. Downstream that reads as a payee with no
        settlements, not as a failed crawl.
        MUTATION: checking only `truncated` and ignoring `errors`."""
        def boom(address, params):
            raise OSError("429 Too Many Requests")
        import tempfile
        d = tempfile.mkdtemp()
        with open(d + "/payees.txt", "w") as f:
            f.write(PAYEE + "\n")
        real = B.BlockscoutPager
        class _Stub:
            def __init__(self, **kw):
                self.fetch = boom
        B.BlockscoutPager = _Stub
        try:
            rc = B.main(["--payees-file", d + "/payees.txt", "--store", d + "/s.db"])
        finally:
            B.BlockscoutPager = real
        self.assertEqual(rc, 1)

    def test_strict_exits_3_distinctly(self):
        """A DIFFERENT code from the soft warning: 1 means 'shipped, but partial',
        3 means 'refused to ship'. A caller automating a full-depth pull needs to
        tell those apart. MUTATION: collapsing both onto 1."""
        self.assertEqual(self._run({PAYEE.lower(): [
            ([_item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64)], {"c": 1})]},
            extra=("--strict",)), 3)


class TestBackfill(unittest.TestCase):
    """
    Mutation notes:
      - not ingesting -> reputation stays empty (test_seeds_reputation FAILS).
      - not idempotent -> re-run double-counts (test_idempotent FAILS).
      - ingesting an invalid address -> test_skips_invalid FAILS.
    """
    def _store(self):
        from reputation_store import ReputationStore
        d = tempfile.mkdtemp()
        return ReputationStore(d + "/rep.db")

    def test_seeds_reputation(self):
        fetch = _pager({PAYEE.lower(): [([
            _item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64),
            _item(PAYEE, "0x" + "c" * 40, tx="0x" + "2" * 64),         # distinct payer
        ], None)]})
        store = self._store()
        summary = B.backfill(store, [PAYEE], fetch)
        self.assertEqual(summary["ingested"], 2)
        rec = store.lookup(PAYEE)
        self.assertGreaterEqual(rec.get("settlement_count", 0), 2)
        self.assertGreaterEqual(rec.get("distinct_payers", 0), 2)   # the Sybil signal

    def test_a_capped_payee_is_MARKED_and_COUNTED_not_silently_kept(self):
        """THE DEFECT, at the level that actually reports a corpus. A truncated
        run used to return {payees, fetched, ingested} identical in shape to a
        complete one -- which is how a seed holding 0.8% of its top payee read as
        healthy. The data is KEPT (a flagged window beats no window), but the
        summary says so.
        MUTATION: dropping `truncated` from the summary, or not setting the
        per-payee flag -- both restore a corpus that cannot report its own
        incompleteness."""
        fetch = _pager({PAYEE.lower(): [
            ([_item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64)], {"c": 1}),
            ([_item(PAYEE, "0x" + "c" * 40, tx="0x" + "2" * 64)], {"c": 2}),
        ]})
        summary = B.backfill(self._store(), [PAYEE], fetch, max_pages=1)
        self.assertEqual(summary["truncated"], 1)
        self.assertTrue(summary["per_payee"][PAYEE.lower()]["truncated"])
        self.assertEqual(summary["ingested"], 1)      # the window is still kept
        self.assertEqual(summary["errors"], 0)        # truncation is not an error

    def test_a_complete_run_reports_zero_truncated(self):
        """RESTRAINT CONTROL. MUTATION: counting every payee as truncated, which
        would make the warning meaningless and train operators to ignore it."""
        fetch = _pager({PAYEE.lower(): [([_item(PAYEE, "0x" + "b" * 40,
                                                tx="0x" + "1" * 64)], None)]})
        summary = B.backfill(self._store(), [PAYEE], fetch)
        self.assertEqual(summary["truncated"], 0)
        self.assertNotIn("truncated", summary["per_payee"][PAYEE.lower()])

    def test_strict_mode_propagates_instead_of_burying_it_as_an_error(self):
        """`backfill` is fail-soft: it catches Exception per payee and records
        {"error": ...}. IncompleteHistory must NOT land there -- a capped payee
        in a full-depth pull invalidates the RUN, and filing it beside a 429
        hides exactly the thing strict mode exists to surface.
        MUTATION: removing the `except IncompleteHistory: raise` re-raise, which
        the broad handler below it would then swallow."""
        fetch = _pager({PAYEE.lower(): [
            ([_item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64)], {"c": 1}),
            ([_item(PAYEE, "0x" + "c" * 40, tx="0x" + "2" * 64)], {"c": 2}),
        ]})
        with self.assertRaises(B.IncompleteHistory):
            B.backfill(self._store(), [PAYEE], fetch, max_pages=1, strict=True)

    def test_idempotent_rerun(self):
        pages = {PAYEE.lower(): [([_item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64)], None)]}
        store = self._store()
        self.assertEqual(B.backfill(store, [PAYEE], _pager(pages))["ingested"], 1)
        self.assertEqual(B.backfill(store, [PAYEE], _pager(pages))["ingested"], 0)

    def test_skips_invalid_address(self):
        store = self._store()
        summary = B.backfill(store, ["not-an-address", PAYEE],
                             _pager({PAYEE.lower(): [([], None)]}))
        self.assertEqual(summary["payees"], 1)
        self.assertIn("skipped", summary["per_payee"]["not-an-address"])

    def test_multi_payee(self):
        p2 = "0x" + "f" * 40
        fetch = _pager({
            PAYEE.lower(): [([_item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64)], None)],
            p2.lower(): [([_item(p2, "0x" + "c" * 40, tx="0x" + "2" * 64)], None)]})
        store = self._store()
        summary = B.backfill(store, [PAYEE, p2], fetch)
        self.assertEqual(summary["payees"], 2)
        self.assertEqual(summary["ingested"], 2)

    def test_fetched_deduped_on_nonadvancing_pager(self):
        # a pager that never advances (always returns truthy next_params) re-serves
        # the same tx every page. Mutation: no dedup -> fetched == max_pages, a
        # misleading transfer count (store stays correct via idempotency).
        one = _item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64)

        def stuck(address, params):
            return ([one], {"page": 1})           # ALWAYS a next page
        store = self._store()
        summary = B.backfill(store, [PAYEE], stuck, max_pages=5)
        self.assertEqual(summary["fetched"], 1)   # deduped, not 5
        self.assertEqual(summary["ingested"], 1)

    def test_two_same_amount_transfers_one_tx_diff_senders_kept(self):
        # a disperse/multicall tx paying the payee the same amount from two senders
        # is two REAL settlements; dedup must keep both (differ by `from`).
        TX = "0x" + "9" * 64
        fetch = _pager({PAYEE.lower(): [([
            _item(PAYEE, "0x" + "b" * 40, tx=TX),
            _item(PAYEE, "0x" + "c" * 40, tx=TX),     # same tx, same amount, other sender
        ], None)]})
        rows = B.payee_transfers(fetch, PAYEE)
        self.assertEqual(len(rows), 2)                # not collapsed by the dedup

    def test_backfill_failsoft_on_transport_error(self):
        # one payee's pager raising must NOT abort the whole scan.
        p2 = "0x" + "f" * 40

        def fetch(address, params):
            if address.lower() == PAYEE.lower():
                raise ConnectionError("429 slow down")
            return ([_item(p2, "0x" + "c" * 40, tx="0x" + "2" * 64)], None)
        store = self._store()
        summary = B.backfill(store, [PAYEE, p2], fetch)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["per_payee"][PAYEE.lower()]["error"], "ConnectionError")
        self.assertEqual(summary["ingested"], 1)      # p2 still processed

    def test_backfill_dedupes_repeated_payee(self):
        # same address passed twice (e.g. checksummed + lowercase) is fetched once.
        calls = {"n": 0}

        def fetch(address, params):
            calls["n"] += 1
            return ([_item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64)], None)
        store = self._store()
        checksummed = "0x" + "A" * 40                 # same address, upper hex, 0x kept
        summary = B.backfill(store, [PAYEE, checksummed], fetch)
        self.assertEqual(calls["n"], 1)               # not fetched twice
        self.assertEqual(summary["payees"], 1)


class TestBlockscoutPager(unittest.TestCase):
    """The live pager parses items/next_page_params and fetches through http_util
    (retry/backoff + size cap live there). Transport is stubbed -- no network."""

    def test_fetch_parses_and_delegates_to_http_util(self):
        import http_util
        seen = {}

        def fake_get_json(url, **kw):
            seen["url"] = url
            return {"items": [{"x": 1}], "next_page_params": {"block": 42}}
        orig = http_util.get_json
        http_util.get_json = fake_get_json
        try:
            items, nxt = B.BlockscoutPager().fetch(PAYEE, {"block": 41})
        finally:
            http_util.get_json = orig
        self.assertEqual(items, [{"x": 1}])
        self.assertEqual(nxt, {"block": 42})
        self.assertIn(PAYEE, seen["url"])
        self.assertIn("filter=to", seen["url"])

    def test_fetch_rejects_invalid_address_without_calling_out(self):
        called = {"n": 0}
        import http_util
        orig = http_util.get_json
        http_util.get_json = lambda *a, **k: called.__setitem__("n", called["n"] + 1)
        try:
            self.assertEqual(B.BlockscoutPager().fetch("not-an-addr", None), ([], None))
        finally:
            http_util.get_json = orig
        self.assertEqual(called["n"], 0)     # never interpolate an unvalidated addr


class TestReadPayees(unittest.TestCase):
    """
    Mutation notes:
      - keep the whole line (don't split on '#') -> test_inline_comment FAILS: an
        annotated address "0xabc  # tier" would be read verbatim and never match a
        real payee, silently emptying an annotated manifest.
    """
    def _write(self, text):
        import os
        p = os.path.join(tempfile.mkdtemp(), "payees.txt")
        with open(p, "w") as f:
            f.write(text)
        return p

    def _read(self, path):
        import argparse
        return B._read_payees(argparse.Namespace(payee=None, payees_file=path))

    def test_inline_comment_stripped(self):
        addr = "0x" + "a" * 40
        got = self._read(self._write("%s  # distinct=42  tensorfeed.ai\n" % addr))
        self.assertEqual(got, [addr])       # annotation stripped, bare address kept

    def test_full_line_and_blank_skipped(self):
        addr = "0x" + "b" * 40
        got = self._read(self._write("# --- SECTION ---\n\n%s\n" % addr))
        self.assertEqual(got, [addr])

    def test_plain_addresses(self):
        a, b = "0x" + "c" * 40, "0x" + "d" * 40
        got = self._read(self._write("%s\n%s\n" % (a, b)))
        self.assertEqual(got, [a, b])


if __name__ == "__main__":
    unittest.main()
