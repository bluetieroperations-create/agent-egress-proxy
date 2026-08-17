#!/usr/bin/env python3
"""
rwa_backfill.py -- seed the RWA outcome corpus from PUBLIC on-chain history, with ZERO
customers. The tokenized-RWA analogue of chain_backfill.py.

`issuer_trust` needs LABELED per-issuer outcomes to calibrate, but the organic flywheel
(real agent buys -> T+N labels) bootstraps slowly. On-chain history is ALREADY labeled:
a tokenized-stock transfer that landed IS a successful settlement. So we paginate a known
token's transfer history (keyless Blockscout), reconstruct each acquisition (a transfer
TO a real wallet -- mint or secondary buy), and write a `buy` + a `settled=True` `outcome`
into the ledger, keyed by tx+recipient so re-runs are idempotent. Result: an issuer goes
from "insufficient" (no data) to a real, hard-to-fake footprint -- settlement volume +
distinct buyers + active period -- enough for `issuer_trust` to grade.

HONEST SCOPE: a token transfer shows the security ARRIVED (settlement) and WHO received
it (breadth), but NOT the USDC paid, so this backfill does NOT produce mark-to-market /
underwater labels (no price-paid on a bare transfer). Those come from the organic
flywheel (real buys carry the paid price) or a future paired-swap parser. So a
backfill-only issuer grades on settlement + volume, not value-outcome -- which is the
correct, conservative calibration (proven to settle; value-outcome not yet observed).

Targeted, not a firehose (per-token cap, like chain_backfill). Keyless, fail-soft,
idempotent. Stdlib (reuses http_util + rwa_ledger).
"""
from __future__ import annotations

from addresses import addresses_equal, is_evm_address
from holder_concentration import BLOCKSCOUT_BASES
from rwa_ledger import build_outcome_event

DEFAULT_MAX_PAGES = 5
DEFAULT_PER_TOKEN_CAP = 500          # bound a popular token's history (targeted)


def _addr(x):
    """Blockscout address field is {hash, ...} or a bare string -> the hash."""
    if isinstance(x, dict):
        return x.get("hash")
    return x if isinstance(x, str) else None


def _to_unix(ts):
    """ISO-8601 (or numeric) timestamp -> unix seconds, or None. NEVER raises."""
    if isinstance(ts, (int, float)):
        try:
            return int(ts)                    # OverflowError on inf/nan
        except (OverflowError, ValueError):
            return None
    if not isinstance(ts, str):
        return None
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def collect_paged(fetch, token, max_pages):
    """Walk up to `max_pages` of `fetch(token, page_params) -> (items, next_params)`."""
    items, params, pages = [], None, 0
    while pages < max_pages:
        page_items, params = fetch(token, params)
        items.extend(page_items or [])
        pages += 1
        if not params:
            break
    return items


def token_acquisitions(fetch, token, *, max_pages=DEFAULT_MAX_PAGES, cap=DEFAULT_PER_TOKEN_CAP):
    """Reconstruct ACQUISITIONS of `token` from its transfer history: each transfer TO a
    real wallet (mint from 0x0 or a secondary buy) is one acquisition. Excludes burns/
    redemptions (to 0x0) and transfers to the token contract itself. Deduped by
    (tx, recipient); capped. Returns [{to, tx_hash, timestamp, value}]."""
    raw = collect_paged(fetch, token, max_pages)
    seen, out = set(), []
    for t in raw:
        if not isinstance(t, dict):
            continue
        to = _addr(t.get("to"))
        tx = t.get("tx_hash") or t.get("transaction_hash")
        if not is_evm_address(to) or int(to, 16) == 0 or addresses_equal(to, token):
            continue
        key = (tx, to.lower())
        if key in seen:
            continue
        seen.add(key)
        total = t.get("total") if isinstance(t.get("total"), dict) else {}
        out.append({"to": to.lower(), "tx_hash": tx, "timestamp": t.get("timestamp"),
                    "value": total.get("value") or t.get("value")})
        if len(out) >= cap:
            break
    return out


def _backfill_buy(rec, chain, acq, receipt_id):
    """A backfill BUY event -- same ledger schema as build_rwa_event, but sourced from
    on-chain history (no verdict, no price-paid)."""
    rec = rec if isinstance(rec, dict) else {}
    return {
        "event": "buy", "receipt_id": receipt_id, "ts": _to_unix(acq.get("timestamp")),
        "counterparty": None, "payer": acq.get("to"), "amount": None, "chain": chain,
        "token": acq.get("token"), "verdict": None, "issuer": rec.get("issuer"),
        "symbol": rec.get("symbol"), "underlying_symbol": rec.get("underlying_symbol"),
        "trading_halted": bool(rec.get("trading_halted")), "restriction_grade": None,
        "restriction_standard": None, "peg_ratio": None, "underlying_price": None,
        "paid_unit_price": None, "pre_balance": None, "source": "backfill",
    }


