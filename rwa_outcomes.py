#!/usr/bin/env python3
"""
rwa_outcomes.py -- the OUTCOME-capture loop: the piece that LABELS the accumulation
corpus, turning "what Blackwall decided" into "what actually happened."

A buy event records the decision (verdict, peg-at-decision, restriction grade). By
itself that's an opinion. The moat is the LABEL: re-check the asset AFTER the buy and
record how it turned out. This module closes that loop with two labels:

  * MARK-TO-MARKET (peg persistence) -- sound + keyless via Pyth. Re-fetch the
    underlying equity price at T+N; the position's mark = underlying_now / price_paid.
    `mark_ratio < 1` = the buyer is underwater relative to the real stock. Cross-check
    against the decision-time overpay: if the peg gate flagged an overpay AND the buy
    went underwater, the warning is VINDICATED -- exactly the labeled evidence that
    trains issuer/asset reputation.
  * SETTLEMENT-HELD (optional) -- via an injected balance reader: does the payer now
    hold a positive balance of the acquired token? A heuristic proxy for "the security
    actually arrived" (imperfect without tx attribution; recorded as `holds_balance`).

`capture_outcomes(ledger, checker, horizon, now)` is the driver: for each pending buy
(older than the T+N horizon, not yet labeled) it runs the checker and appends an outcome
event, joined to the buy by receipt_id. Run it on a schedule (cron/CLI). FAIL-OPEN
throughout -- an unreachable oracle yields None metrics, never a crash. Stdlib.
"""
from __future__ import annotations

from rwa_ledger import build_outcome_event


def _fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def assess_outcome(buy_event, underlying_now, holds_balance=None):
    """PURE: label a buy from the underlying's price NOW (+ optional balance-held).

    Returns {underlying_now, underlying_move, mark_ratio, underwater, holds_balance}.
      * underlying_move = underlying_now / underlying_at_decision (did the stock move?)
      * mark_ratio      = underlying_now / price_paid (position mark; <1 = underwater)
    Any dimension with missing inputs is None (never fabricated). NEVER raises."""
    be = buy_event if isinstance(buy_event, dict) else {}
    u1 = _fnum(underlying_now)
    u0 = _fnum(be.get("underlying_price"))
    paid = _fnum(be.get("paid_unit_price"))
    move = (u1 / u0) if (u1 is not None and u0 not in (None, 0)) else None
    mark = (u1 / paid) if (u1 is not None and paid not in (None, 0)) else None
    return {
        "underlying_now": u1,
        "underlying_move": move,
        "mark_ratio": mark,
        "underwater": (mark < 1.0) if mark is not None else None,
        "holds_balance": holds_balance if isinstance(holds_balance, bool) else None,
    }


class OutcomeChecker:
    """Produce an outcome label for a buy event. `pyth_source` gives the underlying
    price now (fail-open None). `balance_reader` (optional) is callable(token, chain,
    payer) -> bool|None for the settlement-held heuristic. NEVER raises out of check()."""

    def __init__(self, pyth_source=None, balance_reader=None):
        self.pyth_source = pyth_source
        self.balance_reader = balance_reader

    def check(self, buy_event):
        be = buy_event if isinstance(buy_event, dict) else {}
        underlying_now = None
        sym = be.get("underlying_symbol")
        if self.pyth_source is not None and sym:
            try:
                underlying_now = self.pyth_source.price(sym)
            except Exception:
                underlying_now = None
        holds = None
        if self.balance_reader is not None and be.get("payer"):
            try:
                holds = self.balance_reader(be.get("token"), be.get("chain"),
                                            be.get("payer"))
            except Exception:
                holds = None
        return assess_outcome(be, underlying_now, holds_balance=holds)


def capture_outcomes(ledger, checker, horizon_seconds, now, max_events=None):
    """The T+N labeling loop: for each pending buy (older than `horizon_seconds`, not
    yet labeled), run `checker` and append an outcome event. Returns the number of
    outcomes recorded. FAIL-OPEN per-item: one bad buy never stops the batch."""
    pending = ledger.pending_buys(horizon_seconds, now)
    if max_events is not None:
        pending = pending[:max_events]
    n = 0
    for buy in pending:
        try:
            outcome = checker.check(buy)
            ledger.record(build_outcome_event(buy, outcome, now=now))
            n += 1
        except Exception:
            continue
    return n


def main(argv=None):
    """CLI: label the corpus. Run on a schedule (e.g. hourly cron) with a T+N horizon.
    `python rwa_outcomes.py rwa.jsonl --horizon-hours 24`"""
    import argparse
    import time
    from pyth_price import PythPriceSource
    from rwa_ledger import RwaLedger
    ap = argparse.ArgumentParser(description="Label RWA buys with T+N outcomes.")
    ap.add_argument("ledger", help="path to the rwa_ledger JSONL corpus")
    ap.add_argument("--horizon-hours", type=float, default=24.0,
                    help="only label buys older than this (default 24h)")
    ap.add_argument("--max", type=int, default=None, help="cap outcomes per run")
    ap.add_argument("--evm-rpc", default=None, help="EVM RPC for the settlement-held read")
    ap.add_argument("--solana-rpc", default=None, help="Solana RPC for settlement-held")
    args = ap.parse_args(argv)
    led = RwaLedger(args.ledger)
    balance_reader = None
    if args.evm_rpc or args.solana_rpc:
        from rwa_balance import BalanceReader
        balance_reader = BalanceReader(evm_rpc_url=args.evm_rpc,
                                       solana_rpc_url=args.solana_rpc)
    checker = OutcomeChecker(PythPriceSource(), balance_reader=balance_reader)
    n = capture_outcomes(led, checker, int(args.horizon_hours * 3600),
                         int(time.time()), max_events=args.max)
    print("captured %d outcome(s) from %s" % (n, args.ledger))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
