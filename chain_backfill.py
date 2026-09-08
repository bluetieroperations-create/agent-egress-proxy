#!/usr/bin/env python3
"""
chain_backfill.py -- seed Blackwall's reputation from PUBLIC Base USDC history.

Feed the counterparty-reputation moat WITHOUT customers. Blackwall scores the
PAYEE and counts DISTINCT PAYERS -- and a payee's inbound USDC is public on Base.
So for each x402 payee address you care about (an endpoint's `payTo`), walk its
inbound USDC and ingest it into a `ReputationStore`. Boot the engine with a real
corpus before customer #1.

Design: **targeted, not the firehose.** We pull inbound USDC to KNOWN payee
addresses (low-noise, precise), not every USDC transfer on Base. Reuses
`settlement_watch.extract_usdc_transfers` (identifies USDC by CONTRACT, so a
lookalike token can't spoof history) and `reputation_store.ingest_transfers`
(idempotent on `tx_hash`, so re-runs never double-count).

Scope / honesty: inbound USDC to a payee's dedicated receiving address is treated as
its settlement history; we do not distinguish an x402 `transferWithAuthorization`
from other inbound to that same address -- acceptable because it's the endpoint's
payTo. This gives BREADTH (who's established, distinct-payer counts, price norms)
cheaply; dispute/outcome DEPTH still comes from Traceipt receipts + the verdict
flywheel (see `docs/LIVE_PROOF.md`, `traceipt_pull.py`). Stdlib; the page transport
is injectable for tests.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse

import http_util
from addresses import addresses_equal, is_evm_address
from settlement_watch import (BASE_USDC, DEFAULT_BASE_URL, DEFAULT_UA, HTTP_TIMEOUT,
                              extract_usdc_transfers)


class IncompleteHistory(Exception):
    """The walk stopped at its page cap, so the result is a WINDOW, not a history.

    Deliberately an exception (in `strict` mode) rather than a partial return: a
    truncated corpus that presents as complete silently corrupts every statistic
    computed from it. Same reasoning, and the same name, as
    `solana_backfill.IncompleteHistory` -- that module raises rather than
    truncating, which is why the Solana corpus is exhaustive and this one was not.
    """


def collect_paged(fetch, address, max_pages, *, strict=False):
    """Walk up to `max_pages` of `fetch(address, page_params) -> (items,
    next_params)`. Returns **(items, truncated)**.

    TRUNCATION IS REPORTED, NOT SWALLOWED -- and the tuple is the point. This
    used to return a bare list and stop at the cap with no signal, so a caller
    could not tell "history exhausted" from "there is much more and I quit".
    MEASURED CONSEQUENCE: the committed Base seed captured 0.8% of its
    highest-value payee (239 of 29,231 transfers) and looked complete; 70 of 281
    payees (25%) sit on an exact 50-multiple >= 100, which is the page cap, not
    the ecosystem. Returning a tuple forces every caller to acknowledge the
    question, which is what a bare list let everyone skip.

    `strict=True` raises `IncompleteHistory` instead. Off by default because
    `backfill` is fail-soft per payee: raising there would discard the window we
    DID fetch and record the payee as an error, which is strictly worse than a
    flagged partial. Turn it on for a deliberate full-depth pull, where hitting
    the cap means the run is wrong rather than merely bounded.
    """
    # `exhausted` is set ONLY when the pager itself says there is no more. Every
    # other way out -- the cap, or `max_pages=0` -- leaves it False, so the
    # default answer is "I did not confirm this is complete". Deriving it the
    # other way round (from the page count, or by returning early on the last
    # page) makes the completeness claim unreachable in exactly the cases worth
    # testing; mutation testing caught three surviving mutants on that shape.
    items, params, pages = [], None, 0
    exhausted = False
    while pages < max_pages:
        page_items, params = fetch(address, params)
        items.extend(page_items or [])
        pages += 1
        if not params:
            exhausted = True              # the pager said there is no more
            break
    truncated = not exhausted
    if truncated and strict:
        raise IncompleteHistory(
            "%s: stopped at the %d-page cap with more history available"
            % (address, max_pages))
    return items, truncated


def payee_transfers(fetch, address, *, usdc=BASE_USDC, max_pages=5, strict=False):
    """Inbound USDC transfers to `address`, paged + normalized + inbound-only, and
    DEDUPED. Returns **(transfers, truncated)** -- see `collect_paged` for why the
    truncation flag is carried rather than dropped.

    A pager that re-serves a page (non-advancing, or Blockscout offset
    overlap as new txs land) would otherwise double-count `fetched`; the store's
    idempotent key protects `ingested` but not the reported transfer count."""
    raw, truncated = collect_paged(fetch, address, max_pages, strict=strict)
    inbound = [t for t in extract_usdc_transfers(raw, usdc)
               if t.get("to") and addresses_equal(t["to"], address)]
    seen, out = set(), []
    for t in inbound:
        # keep two same-amount transfers in one tx from DIFFERENT senders (real,
        # distinct settlements); collapse only byte-identical duplicates.
        key = (t.get("tx_hash"), t.get("from"), t.get("to"), str(t.get("amount")))
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out, truncated


def backfill(store, payees, fetch, *, usdc=BASE_USDC, max_pages=5, strict=False):
    """Ingest inbound USDC for each valid payee. Returns a stage summary
    {payees, fetched, ingested, errors, truncated, per_payee}. Invalid addresses
    are skipped (not silently ingested); a duplicate payee is processed once; a
    transport error on one payee is recorded and the run CONTINUES (fail-soft),
    so one 429/timeout can't abort the whole scan.

    `truncated` is the COUNT of payees that hit the page cap, and each such
    per-payee entry carries `truncated: True`. Without it a run reports
    "281 payees, 46,031 settlements" and reads as a complete corpus when it is a
    250-row window per payee -- the defect that produced the shipped seed.
    `strict=True` propagates `IncompleteHistory` instead, for a full-depth pull
    where a capped payee means the run is wrong rather than merely bounded."""
    per, total_fetched, total_ingested, ok, errors, seen = {}, 0, 0, 0, 0, set()
    truncated_payees = 0
    for p in payees or []:
        if not is_evm_address(p):
            per[str(p)] = {"skipped": "not a valid EVM address"}
            continue
        low = p.lower()
        if low in seen:                       # de-dupe (checksummed + lowercase, or repeats)
            continue
        seen.add(low)
        try:
            xfers, was_truncated = payee_transfers(fetch, p, usdc=usdc,
                                                   max_pages=max_pages, strict=strict)
        except IncompleteHistory:
            # strict mode: a capped payee invalidates the RUN, so do not bury it
            # in per_payee alongside ordinary transport errors.
            raise
        except Exception as e:                # fail-soft: one bad payee != dead scan
            per[low] = {"error": type(e).__name__}
            errors += 1
            continue
        ingested = store.ingest_transfers(xfers) if xfers else 0
        per[low] = {"fetched": len(xfers), "ingested": ingested}
        if was_truncated:
            per[low]["truncated"] = True
            truncated_payees += 1
        total_fetched += len(xfers)
        total_ingested += ingested
        ok += 1
    return {"payees": ok, "fetched": total_fetched, "ingested": total_ingested,
            "errors": errors, "truncated": truncated_payees, "per_payee": per}


class BlockscoutPager:
    """Live Base transport: pages a payee's inbound USDC via Blockscout v2
    (`GET /api/v2/addresses/{addr}/token-transfers?type=ERC-20&filter=to`). Fetches
    through `http_util.get_json`, so a transient 429/5xx/timeout is retried with
    backoff (a rate-limited public node no longer silently drops a payee's history)
    and the response is size-capped."""

    def __init__(self, base_url=DEFAULT_BASE_URL, usdc=BASE_USDC,
                 user_agent=DEFAULT_UA, timeout=HTTP_TIMEOUT,
                 retries=http_util.DEFAULT_RETRIES, backoff=http_util.DEFAULT_BACKOFF):
        self.base_url = base_url.rstrip("/")
        self.usdc = usdc
        self.user_agent = user_agent
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    def fetch(self, address, page_params):
        if not is_evm_address(address):     # never interpolate an unvalidated addr
            return [], None
        q = "type=ERC-20&filter=to"
        if page_params:
            q += "&" + urllib.parse.urlencode(page_params)
        url = "%s/api/v2/addresses/%s/token-transfers?%s" % (self.base_url, address, q)
        data = http_util.get_json(url, timeout=self.timeout, user_agent=self.user_agent,
                                  retries=self.retries, backoff=self.backoff)
        return (data.get("items") or [], data.get("next_page_params"))


def _read_payees(args):
    payees = list(args.payee or [])
    if args.payees_file:
        with open(args.payees_file, "r", encoding="utf-8") as f:
            for ln in f:
                # Strip INLINE comments too (addr followed by `# annotation`), not
                # just whole-line ones -- a self-documenting manifest annotates each
                # address with its tier/domain, and the bare address is token 0.
                addr = ln.split("#", 1)[0].strip()
                if addr:
                    payees.append(addr)
    return payees


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Seed a Blackwall reputation store from public Base USDC history.")
    p.add_argument("--store", required=True, help="ReputationStore SQLite path")
    p.add_argument("--payee", action="append",
                   help="an x402 payee (payTo) address to backfill (repeatable)")
    p.add_argument("--payees-file", help="file with one payee address per line (# comments ok)")
    p.add_argument("--max-pages", type=int, default=5,
                   help="pages per payee (~50 transfers/page; default 5)")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Blockscout base URL")
    p.add_argument("--usdc", default=BASE_USDC, help="USDC contract address")
    p.add_argument("--strict", action="store_true",
                   help="FAIL the run if any payee hits the page cap, instead of "
                        "recording a flagged partial. Use for a deliberate "
                        "full-depth pull, where a capped payee means the corpus "
                        "is a window and every statistic from it is wrong.")
    args = p.parse_args(argv)

    payees = _read_payees(args)
    if not payees:
        sys.stderr.write("chain_backfill: no payees (pass --payee or --payees-file)\n")
        return 2
    from reputation_store import ReputationStore
    store = ReputationStore(args.store)
    pager = BlockscoutPager(base_url=args.base_url, usdc=args.usdc)
    try:
        summary = backfill(store, payees, pager.fetch, usdc=args.usdc,
                           max_pages=args.max_pages, strict=args.strict)
    except IncompleteHistory as e:
        sys.stderr.write("chain_backfill: INCOMPLETE -- %s\n"
                         "Raise --max-pages or drop --strict; the corpus this "
                         "would have written is a window, not a history.\n" % e)
        return 3
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    sys.stdout.write("Seeded %d payee(s): %d transfers, %d new settlements.\n"
                     % (summary["payees"], summary["fetched"], summary["ingested"]))
    # LOUD, on stderr, and it sets the exit code: the shipped seed captured 0.8%
    # of its top payee while reporting a healthy-looking total, because nothing
    # ever said this. A run that silently truncates must not exit 0.
    incomplete = False
    if summary["truncated"]:
        sys.stderr.write(
            "chain_backfill: WARNING %d of %d payee(s) hit the %d-page cap -- "
            "their history is TRUNCATED, not complete. Statistics derived from "
            "this corpus (age_days, first_seen, burst detection, medians) "
            "describe the crawl window, not the ecosystem. Re-run with a higher "
            "--max-pages for full depth.\n"
            % (summary["truncated"], summary["payees"], args.max_pages))
        incomplete = True
    # SAME DEFECT CLASS as the page cap, found by running the CLI: `backfill` is
    # fail-soft per payee, so a run where every payee 429s returns errors=N and
    # otherwise looks like a normal result. It must not exit 0 either -- a
    # scheduled run that fetched nothing should be actionable without reading
    # its stdout.
    if summary["errors"]:
        sys.stderr.write(
            "chain_backfill: WARNING %d payee(s) FAILED to fetch (transport "
            "errors); their history is missing from this corpus entirely, which "
            "reads downstream as a payee with no settlements.\n"
            % summary["errors"])
        incomplete = True
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
