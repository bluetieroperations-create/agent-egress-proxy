"""Turn the one-off liveness survey into a RECORDED SERIES.

WHY THIS EXISTS
---------------
`directory_liveness.py` measures the x402 seller ecosystem on the day you run it:
195 hosts, each classified by whether its payment requirements can actually be
parsed. That measurement is genuinely proprietary -- it exists because we ran the
probe, not because we downloaded it -- but a single snapshot is one frame of a
film. Its value is almost entirely in the DIFF.

The asset is not "who is live". Anyone can measure that today. The asset is
"who WAS live, and who stopped" -- and that cannot be reconstructed after the
fact by anyone who did not start recording. A competitor who copies this idea in
six months still does not have this month.

So: snapshots are append-only and dated, never overwritten. Every derived signal
below is a function of two or more snapshots.

DESIGN NOTES
------------
- Keyed on `host`, not URL. A host that moves its endpoint path is the same
  seller; treating the URL as the key would report a move as a death plus a
  birth and inflate churn.
- `settlements` is CUMULATIVE on-chain history, so it may only ever rise for a
  given host. A DECREASE means the backfill window moved, not that settlements
  were undone -- so `growth` clamps at zero rather than reporting a negative,
  which would otherwise read as a shrinking seller.
- A host missing from a later snapshot is `disappeared`, which is NOT the same as
  the `dead` liveness class. Missing means the directory stopped advertising it;
  `dead` means we probed it and got nothing. Both matter and they are kept apart.
- Pure functions only. Reading and writing files is confined to the bottom.
"""

import gzip
import json
import os
import re

from datetime import date

# Classes from directory_liveness that mean "we can actually parse a price".
SCOREABLE = frozenset({"body_accepts", "hdr_accepts"})

SNAPSHOT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json(\.gz)?$")


def snapshot_name(when, compress=False):
    """Filename for a snapshot taken on `when` (a date).

    Readings carrying full text (MCP tool descriptions and schemas) run to
    hundreds of MB uncompressed, so they are stored gzipped. The date, not the
    extension, is the identity -- `store` refuses either form if the other
    already exists for that date.
    """
    return f"{when.isoformat()}.json.gz" if compress else f"{when.isoformat()}.json"


def parse_snapshot_name(filename):
    """Date encoded in a snapshot filename, or None if it is not one."""
    m = SNAPSHOT_RE.match(filename)
    if not m:
        return None
    return date.fromisoformat(m.group(1))


def index_by_host(results):
    """{host: row} for one snapshot.

    Later rows win, matching the survey's own ranking, which emits the
    best-classified probe for a host first.
    """
    out = {}
    for row in results:
        host = row.get("host")
        if host:
            out.setdefault(host, row)
    return out


def is_scoreable(row):
    return row.get("class") in SCOREABLE


def growth(prev_row, curr_row):
    """Settlements added between two observations of the same host.

    Clamped at zero: cumulative history cannot fall, so a drop is a moved
    backfill window, not negative growth.
    """
    before = prev_row.get("settlements") or 0
    after = curr_row.get("settlements") or 0
    return max(0, after - before)


def diff(prev, curr):
    """Transitions between two snapshots (lists of survey rows).

    Returns dict of host-lists plus per-host settlement growth.
    """
    a, b = index_by_host(prev), index_by_host(curr)
    appeared = sorted(set(b) - set(a))
    disappeared = sorted(set(a) - set(b))
    both = sorted(set(a) & set(b))

    became_payable, became_unpayable, class_changed = [], [], []
    for host in both:
        was, now = is_scoreable(a[host]), is_scoreable(b[host])
        if now and not was:
            became_payable.append(host)
        elif was and not now:
            became_unpayable.append(host)
        if a[host].get("class") != b[host].get("class"):
            class_changed.append(host)

    return {
        "appeared": appeared,
        "disappeared": disappeared,
        "became_payable": became_payable,
        "became_unpayable": became_unpayable,
        "class_changed": class_changed,
        "growth": {h: growth(a[h], b[h]) for h in both if growth(a[h], b[h])},
    }


def churn_rate(prev, curr):
    """Share of the earlier snapshot's hosts absent from the later one.

    Zero when the earlier snapshot is empty -- an undefined rate is reported as
    no churn rather than raising, since a caller charting a series should not
    crash on a leading empty snapshot.
    """
    a = index_by_host(prev)
    if not a:
        return 0.0
    gone = len(set(a) - set(index_by_host(curr)))
    return gone / len(a)


def survival(snapshots):
    """Per-host first_seen / last_seen / observations across dated snapshots.

    `snapshots` is a list of (date, results). This is the mortality table --
    the output that cannot be backfilled and is therefore the actual asset.
    """
    out = {}
    for when, results in sorted(snapshots, key=lambda s: s[0]):
        for host in index_by_host(results):
            rec = out.setdefault(
                host, {"first_seen": when, "last_seen": when, "observations": 0}
            )
            rec["last_seen"] = when
            rec["observations"] += 1
    for rec in out.values():
        rec["days_observed"] = (rec["last_seen"] - rec["first_seen"]).days
    return out


def still_alive(snapshots, as_of):
    """Hosts present in the latest snapshot on or before `as_of`."""
    dated = [s for s in sorted(snapshots, key=lambda s: s[0]) if s[0] <= as_of]
    if not dated:
        return set()
    return set(index_by_host(dated[-1][1]))


# --- I/O ------------------------------------------------------------------

def _existing(directory, when):
    for name in (snapshot_name(when), snapshot_name(when, True)):
        path = os.path.join(directory, name)
        if os.path.exists(path):
            return path
    return None


def store(directory, results, when, compress=False):
    """Write a dated snapshot. Refuses to overwrite an existing one.

    Append-only is the whole point: silently replacing a past measurement
    destroys the one thing here a competitor cannot reproduce.
    """
    os.makedirs(directory, exist_ok=True)
    clash = _existing(directory, when)
    if clash:
        raise FileExistsError(clash)
    path = os.path.join(directory, snapshot_name(when, compress))
    opener = gzip.open if compress else open
    with opener(path, "wt") as fh:
        json.dump(results, fh)
    return path


def load_all(directory):
    """[(date, results)] for every snapshot on disk, oldest first."""
    out = []
    for name in sorted(os.listdir(directory)):
        when = parse_snapshot_name(name)
        if when is None:
            continue
        path = os.path.join(directory, name)
        opener = gzip.open if name.endswith(".gz") else open
        with opener(path, "rt") as fh:
            out.append((when, json.load(fh)))
    return out
