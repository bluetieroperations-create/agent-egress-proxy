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
                    now=None, receipt_id=None):
    """PURE: assemble one BUY accumulation event from a forecast's decision context.
    `now` (unix seconds) is passed in -- the builder does no I/O and no clock read.
    `receipt_id` is the join key an OUTCOME event links back to."""
    acq = clean.get("acquires") if isinstance(clean, dict) else {}
    acq = acq if isinstance(acq, dict) else {}
    rec = asset_record if isinstance(asset_record, dict) else {}
    sig = rwa_signal if isinstance(rwa_signal, dict) else {}
    pg = peg if isinstance(peg, dict) else {}
    return {
        "event": "buy",
        "receipt_id": receipt_id,
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


def build_outcome_event(buy_event, outcome, now=None):
    """PURE: assemble an OUTCOME event that LABELS a prior buy (linked by receipt_id).
    `outcome` is whatever the checker produced (mark_ratio / underlying_move /
    underwater / holds_balance). `now` is passed in."""
    be = buy_event if isinstance(buy_event, dict) else {}
    oc = outcome if isinstance(outcome, dict) else {}
    return {
        "event": "outcome",
        "receipt_id": be.get("receipt_id"),
        "ts": int(now) if now is not None else None,
        "buy_ts": be.get("ts"),
        "token": be.get("token"),
        "chain": be.get("chain"),
        "issuer": be.get("issuer"),
        "underlying_symbol": be.get("underlying_symbol"),
        "underlying_at_decision": be.get("underlying_price"),
        "underlying_now": oc.get("underlying_now"),
        "underlying_move": oc.get("underlying_move"),
        "mark_ratio": oc.get("mark_ratio"),
        "underwater": oc.get("underwater"),
        "holds_balance": oc.get("holds_balance"),
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

    # -- selection for the outcome loop --
    def pending_buys(self, horizon_seconds, now, events=None):
        """Buys older than `horizon_seconds` that carry a receipt_id and do NOT yet have
        a linked OUTCOME event -- the work-list for capture_outcomes (the T+N labeler)."""
        all_events = events if events is not None else self.load()
        done = {e.get("receipt_id") for e in all_events
                if e.get("event") == "outcome" and e.get("receipt_id")}
        out = []
        for e in all_events:
            if e.get("event", "buy") != "buy":
                continue
            rid, ts = e.get("receipt_id"), e.get("ts")
            if not rid or not isinstance(ts, int):
                continue
            if now - ts < horizon_seconds or rid in done:
                continue
            out.append(e)
        return out

    # -- aggregate (outcome-aware) --
    def asset_profile(self, token, chain=None, events=None):
        """Rollup for one asset. BUY metrics (verdict mix, restriction grades, peg
        samples) + OUTCOME metrics (settlement rate, underwater rate, avg mark) once
        outcomes have been captured. None if never seen."""
        all_events = events if events is not None else self.load()
        rows = [e for e in all_events if _match_token(e.get("token"), token)
                and (chain is None or (e.get("chain") or "") == (chain or ""))]
        buys = [e for e in rows if e.get("event", "buy") == "buy"]
        outs = [e for e in rows if e.get("event") == "outcome"]
        if not buys and not outs:
            return None
        pegs = [e["peg_ratio"] for e in buys if isinstance(e.get("peg_ratio"), (int, float))]
        prof = {
            "token": token, "chain": chain,
            "count": len(buys),
            "issuer": next((e.get("issuer") for e in rows if e.get("issuer")), None),
            "symbol": next((e.get("symbol") for e in buys if e.get("symbol")), None),
            "underlying_symbol": next((e.get("underlying_symbol") for e in rows
                                       if e.get("underlying_symbol")), None),
            "verdicts": dict(Counter(e.get("verdict") for e in buys)),
            "restriction_grades": dict(Counter(e.get("restriction_grade") for e in buys
                                               if e.get("restriction_grade"))),
            "halt_count": sum(1 for e in buys if e.get("trading_halted")),
            "peg_samples": len(pegs),
            "max_peg_ratio": max(pegs) if pegs else None,
            "avg_peg_ratio": (sum(pegs) / len(pegs)) if pegs else None,
            "last_seen": max((e["ts"] for e in rows if isinstance(e.get("ts"), int)),
                             default=None),
        }
        prof.update(_outcome_metrics(outs))
        return prof

    def issuer_profile(self, issuer, events=None):
        """Rollup across ALL of an issuer's assets: buy verdict mix + OUTCOME metrics
        (settlement reliability, underwater rate) -- the earned issuer-trust-tier input."""
        rows = [e for e in (events if events is not None else self.load())
                if (e.get("issuer") or "") == (issuer or "")]
        buys = [e for e in rows if e.get("event", "buy") == "buy"]
        outs = [e for e in rows if e.get("event") == "outcome"]
        if not buys and not outs:
            return None
        prof = {
            "issuer": issuer,
            "count": len(buys),
            "distinct_assets": len({_asset_key(e.get("token"), e.get("chain")) for e in buys}),
            "verdicts": dict(Counter(e.get("verdict") for e in buys)),
            "restriction_grades": dict(Counter(e.get("restriction_grade") for e in buys
                                               if e.get("restriction_grade"))),
            "halt_count": sum(1 for e in buys if e.get("trading_halted")),
            "chains": sorted({e.get("chain") for e in buys if e.get("chain")}),
        }
        prof.update(_outcome_metrics(outs))
        return prof


def _match_token(a, b):
    """Chain-aware address equality: EVM case-insensitive, base58 exact."""
    if not a or not b:
        return False
    if a.startswith("0x") and b.startswith("0x"):
        return a.lower() == b.lower()
    return a == b


def _outcome_metrics(outcomes):
    """Derive the LABELED metrics from a set of captured outcome events. None values
    when a dimension has no samples (never fabricate a rate from zero evidence)."""
    settled = [e["holds_balance"] for e in outcomes if isinstance(e.get("holds_balance"), bool)]
    under = [e["underwater"] for e in outcomes if isinstance(e.get("underwater"), bool)]
    marks = [e["mark_ratio"] for e in outcomes if isinstance(e.get("mark_ratio"), (int, float))]
    return {
        "outcomes": len(outcomes),
        "settlement_success_rate": (sum(settled) / len(settled)) if settled else None,
        "underwater_rate": (sum(under) / len(under)) if under else None,
        "avg_mark_ratio": (sum(marks) / len(marks)) if marks else None,
    }


# Minimum LABELED outcomes before an issuer's trust grade is meaningful (below this,
# the grade is "insufficient" -- no evidence is not trust, mirroring reputation_score).
ISSUER_TRUST_MIN_OUTCOMES = 5


def issuer_trust(profile):
    """PURE: map an outcome-aware `issuer_profile` -> {grade, reasons}. DESCRIPTIVE for
    now (the roadmap graduates it to a conservative verdict input once calibrated).

    grade: 'insufficient' (too few labeled outcomes), 'high', 'medium', 'low'. Built
    from settlement reliability + underwater rate + STOP share -- never from raw buy
    volume (which is wash-tradeable)."""
    if not isinstance(profile, dict):
        return {"grade": "insufficient", "reasons": ["no profile"]}
    n_out = profile.get("outcomes") or 0
    reasons = []
    if n_out < ISSUER_TRUST_MIN_OUTCOMES:
        return {"grade": "insufficient",
                "reasons": ["only %d labeled outcome(s) (< %d) -- not enough evidence"
                            % (n_out, ISSUER_TRUST_MIN_OUTCOMES)]}
    settle = profile.get("settlement_success_rate")
    under = profile.get("underwater_rate")
    verdicts = profile.get("verdicts") or {}
    total_v = sum(verdicts.values()) or 1
    stop_share = verdicts.get("STOP", 0) / total_v

    score = 0
    if settle is not None:
        reasons.append("settlement success %.0f%%" % (settle * 100))
        score += 1 if settle >= 0.95 else (0 if settle >= 0.8 else -1)
    if under is not None:
        reasons.append("underwater %.0f%% of outcomes" % (under * 100))
        score += 1 if under <= 0.2 else (0 if under <= 0.5 else -1)
    if stop_share > 0.1:
        reasons.append("%.0f%% of buys STOPped" % (stop_share * 100))
        score -= 1
    grade = "high" if score >= 2 else ("low" if score <= -1 else "medium")
    return {"grade": grade, "reasons": reasons}
