#!/usr/bin/env python3
"""
solana_backfill.py -- pull x402 settlement history for Solana payees.

WHY THIS EXISTS
---------------
Blackwall's x402 corpus is Base USDC. Base is only about half the network: a
harvest of live 402 challenges from 195 hosts returned 353 quotes across 36
(chain, asset) pairs on 26 CHAINS -- Base 53%, Solana 15%, Polygon 9%, Arbitrum
7%. The token axis is nearly settled (USDC ~90% of quotes; DAI, PYUSD, USDe,
FDUSD and RLUSD appear ZERO times), so indexing more stablecoins buys almost
nothing. Indexing more CHAINS buys a lot, and Solana is the largest one missing.

Measured before writing this, on the 44 Solana payees that advertise USDC:
37 have a USDC token account, 93,876 signatures in total, and a sampled 97.5%
of those are inbound USDC. That is roughly twice the entire Base corpus (46,031
settlements) from 37 payees -- Solana has few payees and heavy traffic per payee.

WHY SOLANA NEEDS ITS OWN MODULE
--------------------------------
`chain_backfill` walks EVM Transfer LOGS. Solana has no logs to filter: balances
live in per-owner TOKEN ACCOUNTS, and a payee's advertised `payTo` is the OWNER,
not the account that actually receives USDC. So the walk is
owner -> `getTokenAccountsByOwner` -> token account -> `getSignaturesForAddress`
-> `getTransaction` per signature. No PDA derivation and no base58 arithmetic:
the RPC resolves the token account for us, which is why this stays stdlib.

THE PAGINATION TRAP -- the reason this module exists as code and not a script
-----------------------------------------------------------------------------
`getSignaturesForAddress` returns at most 1000 entries; you page backwards with
`before`. The obvious loop stops when a page comes back short. THAT IS WRONG,
and wrong in the direction that hides itself: a rate-limited or failed page also
comes back short (or empty), and the loop reads it as "end of history" and
returns a truncated corpus with no error. Measured cost of exactly this bug
during sizing: 66,209 signatures reported against a true 93,876 -- a 29% silent
loss, and the deepest payee read as 19,000 instead of 46,682.

So `collect_signatures` NEVER infers exhaustion from a short page. It confirms
with a follow-up request, and if the transport fails it raises
`IncompleteHistory` rather than returning what it happens to have. A partial
backfill that announces itself is recoverable; one that looks complete is not.

RATE LIMITS: DO NOT REACH FOR BATCHING
---------------------------------------
The cost driver is one `getTransaction` per signature. JSON-RPC batching looks
like the fix and is not: the public endpoints rate-limit PER CALL INSIDE the
batch, returning ~8 results whether the batch holds 10 or 100 ("Too many
requests for a specific RPC call"), while the same signatures fetched serially
all succeed. Measured ceiling is ~12 tx/s across three public endpoints at 4
workers; 8 workers draw 429s. Budget ~2.2 hours for a full cold backfill, or
supply a keyed archival endpoint and this collapses to minutes.

SCOPE / HONESTY
---------------
Inbound USDC to a payee's token account is treated as its settlement history; we
do not distinguish an x402 payment from any other inbound USDC to that same
account. Same bargain `chain_backfill` makes, and acceptable for the same reason
-- it is the endpoint's own advertised payTo.

The payee set comes from live 402 challenges, so it carries the SAME CRAWL BIAS
as the Base corpus: a buyer's other counterparties are never seen, which
inflates `buyer_exclusivity` in `volume_integrity`. Price uniformity is the
signal that survives this; see that module's notes before quoting any Solana
verdict. One further caution specific to Solana: a SINGLE account holds 50% of
all 93,876 signatures and the top five hold 84%, so screen before aggregating.

Pure parsing is separated from transport: `usdc_delta` and `normalize_payment`
take an already-fetched transaction and never touch the network, so the
interesting logic is testable without a socket.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

import user_agent as ua_policy

#: Circulating USDC on Solana mainnet. Identified by MINT, so a lookalike token
#: with the same symbol cannot contribute history -- the same rule
#: `settlement_watch` applies by contract address on Base.
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

#: Public endpoints, rotated on failure. None of these are archival-guaranteed;
#: history proved complete in practice (payee first-activity dates spread across
#: nine months back to 2025-01-06 with no clustering, which a retention wall
#: would have produced).
DEFAULT_ENDPOINTS = ("https://api.mainnet-beta.solana.com",
                     "https://solana-rpc.publicnode.com",
                     "https://api.mainnet.solana.com")

#: ONE owner for User-Agent policy (user_agent.py) -- a literal here is a
#: lock violation, and four uncoordinated UA strings is what the lock exists
#: to prevent. Browser-prefixed, like the other chain-RPC callers: public
#: RPCs sit behind Cloudflare and 403 a bare urllib UA.
DEFAULT_UA = ua_policy.browser("solana-backfill")
HTTP_TIMEOUT = 45

#: `getSignaturesForAddress` hard-caps at 1000 regardless of what is requested.
SIGNATURE_PAGE_LIMIT = 1000

#: Bounds a misbehaving pager. 600 pages is 600k signatures -- far beyond the
#: 46,682 of the largest payee observed, so hitting it means something is wrong.
MAX_PAGES = 600


class IncompleteHistory(Exception):
    """Raised when the walk could not be completed.

    Deliberately an exception and not a partial return: a truncated corpus that
    presents as complete silently corrupts every statistic computed from it.
    """


def _account_keys(tx):
    """Pubkeys from a transaction, tolerating both encodings.

    `jsonParsed` yields dicts with a `pubkey`; the legacy encoding yields bare
    strings. Reading only one shape drops every transaction served the other
    way, which looks like a payee with no history rather than a parse bug.
    """
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    out = []
    for k in msg.get("accountKeys") or ():
        out.append(k.get("pubkey") if isinstance(k, dict) else k)
    return out


def usdc_delta(tx, account, *, mint=SOLANA_USDC):
    """PURE: net change in `account`'s balance of `mint`, in base units.

    Returns None when the transaction failed or never touched that account's
    mint balance -- distinct from 0, which means it touched and netted flat.

    Works from `pre`/`postTokenBalances`, which is what makes this robust: it
    reports the ACTUAL balance change rather than trusting a parsed instruction,
    so a multi-instruction transaction, a CPI, or a transfer routed through a
    program all net out correctly.

    Base units, not floats: `uiAmount` is a float and a decimal-converted
    integer picks up representation noise, which `volume_integrity` would then
    read as price VARIETY -- exactly the signal that must not be faked.
    """
    meta = (tx or {}).get("meta") or {}
    if meta.get("err"):
        return None
    keys = _account_keys(tx)
    try:
        idx = keys.index(account)
    except ValueError:
        return None

    def side(field):
        for b in meta.get(field) or ():
            if b.get("accountIndex") == idx and b.get("mint") == mint:
                amt = (b.get("uiTokenAmount") or {}).get("amount")
                try:
                    return int(amt)
                except (TypeError, ValueError):
                    return None
        return None

    pre, post = side("preTokenBalances"), side("postTokenBalances")
    if pre is None and post is None:
        return None
    return (post or 0) - (pre or 0)


def token_decimals(tx, account, *, mint=SOLANA_USDC):
    """PURE: the mint's decimals as this transaction reports them, or None.

    Read per transaction rather than hardcoded so a wrong assumption shows up as
    a missing value instead of a silently mis-scaled amount.
    """
    meta = (tx or {}).get("meta") or {}
    keys = _account_keys(tx)
    try:
        idx = keys.index(account)
    except ValueError:
        return None
    for field in ("postTokenBalances", "preTokenBalances"):
        for b in meta.get(field) or ():
            if b.get("accountIndex") == idx and b.get("mint") == mint:
                d = (b.get("uiTokenAmount") or {}).get("decimals")
                if isinstance(d, int):
                    return d
    return None


def payer_of(tx, account, *, mint=SOLANA_USDC):
    """PURE: the OWNER wallet whose `mint` balance fell, i.e. who paid.

    Returns the owner, not the token account. Blackwall counts DISTINCT PAYERS,
    and one wallet's token account is derived from its owner -- keying on the
    token account would count the same payer separately per mint and understate
    concentration. `volume_integrity` reads these as `buyers`, so the identity
    has to be the wallet.

    None when no single account fell (a multi-payer transaction, or balances the
    RPC did not report), which is honest: a guessed payer becomes a fabricated
    edge in the payment graph.
    """
    meta = (tx or {}).get("meta") or {}
    if meta.get("err"):
        return None
    keys = _account_keys(tx)
    try:
        payee_idx = keys.index(account)
    except ValueError:
        payee_idx = None

    def amounts(field):
        out = {}
        for b in meta.get(field) or ():
            if b.get("mint") != mint:
                continue
            try:
                out[b.get("accountIndex")] = (int((b.get("uiTokenAmount") or {}).get("amount")),
                                              b.get("owner"))
            except (TypeError, ValueError):
                continue
        return out

    pre, post = amounts("preTokenBalances"), amounts("postTokenBalances")
    fell = []
    for idx in set(pre) | set(post):
        if idx == payee_idx:
            continue
        before = pre.get(idx, (0, None))[0]
        after = post.get(idx, (0, None))[0]
        if after < before:
            fell.append(pre.get(idx, post.get(idx))[1])
    return fell[0] if len(fell) == 1 else None


def normalize_payment(tx, signature, account, *, mint=SOLANA_USDC):
    """PURE: one inbound payment record, or None if this is not one.

    Outbound and flat transactions return None -- a payee's own spending is not
    its settlement history, and counting it would inflate every volume figure.
    """
    delta = usdc_delta(tx, account, mint=mint)
    if delta is None or delta <= 0:
        return None
    dec = token_decimals(tx, account, mint=mint)
    return {"signature": signature,
            "account": account,
            "payer": payer_of(tx, account, mint=mint),
            "amount_raw": delta,
            "decimals": dec,
            "amount": (delta / float(10 ** dec)) if dec is not None else None,
            "block_time": (tx or {}).get("blockTime"),
            "slot": (tx or {}).get("slot")}


def collect_signatures(rpc, account, *, limit=SIGNATURE_PAGE_LIMIT,
                       max_pages=MAX_PAGES):
    """Every signature touching `account`, newest first.

    `rpc(method, params) -> result`, raising on transport failure.

    A SHORT PAGE IS NOT PROOF OF EXHAUSTION -- see the module docstring. Ending
    the walk there costs history silently whenever a page is rate-limited, so a
    short page is confirmed with one more request before it is believed. A
    transport failure raises `IncompleteHistory`; it never returns a truncated
    list that reads as the whole record.
    """
    out, seen, before, pages = [], set(), None, 0

    def absorb(page):
        """Extend `out` with entries not already held. Returns the new cursor.

        Deduped because the cursor can fail to advance: if a page's last entry
        carries no signature, `before` goes None and the next request re-serves
        the newest page. `chain_backfill` was bitten by a re-serving pager
        already -- there the store's idempotent key absorbed it and only the
        reported count was wrong. Here nothing downstream would catch it.
        """
        for entry in page:
            sig = entry.get("signature")
            if sig is not None and sig in seen:
                continue
            if sig is not None:
                seen.add(sig)
            out.append(entry)
        return page[-1].get("signature")

    while pages < max_pages:
        params = {"limit": limit}
        if before:
            params["before"] = before
        try:
            page = rpc("getSignaturesForAddress", [account, params]) or []
        except Exception as exc:
            raise IncompleteHistory(
                "%s: page %d failed after %d signatures: %s"
                % (account, pages + 1, len(out), exc))
        if not page:
            return out
        before = absorb(page)
        pages += 1
        if len(page) < limit:
            # Might be the end, might be a degraded response. Ask again.
            try:
                confirm = rpc("getSignaturesForAddress",
                              [account, {"limit": limit, "before": before}]) or []
            except Exception as exc:
                raise IncompleteHistory(
                    "%s: could not confirm exhaustion after %d signatures: %s"
                    % (account, len(out), exc))
            if not confirm:
                return out
            before = absorb(confirm)
            pages += 1
    raise IncompleteHistory(
        "%s: hit the %d-page cap with history remaining" % (account, max_pages))


def token_account(rpc, owner, *, mint=SOLANA_USDC):
    """The owner's token account for `mint`, or None if it has never held any.

    None is a real answer, not an error: 7 of 44 Solana x402 payees advertise a
    USDC price and have no USDC account at all -- they have never been paid.
    """
    res = rpc("getTokenAccountsByOwner", [owner, {"mint": mint},
                                          {"encoding": "jsonParsed"}]) or {}
    vals = res.get("value") or []
    return vals[0].get("pubkey") if vals else None


def payee_payments(rpc, owner, *, mint=SOLANA_USDC, max_pages=MAX_PAGES,
                   on_progress=None):
    """Inbound USDC payment records for a payee `owner`, newest first.

    Returns {owner, account, signatures, payments, skipped}. `skipped` counts
    signatures that were not inbound payments, so the yield rate stays visible
    rather than being quietly absorbed.
    """
    account = token_account(rpc, owner, mint=mint)
    if account is None:
        return {"owner": owner, "account": None, "signatures": 0,
                "payments": [], "skipped": 0, "failed": []}
    sigs = collect_signatures(rpc, account, max_pages=max_pages)
    payments, skipped, failed = [], 0, []
    for i, s in enumerate(sigs):
        sg = s.get("signature")
        if not sg:
            skipped += 1
            continue
        try:
            tx = rpc("getTransaction",
                     [sg, {"encoding": "jsonParsed",
                           "maxSupportedTransactionVersion": 0}])
        except Exception:
            # One unfetchable signature must not discard the walk -- the largest
            # observed payee is 46,682 signatures and re-running it is 65 minutes.
            # Recorded rather than swallowed: `failed` is NOT `skipped`, because
            # a skip is a known non-payment and a failure is an unknown.
            failed.append(sg)
            continue
        rec = normalize_payment(tx, sg, account, mint=mint)
        if rec is None:
            skipped += 1
        else:
            payments.append(rec)
        if on_progress and (i + 1) % 250 == 0:
            on_progress(owner, i + 1, len(sigs))
    return {"owner": owner, "account": account, "signatures": len(sigs),
            "payments": payments, "skipped": skipped, "failed": failed}


def to_graph(results, *, min_amount=None, max_amount=None):
    """Payee results -> `(amounts_by_payee, buyers_by_payee, pays_to)`, the
    shape `volume_integrity.screen` takes.

    Band filtering is OPTIONAL and off by default. `volume_integrity` cannot see
    how its input was filtered and will report a share of whatever denominator
    it is handed, so the choice belongs to the caller and should be stated
    alongside any figure derived from it.

    NO `funders_of` IS PRODUCED, and that is deliberate. Funding data needs the
    payers' own inbound history, which this walk does not collect; returning an
    empty map would let `screen_payee` score 2-of-2 as though three signals had
    been checked. Passing nothing makes it record `funder_concentration` as
    unchecked instead -- see that module on absent evidence never counting as
    evidence.
    """
    amounts, buyers, pays_to = {}, {}, {}
    for r in results or ():
        owner = r.get("owner")
        if owner is None:
            continue
        amounts.setdefault(owner, [])
        for p in r.get("payments") or ():
            amt = p.get("amount")
            if amt is None:
                continue
            if min_amount is not None and amt < min_amount:
                continue
            if max_amount is not None and amt > max_amount:
                continue
            amounts[owner].append(amt)
            payer = p.get("payer")
            if payer:
                buyers.setdefault(owner, set()).add(payer)
                pays_to.setdefault(payer, set()).add(owner)
    return amounts, buyers, pays_to


def decimal_string(amount_raw, decimals):
    """Base units -> an exact decimal string. No float ever touches this.

    `str(raw / 10 ** decimals)` is the obvious version and it is wrong, though
    not for the reason one expects -- Python's repr is shortest-round-trip, so
    it does not emit "0.020000000000000004". It emits SCIENTIFIC NOTATION: of
    the base-unit amounts 1..199999 at 6 decimals, 99 render as "1e-06",
    "2e-06" and so on, and at 9 decimals essentially every small amount does.

    These strings become the `amount` column and the dedup key. "1e-06" and
    "0.000001" are two spellings of one price, which makes re-ingest
    non-idempotent and makes `price_uniformity` read a fixed-price seller as
    varied -- defeating the only signal in `volume_integrity` that a backfill
    cannot fake. x402 sellers quote down to $0.0001, so this is the live range,
    not a hypothetical one.
    """
    if decimals is None:
        return None
    sign = "-" if amount_raw < 0 else ""
    digits = str(abs(int(amount_raw))).rjust(decimals + 1, "0")
    if decimals == 0:
        return sign + digits
    whole, frac = digits[:-decimals], digits[-decimals:]
    frac = frac.rstrip("0")
    return sign + whole + ("." + frac if frac else "")


def to_transfers(results):
    """Payee results -> rows for `reputation_store.ingest_transfers`.

    The counterparty is the OWNER, not the token account: the owner is what a
    seller advertises as `payTo` and what every other part of the system keys
    on, so storing the token account would make the corpus unjoinable to the
    directory it came from.

    Records with no signature, no amount or no payer-visible decimals are
    dropped rather than stored with a hole -- `ingest_transfers` dedups on
    `tx_hash` and SQLite treats NULLs as distinct, so a row with a null
    signature would duplicate itself on every re-ingest.
    """
    out = []
    for r in results or ():
        owner = r.get("owner")
        if not owner:
            continue
        for p in r.get("payments") or ():
            amount = decimal_string(p.get("amount_raw"), p.get("decimals"))
            if amount is None or not p.get("signature"):
                continue
            out.append({"to": owner,
                        "from": p.get("payer"),
                        "amount": amount,
                        "tx_hash": p["signature"],
                        "timestamp": p.get("block_time")})
    return out


def make_rpc(endpoints=DEFAULT_ENDPOINTS, *, user_agent=DEFAULT_UA,
             timeout=HTTP_TIMEOUT, tries=4, sleep=None, opener=None):
    """A transport over rotating public endpoints. The one impure part.

    Rotates on every attempt: the endpoints rate-limit independently, so a 429
    from one is routinely served by the next. An RPC-level `error` is retried
    like a transport failure -- "Too many requests for a specific RPC call"
    arrives as a 200 with an error body, and treating it as a real answer is how
    a rate limit turns into missing history.
    """
    _sleep = sleep or time.sleep
    _open = opener or urllib.request.urlopen

    def rpc(method, params=None):
        last = None
        for i in range(tries):
            url = endpoints[i % len(endpoints)]
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                               "params": params or []}).encode("utf-8")
            req = urllib.request.Request(
                url, data=body,
                headers={"content-type": "application/json",
                         "user-agent": user_agent})
            try:
                with _open(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read())
                if payload.get("error"):
                    last = RuntimeError(str(payload["error"]))
                else:
                    return payload.get("result")
            except Exception as exc:      # transport, decode, or RPC-level
                last = exc
            if i < tries - 1:
                _sleep(1.0 * (i + 1))
        raise last or RuntimeError("%s failed" % method)
    return rpc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("payees", nargs="+", help="Solana owner addresses (payTo)")
    parser.add_argument("--mint", default=SOLANA_USDC)
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--json", help="write records here")
    parser.add_argument("--signatures-only", action="store_true",
                        help="count history without the per-signature fetch")
    args = parser.parse_args(argv)

    rpc = make_rpc()
    out, total, paid = [], 0, 0
    for owner in args.payees:
        try:
            if args.signatures_only:
                acct = token_account(rpc, owner, mint=args.mint)
                n = len(collect_signatures(rpc, acct, max_pages=args.max_pages)) if acct else 0
                r = {"owner": owner, "account": acct, "signatures": n,
                     "payments": [], "skipped": 0}
            else:
                r = payee_payments(rpc, owner, mint=args.mint,
                                   max_pages=args.max_pages)
        except IncompleteHistory as exc:
            print("INCOMPLETE %s: %s" % (owner, exc), file=sys.stderr)
            continue
        out.append(r)
        total += r["signatures"]
        paid += len(r["payments"])
        print("%-46s %7d signatures  %7d payments  %s"
              % (owner, r["signatures"], len(r["payments"]),
                 r["account"] or "-- never received this mint --"))
    print("\n%d signatures, %d inbound payments across %d payees"
          % (total, paid, len(out)))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1)
        print("wrote %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
