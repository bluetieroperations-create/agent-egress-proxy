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


def collect_paged(fetch, address, max_pages):
    """Walk up to `max_pages` of `fetch(address, page_params) -> (items,
    next_params)` and return all raw items. Stops when `next_params` is falsy or
    the page cap is hit (the cap also bounds a misbehaving pager)."""
    items, params, pages = [], None, 0
    while pages < max_pages:
        page_items, params = fetch(address, params)
        items.extend(page_items or [])
        pages += 1
        if not params:
            break
    return items


def payee_transfers(fetch, address, *, usdc=BASE_USDC, max_pages=5):
    """Inbound USDC transfers to `address`, paged + normalized + inbound-only, and
    DEDUPED. A pager that re-serves a page (non-advancing, or Blockscout offset
    overlap as new txs land) would otherwise double-count `fetched`; the store's
    idempotent key protects `ingested` but not the reported transfer count."""
    raw = collect_paged(fetch, address, max_pages)
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
    return out


def backfill(store, payees, fetch, *, usdc=BASE_USDC, max_pages=5):
    """Ingest inbound USDC for each valid payee. Returns a stage summary
    {payees, fetched, ingested, errors, per_payee}. Invalid addresses are skipped
    (not silently ingested); a duplicate payee is processed once; a transport error
    on one payee is recorded and the run CONTINUES (fail-soft), so one 429/timeout
    can't abort the whole scan."""
    per, total_fetched, total_ingested, ok, errors, seen = {}, 0, 0, 0, 0, set()
    for p in payees or []:
        if not is_evm_address(p):
            per[str(p)] = {"skipped": "not a valid EVM address"}
            continue
        low = p.lower()
        if low in seen:                       # de-dupe (checksummed + lowercase, or repeats)
            continue
        seen.add(low)
        try:
            xfers = payee_transfers(fetch, p, usdc=usdc, max_pages=max_pages)
        except Exception as e:                # fail-soft: one bad payee != dead scan
            per[low] = {"error": type(e).__name__}
            errors += 1
            continue
        ingested = store.ingest_transfers(xfers) if xfers else 0
        per[low] = {"fetched": len(xfers), "ingested": ingested}
        total_fetched += len(xfers)
        total_ingested += ingested
        ok += 1
    return {"payees": ok, "fetched": total_fetched, "ingested": total_ingested,
            "errors": errors, "per_payee": per}


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
            payees += [ln.strip() for ln in f
                       if ln.strip() and not ln.lstrip().startswith("#")]
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
    args = p.parse_args(argv)

    payees = _read_payees(args)
    if not payees:
        sys.stderr.write("chain_backfill: no payees (pass --payee or --payees-file)\n")
        return 2
    from reputation_store import ReputationStore
    store = ReputationStore(args.store)
    pager = BlockscoutPager(base_url=args.base_url, usdc=args.usdc)
    summary = backfill(store, payees, pager.fetch, usdc=args.usdc,
                       max_pages=args.max_pages)
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    sys.stdout.write("Seeded %d payee(s): %d transfers, %d new settlements.\n"
                     % (summary["payees"], summary["fetched"], summary["ingested"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