def backfill_token(ledger, token_record, chain, fetch, existing_ids=None, *,
                   max_pages=DEFAULT_MAX_PAGES, cap=DEFAULT_PER_TOKEN_CAP, now=None):
    """Backfill one token's acquisitions into `ledger` as buy + settled outcome events,
    idempotent via `existing_ids` (receipt_id set). `token_record` is the registry record
    {token, issuer, symbol, underlying_symbol, ...}. Returns the count of NEW acquisitions
    written. A transfer that landed on-chain is a SUCCESSFUL settlement -> settled=True."""
    token = token_record.get("token") if isinstance(token_record, dict) else None
    if not is_evm_address(token):
        return 0
    existing_ids = existing_ids if existing_ids is not None else set()
    acqs = token_acquisitions(fetch, token, max_pages=max_pages, cap=cap)
    n = 0
    for a in acqs:
        a = dict(a, token=token)
        rid = "backfill:%s:%s:%s" % (chain, a.get("tx_hash"), a.get("to"))
        if rid in existing_ids:
            continue
        buy = _backfill_buy(token_record, chain, a, rid)
        ledger.record(buy)
        ledger.record(build_outcome_event(
            buy, {"settled": True, "holds_balance": True}, now=buy.get("ts") or now))
        existing_ids.add(rid)
        n += 1
    return n


def backfill(ledger, records, chain, fetch, *, max_pages=DEFAULT_MAX_PAGES,
             cap=DEFAULT_PER_TOKEN_CAP, now=None):
    """Backfill each token record for `chain`. Fail-soft: one bad token doesn't abort the
    run. Loads the ledger's existing receipt_ids ONCE for idempotency. Returns a summary
    {tokens, acquisitions, errors, per_token}."""
    existing = {e.get("receipt_id") for e in ledger.load()
                if isinstance(e, dict) and e.get("receipt_id")}
    per, total, errors, ok = {}, 0, 0, 0
    for rec in records or []:
        token = rec.get("token")
        if not is_evm_address(token):
            per[str(token)] = {"skipped": "not a valid EVM address"}
            continue
        try:
            k = backfill_token(ledger, rec, chain, fetch, existing,
                               max_pages=max_pages, cap=cap, now=now)
        except Exception as e:
            per[token.lower()] = {"error": type(e).__name__}
            errors += 1
            continue
        per[token.lower()] = {"acquisitions": k}
        total += k
        ok += 1
    return {"tokens": ok, "acquisitions": total, "errors": errors, "per_token": per}


class RwaTransferPager:
    """Live keyless transport: pages a token's transfers via Blockscout v2
    (`GET /api/v2/tokens/{token}/transfers`). `fetch(token, params) -> (items, next)`."""

    def __init__(self, chain, timeout=8.0, bases=None):
        self.base = (bases or BLOCKSCOUT_BASES).get((chain or "").strip().lower())
        self.timeout = timeout

    def __call__(self, token, params):
        import http_util
        from urllib.parse import urlencode
        if not self.base:
            return ([], None)
        url = "%s/api/v2/tokens/%s/transfers" % (self.base, token)
        if params:
            url += "?" + urlencode(params)
        data = http_util.get_json(url, timeout=self.timeout)
        if not isinstance(data, dict):
            return ([], None)
        return (data.get("items") or [], data.get("next_page_params"))


def _seed_records(chain):
    """Registry records for `chain` (Backed feed + operator seed)."""
    from tokenized_stock_registry import build_registry
    reg = build_registry()
    seen, out = set(), []
    for (net, addr), rec in reg.by_key.items():
        if net == chain and addr not in seen:
            seen.add(addr)
            out.append(dict(rec, token=addr))
    return out


def main(argv=None):
    """CLI: backfill an issuer's on-chain history into the RWA corpus.
    `python rwa_backfill.py rwa.jsonl --chain ethereum [--cap 500] [--max-pages 5]`"""
    import argparse
    import json
    import sys
    import time
    from rwa_ledger import RwaLedger
    ap = argparse.ArgumentParser(description="Seed the RWA corpus from on-chain history.")
    ap.add_argument("ledger", help="path to the rwa_ledger JSONL corpus")
    ap.add_argument("--chain", default="ethereum", help="chain to backfill (default ethereum)")
    ap.add_argument("--cap", type=int, default=DEFAULT_PER_TOKEN_CAP)
    ap.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    args = ap.parse_args(argv)
    records = _seed_records(args.chain)
    summary = backfill(RwaLedger(args.ledger), records, args.chain,
                       RwaTransferPager(args.chain), max_pages=args.max_pages,
                       cap=args.cap, now=int(time.time()))
    sys.stdout.write(json.dumps({"chain": args.chain, "token_records": len(records),
                                 **summary}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
