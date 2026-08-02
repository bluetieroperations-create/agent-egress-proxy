#!/usr/bin/env python3
"""
settlement_velocity.py -- the TEMPORAL axis of counterparty reputation.

`distinct_payers` counts how many payers a payee has; the payer graph asks who they
are. Neither asks WHEN. A wash-farm can manufacture N distinct, cross-paying wallets
-- but it funds and fires them in a BURST: 30 "distinct payers" whose FIRST payment
all lands within an hour is Sybil-shaped in a way count and graph can't see. An
organic payee acquires its payers gradually.

This module reads the settlement timestamps (already ingested, previously unused) and
derives, per payee:
  * age_days / recency_days -- how long the history spans, and how fresh it is.
  * peak_day_share -- the largest fraction of DISTINCT payers first seen on a single
    UTC day. ~1.0 => nearly all payers appeared at once (burst); low => spread out.
  * burst_sybil -- enough distinct payers, yet peak_day_share is extreme: their
    diversity was manufactured in a window. Orthogonal to the graph (which is about
    who pays whom, not when).
  * stale -- the last settlement is old; the reputation may be out of date.

Pure + stdlib; events and `now` are injected, so it's fully unit-tested.

What GATES vs what's DIAGNOSTIC (eval finding, docs/PAYER_GRAPH.md):
  * `stale` GATES the verdict (HOLD, never STOP, fail-open). last_seen is robust --
    the backfill always fetches the MOST RECENT settlements -- so "no settlement in
    STALE_DAYS" reliably flags a possibly-dead/abandoned endpoint.
  * `burst_sybil` is DIAGNOSTIC ONLY -- it does NOT gate. A TARGETED backfill captures
    only a recent WINDOW (e.g. 3 pages ~= 150 settlements), so for a high-volume payee
    (150 settlements/day) the whole visible history compresses into ~1 day and every
    payer looks "acquired at once" -- it flags the MOST reputable payees (aidress.ai,
    Nansen). Real burst detection needs COMPLETE history (a full backfill or the live
    receipt stream), so this flag is surfaced for research, not acted on, until then.
"""
from __future__ import annotations

from collections import Counter

from settlement_watch import parse_ts

MIN_DISTINCT_PAYERS = 3           # mirror blackwall's naive gate
BURST_MIN_DISTINCT = 5            # need >= this many payers for a "burst" to mean anything
BURST_PEAK_SHARE = 0.8           # >= this share of distinct payers first seen in ONE day
STALE_DAYS = 90                  # last settlement older than this -> reputation is stale


def _as_dt(ts):
    return ts if hasattr(ts, "tzinfo") else parse_ts(ts)


def temporal_signal(events, *, now=None,
                    burst_min_distinct=BURST_MIN_DISTINCT,
                    burst_peak_share=BURST_PEAK_SHARE, stale_days=STALE_DAYS):
    """Temporal reputation signal for one payee. `events` = iterable of (ts, payer)
    (ts ISO-8601 or datetime). `now` (datetime) enables recency/stale. Pure. Returns
    None when no event has a parseable timestamp (fail-open)."""
    parsed = []
    for ts, payer in events or ():
        dt = _as_dt(ts)
        if dt is None:
            continue
        p = (payer or "").lower() or None
        parsed.append((dt, p))
    if not parsed:
        return None

    times = [dt for dt, _ in parsed]
    first, last = min(times), max(times)
    # payer ACQUISITION: the first time each distinct payer is seen.
    first_by_payer = {}
    for dt, p in parsed:
        if p is None:
            continue
        if p not in first_by_payer or dt < first_by_payer[p]:
            first_by_payer[p] = dt
    distinct = len(first_by_payer)
    per_day = Counter(dt.date() for dt in first_by_payer.values())
    peak_day_new = max(per_day.values()) if per_day else 0
    peak_day_share = round(peak_day_new / distinct, 3) if distinct else None

    age_days = round((last - first).total_seconds() / 86400.0, 2)
    recency_days = None
    if now is not None:
        recency_days = round((_as_dt(now) - last).total_seconds() / 86400.0, 2)

    burst_sybil = (distinct >= burst_min_distinct
                   and peak_day_share is not None
                   and peak_day_share >= burst_peak_share)
    stale = recency_days is not None and recency_days > stale_days
    return {
        "settlements": len(parsed),
        "distinct_payers": distinct,
        "age_days": age_days,
        "recency_days": recency_days,
        "acquisition_days": len(per_day),        # distinct UTC days new payers appeared
        "peak_day_new_payers": peak_day_new,
        "peak_day_share": peak_day_share,
        "burst_sybil": burst_sybil,
        "stale": stale,
    }


class VelocitySource:
    """Reads a payee's settlement timeline from a ReputationStore and computes the
    temporal signal on demand (cached per payee). `now` is snapshotted at
    construction so recency is stable across a batch of verdicts."""

    def __init__(self, store, *, now=None, **kwargs):
        self._store = store
        self._now = now
        self._kw = kwargs
        self._cache = {}

    def temporal(self, counterparty):
        key = (counterparty or "").lower()
        if key not in self._cache:
            try:
                events = list(self._store.iter_settlement_events(key))
            except Exception:
                events = []
            self._cache[key] = temporal_signal(events, now=self._now, **self._kw)
        return self._cache[key]
