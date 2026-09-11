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


class TestMidWalkFailure(unittest.TestCase):
    """A transport failure PART WAY through a walk.

    Reported by the corpus-depth session against 4add6e6 and reproduced before
    fixing: the exception unwound `collect_paged` and threw away every page
    already fetched, so 3 good pages became 0 rows ingested and the payee was
    filed as `{"error": ...}` -- which downstream is indistinguishable from
    "this payee has no history". Runs accumulate and ingest is idempotent, so
    this never showed up in the shipped corpus; a ONE-SHOT full-depth pull has
    no next run to fill the gap.
    """

    def _dying(self, die_on):
        n = {"c": 0}
        def fetch(address, params):
            n["c"] += 1
            if n["c"] >= die_on:
                raise OSError("HTTP 500 from indexer")
            return ([n["c"]], {"page": n["c"] + 1})
        return fetch

    def test_pages_already_fetched_are_KEPT_and_marked_truncated(self):
        """MUTATION: removing the try/except around the fetch, which restores the
        unwind that discarded 3 good pages."""
        items, truncated = B.collect_paged(self._dying(4), PAYEE, max_pages=20,
                                           sleep=lambda _s: None)
        self.assertEqual(items, [1, 2, 3])
        self.assertTrue(truncated)

    def test_a_failure_on_page_ONE_still_raises(self):
        """There is no partial to keep and nothing to label, so this is a genuine
        fetch FAILURE, not a window -- `backfill` must record it in `errors`
        rather than report a payee with zero rows as merely capped.
        MUTATION: returning ([], True) here, which would turn every unreachable
        payee into a 'truncated' one with no rows and hide it from `errors`."""
        with self.assertRaises(OSError):
            B.collect_paged(self._dying(1), PAYEE, max_pages=20,
                            sleep=lambda _s: None)

    def test_strict_reports_the_TRANSPORT_cause_not_the_page_cap(self):
        """An operator reading `--strict` output needs to know whether to raise
        --max-pages or retry the run. MUTATION: a single message for both
        causes, which sends them to the wrong fix."""
        with self.assertRaises(B.IncompleteHistory) as cm:
            B.collect_paged(self._dying(3), PAYEE, max_pages=20, strict=True,
                            sleep=lambda _s: None)
        msg = str(cm.exception)
        self.assertIn("transport failed mid-walk", msg)
        self.assertNotIn("page cap", msg)

    def test_backfill_keeps_the_partial_and_does_NOT_call_it_an_error(self):
        """The property that matters end to end. MUTATION: any path where a
        mid-walk failure lands in `errors` with zero rows again."""
        import tempfile
        from reputation_store import ReputationStore
        n = {"c": 0}
        def fetch(address, params):
            n["c"] += 1
            if n["c"] >= 3:
                raise OSError("HTTP 500")
            return ([_item(PAYEE, "0x" + ("b%039d" % n["c"])[:40],
                           tx="0x" + str(n["c"]) * 64)], {"page": n["c"] + 1})
        store = ReputationStore(tempfile.mkdtemp() + "/r.db")
        s = B.backfill(store, [PAYEE], fetch, max_pages=20, sleep=lambda _x: None)
        self.assertEqual(s["errors"], 0)
        self.assertEqual(s["truncated"], 1)
        self.assertEqual(s["ingested"], 2)          # both good pages kept
        self.assertTrue(s["per_payee"][PAYEE.lower()]["truncated"])


