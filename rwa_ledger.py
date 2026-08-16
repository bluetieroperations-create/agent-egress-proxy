#!/usr/bin/env python3
"""
rwa_ledger.py -- the ACCUMULATION corpus for tokenized-RWA / crypto buys.

The flip from READING public data to ACCUMULATING a private one. Every time Blackwall
forecasts an RWA purchase it sees something no competitor sees -- an agent about to buy
asset X, at price P, with restriction state R -- and (via the outcome loop) what
happened next. This module WRITES that down, append-only, so Blackwall slowly builds a
labeled history nobody else has: which issuers settle cleanly, which tokens depeg, which
got frozen. A snapshot anyone can take; this is the diary only we keep.

Pure event builder + a JSONL append/aggregate store. Aggregations derive the metrics
that matter -- per-ASSET verdict mix / restriction grades / peg samples / halt count,
and per-ISSUER rollups (the earned "issuer trust tier" input). DESCRIPTIVE: this is a
data tap, not a gate. Stdlib only. Records carry NO secrets (counterparty/payer are
public on-chain addresses; no payload free-text).
"""
from __future__ import annotations

import json
import os
from collections import Counter


def build_rwa_event(clean, verdict, asset_record=None, rwa_signal=None, peg=None,
                    now=None):
    """PURE: assemble one accumulation event from a forecast's decision context. `now`
    (unix seconds) is passed in -- the builder does no I/O and no clock read."""
    acq = clean.get("acquires") if isinstance(clean, dict) else {}
    acq = acq if isinstance(acq, dict) else {}
    rec = asset_record if isinstance(asset_record, dict) else {}
    sig = rwa_signal if isinstance(rwa_signal, dict) else {}
    pg = peg if isinstance(peg, dict) else {}
    return {
        "ts": int(now) if now is not None else None,
        "counterparty": clean.get("counterparty"),
        "payer": clean.get("payer"),
        "amount": str(clean.get("amount")),
        "chain": acq.get("chain") or clean.get("chain"),
        "token": acq.get("token"),
        "verdict": verdict.get("verdict") if isinstance(verdict, dict) else None,
        "issuer": rec.get("issuer"),
        "symbol": rec.get("symbol"),
        "underlying_symbol": rec.get("underlying_symbol"),
        "trading_halted": bool(rec.get("trading_halted")),
        "restriction_grade": sig.get("grade"),
        "restriction_standard": sig.get("standard"),
        "peg_ratio": pg.get("divergence_ratio"),
        "underlying_price": pg.get("underlying_price"),
        "paid_unit_price": pg.get("paid_unit_price"),
    }


def _asset_key(token, chain=None):
    t = (token or "")
    t = t.lower() if t.startswith("0x") else t     # EVM case-insensitive; base58 exact
    return (chain or "", t)


class RwaLedger:
    """Append-only JSONL corpus of RWA/crypto buy events, with per-asset and per-issuer
    aggregation. FAIL-SOFT: a write error never propagates (logging must not break a
    verdict); a malformed line is skipped on load."""

    def __init__(self, path):
        self.path = path

    # -- write --
    def record(self, event):
        """Append one event. NEVER raises."""
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        except Exception:
            pass

    # -- read --
    def load(self):
        """Read all events (skipping malformed lines). [] if the file is absent."""
        out = []
        if not self.path or not os.path.exists(self.path):
            return out
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return out
        return out

    # -- aggregate --
    def asset_profile(self, token, chain=None, events=None):
        """Rollup for one asset: how often seen, verdict mix, restriction grades seen,
        halt count, peg samples (overpay ratios), last_seen ts. None if never seen."""
        all_events = events if events is not None else self.load()
        rows = [e for e in all_events if _match_token(e.get("token"), token)
                and (chain is None or (e.get("chain") or "") == (chain or ""))]
        if not rows:
            return None
        pegs = [e["peg_ratio"] for e in rows if isinstance(e.get("peg_ratio"), (int, float))]
        return {
            "token": token, "chain": chain,
            "count": len(rows),
            "issuer": next((e.get("issuer") for e in rows if e.get("issuer")), None),
            "symbol": next((e.get("symbol") for e in rows if e.get("symbol")), None),
            "underlying_symbol": next((e.get("underlying_symbol") for e in rows
                                       if e.get("underlying_symbol")), None),
            "verdicts": dict(Counter(e.get("verdict") for e in rows)),
            "restriction_grades": dict(Counter(e.get("restriction_grade") for e in rows
                                               if e.get("restriction_grade"))),
            "halt_count": sum(1 for e in rows if e.get("trading_halted")),
            "peg_samples": len(pegs),
            "max_peg_ratio": max(pegs) if pegs else None,
            "avg_peg_ratio": (sum(pegs) / len(pegs)) if pegs else None,
            "last_seen": max((e["ts"] for e in rows if isinstance(e.get("ts"), int)),
                             default=None),
        }

    def issuer_profile(self, issuer, events=None):
        """Rollup across ALL of an issuer's assets: total buys, verdict mix, distinct
        assets, restriction grades, halt count -- the earned issuer-trust-tier input."""
        rows = [e for e in (events if events is not None else self.load())
                if (e.get("issuer") or "") == (issuer or "")]
        if not rows:
            return None
        return {
            "issuer": issuer,
            "count": len(rows),
            "distinct_assets": len({_asset_key(e.get("token"), e.get("chain")) for e in rows}),
            "verdicts": dict(Counter(e.get("verdict") for e in rows)),
            "restriction_grades": dict(Counter(e.get("restriction_grade") for e in rows
                                               if e.get("restriction_grade"))),
            "halt_count": sum(1 for e in rows if e.get("trading_halted")),
            "chains": sorted({e.get("chain") for e in rows if e.get("chain")}),
        }


def _match_token(a, b):
    """Chain-aware address equality: EVM case-insensitive, base58 exact."""
    if not a or not b:
        return False
    if a.startswith("0x") and b.startswith("0x"):
        return a.lower() == b.lower()
    return a == b
