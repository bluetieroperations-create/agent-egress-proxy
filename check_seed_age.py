#!/usr/bin/env python3
"""
check_seed_age.py -- how fresh is the committed prebuilt reputation store?

The free-tier store (data/reputation_seed.db.gz) is a FROZEN snapshot: its settlement
timestamps don't advance, but the verdict's `stale` gate (settlement_velocity, default
STALE_DAYS=90) compares last_seen to NOW. So as the committed artifact ages, payees
cross the stale threshold together and start returning HOLD -- ~48% at +90d, ~100% at
+120d after the build. This tool reports the store's age and exits non-zero once it is
within the refresh window, so a human (or CI) knows to run `make refresh-seed` BEFORE
the corpus goes stale.

Pure age logic (`seed_age_days`, `assess`) is unit-tested; the CLI decompresses the
gz store, reads the newest settlement, and prints/【exits on】 the assessment.
"""
from __future__ import annotations

import datetime

# Refresh well BEFORE the stale cliff. STALE_DAYS(90) is when a payee with today's
# last_seen goes stale; refresh at 60 leaves a 30-day margin to notice + act.
REFRESH_WARN_DAYS = 60


def seed_age_days(newest_iso, now):
    """Whole days between the newest settlement timestamp and `now` (both aware
    datetimes; `newest_iso` an ISO-8601 string). Negative clamped to 0. PURE."""
    t = datetime.datetime.fromisoformat(str(newest_iso).replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return max(0, (now - t).days)


def assess(age_days, *, warn=REFRESH_WARN_DAYS, stale_days=90):
    """Classify the store's freshness. Returns {ok, level, age_days, days_to_cliff,
    message}. `ok` False => within the refresh window (CI should fail). PURE."""
    days_to_cliff = stale_days - age_days
    if age_days >= stale_days:
        level, ok = "stale", False
        msg = ("store is %d days old -- PAST the %d-day stale cliff; the corpus is "
               "returning HOLD. Run `make refresh-seed`." % (age_days, stale_days))
    elif age_days >= warn:
        level, ok = "aging", False
        msg = ("store is %d days old -- %d days until the %d-day stale cliff. "
               "Refresh soon: `make refresh-seed`." % (age_days, days_to_cliff, stale_days))
    else:
        level, ok = "fresh", True
        msg = "store is %d days old (%d days of margin)." % (age_days, days_to_cliff)
    return {"ok": ok, "level": level, "age_days": age_days,
            "days_to_cliff": days_to_cliff, "message": msg}


def _newest_ts(gz_path):
    import gzip
    import os
    import sqlite3
    import tempfile
    data = gzip.open(gz_path, "rb").read()
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with open(p, "wb") as f:
            f.write(data)
        row = sqlite3.connect(p).execute("SELECT MAX(ts) FROM settlements").fetchone()
        return row[0] if row else None
    finally:
        os.remove(p)


def main(argv=None):
    import argparse
    import sys
    from settlement_velocity import STALE_DAYS
    p = argparse.ArgumentParser(description="Report freshness of the committed seed store.")
    p.add_argument("--gz", default="data/reputation_seed.db.gz",
                   help="path to the gzipped seed store")
    p.add_argument("--warn-days", type=int, default=REFRESH_WARN_DAYS)
    args = p.parse_args(argv)

    newest = _newest_ts(args.gz)
    if not newest:
        sys.stderr.write("check_seed_age: no settlements found in %s\n" % args.gz)
        return 2
    now = datetime.datetime.now(datetime.timezone.utc)
    a = assess(seed_age_days(newest, now), warn=args.warn_days, stale_days=STALE_DAYS)
    sys.stdout.write("seed store: newest=%s  %s\n" % (newest, a["message"]))
    return 0 if a["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