class TestPageRetry(unittest.TestCase):
    """~2% of page fetches fail even when the indexer is healthy (n=45,
    measured), so a 5-page walk completes only 0.98^5 = 90.4% of the time --
    ~27 of 281 payees fetching nothing per run. Two extra attempts take one
    page's odds of failing from 2% to 0.0008%."""

    def test_a_page_that_fails_once_then_succeeds_completes_the_walk(self):
        """MUTATION: retries=0, i.e. no retry loop at all -- the walk truncates
        on the first blip and ~1 payee in 10 comes back short."""
        n = {"c": 0}
        def flaky(address, params):
            n["c"] += 1
            if n["c"] == 2:
                raise OSError("transient 500")
            return ([n["c"]], {"page": 2} if n["c"] == 1 else None)
        items, truncated = B.collect_paged(flaky, PAYEE, max_pages=5,
                                           sleep=lambda _s: None)
        self.assertEqual(items, [1, 3])             # page 2 retried, then ended
        self.assertFalse(truncated)                 # a retried walk is COMPLETE

    def test_retry_does_NOT_advance_the_cursor(self):
        """The reason the retry lives in its own function. Retrying inside the
        loop body with `params` already reassigned would skip the page it failed
        to read and then call the result complete -- silent history loss dressed
        as success.
        MUTATION: inlining the retry after `params` is rebound."""
        seen = []
        n = {"c": 0}
        def fetch(address, params):
            seen.append(params)
            n["c"] += 1
            if n["c"] == 1:
                raise OSError("transient")
            return ([n["c"]], None)
        B.collect_paged(fetch, PAYEE, max_pages=5, sleep=lambda _s: None)
        self.assertEqual(seen, [None, None])        # same cursor, twice

    def test_retries_are_BOUNDED_and_then_it_gives_up(self):
        """MUTATION: an unbounded retry loop, which turns one dead payee into a
        hung backfill."""
        n = {"c": 0}
        def dead(address, params):
            n["c"] += 1
            raise OSError("permanently down")
        with self.assertRaises(OSError):
            B.collect_paged(dead, PAYEE, max_pages=5, retries=2,
                            sleep=lambda _s: None)
        self.assertEqual(n["c"], 3)                 # 1 attempt + 2 retries

    def test_backoff_is_EXPONENTIAL_and_not_slept_after_the_last_attempt(self):
        """Found by mutation testing: three mutants survived because no test
        asserted the DELAY. Removing the backoff entirely left retries working
        and every test green -- but retrying instantly against a rate-limited
        indexer is what produced the 429s in the first place, so the delay is
        the part that makes the retry useful.
        Also pins that we do NOT sleep after the final attempt: that would add
        PAGE_RETRY_BACKOFF * 2^retries of dead time to every payee we give up
        on, for no benefit.
        MUTATIONS: dropping the sleep; `if True` (sleeping past the last
        attempt); a constant instead of an exponential backoff."""
        slept = []
        def dead(address, params):
            raise OSError("permanently down")
        with self.assertRaises(OSError):
            B.collect_paged(dead, PAYEE, max_pages=5, retries=3,
                            sleep=slept.append)
        # 4 attempts -> 3 sleeps, doubling, and none after the last attempt.
        self.assertEqual(slept, [B.PAGE_RETRY_BACKOFF,
                                 B.PAGE_RETRY_BACKOFF * 2,
                                 B.PAGE_RETRY_BACKOFF * 4])

    def test_a_healthy_page_is_never_retried_or_slept_on(self):
        """RESTRAINT CONTROL. MUTATION: retrying unconditionally, or sleeping
        before the first attempt -- 281 payees x a 1s backoff would add minutes
        to every clean run."""
        slept = []
        n = {"c": 0}
        def ok(address, params):
            n["c"] += 1
            return ([n["c"]], None)
        B.collect_paged(ok, PAYEE, max_pages=5, sleep=slept.append)
        self.assertEqual(n["c"], 1)
        self.assertEqual(slept, [])


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
                           "--store", d + "/s.db", "--max-pages", "1",
                           "--retries", "0", *extra])
        finally:
            B.BlockscoutPager = real

    def test_complete_run_exits_zero(self):
        """RESTRAINT CONTROL: a healthy run must stay 0, or the exit code says
        nothing. MUTATION: returning 1 unconditionally."""
        self.assertEqual(self._run({PAYEE.lower(): [
            ([_item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64)], None)]}), 0)

    def test_truncated_run_exits_ZERO_by_default(self):
        """REGRESSION GUARD, and the bug was mine. Returning 1 on truncation read
        as rigour and broke `scripts/refresh_seed.sh`, which runs `set -eu` and
        invokes this at --max-pages 4 -- so the bounded walk that script ASKS FOR
        killed the scheduled refresh at that line, every run. That refresh is what
        keeps the corpus off the 90-day `stale` cliff, so the safety change
        disabled the safety mechanism. Reproduced before fixing.
        MUTATION: `return 1 if incomplete else 0` -- the original defect."""
        self.assertEqual(self._run({PAYEE.lower(): [
            ([_item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64)], {"c": 1})]}), 0)

    def test_truncated_run_exits_nonzero_WHEN_ASKED(self):
        """The actionable exit is opt-in, so a caller that wants a hard failure
        can have one without imposing it on every existing invocation.
        MUTATION: ignoring --fail-on-incomplete, which makes the flag a lie."""
        self.assertEqual(self._run({PAYEE.lower(): [
            ([_item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64)], {"c": 1})]},
            extra=("--fail-on-incomplete",)), 1)

    def test_a_run_that_could_not_FETCH_is_reported_and_opt_in_fatal(self):
        """Found by running the CLI, not by reading it: backfill is fail-soft per
        payee, so an all-429 run returns errors=N and otherwise looks normal --
        downstream that reads as a payee with no settlements, not a failed crawl.
        It is WARNED about always and fatal only on request, because
        refresh_seed.sh deliberately tolerates a failed fetch ("costs FRESHNESS,
        never COVERAGE" -- it merges onto the committed store first).
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
            soft = B.main(["--payees-file", d + "/payees.txt", "--store", d + "/s.db",
                           "--retries", "0"])
            hard = B.main(["--payees-file", d + "/payees.txt", "--store", d + "/s.db",
                           "--retries", "0", "--fail-on-incomplete"])
        finally:
            B.BlockscoutPager = real
        self.assertEqual(soft, 0)
        self.assertEqual(hard, 1)

    def test_strict_also_refuses_a_run_that_could_not_FETCH(self):
        """AUDIT FINDING. --strict raised on the page cap but not on a transport
        error, because `backfill` is fail-soft there -- so a full-depth pull where
        payees 429'd reported SUCCESS while contributing nothing. An unfetched
        payee is more incomplete than a truncated one: downstream it reads as a
        counterparty with no settlements, not one we failed to reach.
        MUTATION: dropping the `args.strict and summary["errors"]` check, which
        restores a --strict that enforces only the cheaper half of its promise."""
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
            rc = B.main(["--payees-file", d + "/payees.txt", "--store", d + "/s.db",
                         "--retries", "0", "--strict"])
        finally:
            B.BlockscoutPager = real
        self.assertEqual(rc, 3)

    def test_strict_does_NOT_fire_on_a_clean_complete_run(self):
        """RESTRAINT CONTROL. MUTATION: returning 3 whenever --strict is passed,
        which would make a full-depth pull impossible to ever complete."""
        self.assertEqual(self._run({PAYEE.lower(): [
            ([_item(PAYEE, "0x" + "b" * 40, tx="0x" + "1" * 64)], None)]},
            extra=("--strict",)), 0)

    def test_strict_exits_3_distinctly(self):
        """THREE distinct codes: 0 shipped (possibly bounded, which is normal),
        1 shipped-but-incomplete AND the caller asked to be told, 3 refused to
        ship. A caller automating a full-depth pull needs to tell those apart.
        MUTATION: collapsing 3 onto 1, which would make --strict indistinguishable
        from an opt-in warning."""
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
        # retries=0: this pins FAIL-SOFT, not the retry loop -- left at the
        # default it paid 3s of real backoff per run, which is how a retry with
        # no injected clock quietly turns a 0.1s suite into a 12s one.
        p2 = "0x" + "f" * 40

        def fetch(address, params):
            if address.lower() == PAYEE.lower():
                raise ConnectionError("429 slow down")
            return ([_item(p2, "0x" + "c" * 40, tx="0x" + "2" * 64)], None)
        store = self._store()
        summary = B.backfill(store, [PAYEE, p2], fetch, retries=0)
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
