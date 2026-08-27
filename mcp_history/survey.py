"""Take one dated reading of the MCP ecosystem and store it append-only.

Pulls the full registry (every version), reduces to current active servers,
probes the ones with a remote endpoint, and writes the reading through
`ecosystem_history.history.store`, which refuses to overwrite an existing date.

    python mcp_history/survey.py --out data/mcp_snapshots

The registry returns every historical revision, so the pull is large (83k rows
for 25k servers at the first reading). `census()` reports both numbers, because
quoting the row count as the ecosystem size overstates it several-fold -- an
error worth avoiding given published counts vary by exactly that factor.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timezone, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ecosystem_history"))

import probe as P  # noqa: E402
import history as H  # noqa: E402

REGISTRY = "https://registry.modelcontextprotocol.io/v0/servers"


def fetch_registry(base=REGISTRY, page=100, retries=5, log=print):
    """Every registry row, following the cursor. Retries transient failures."""
    out, cursor, pages = [], None, 0
    while True:
        q = {"limit": page}
        if cursor:
            q["cursor"] = cursor
        url = base + "?" + urllib.parse.urlencode(q)
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    doc = json.load(r)
                break
            except Exception:
                if attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt)
        rows = doc.get("servers") or []
        out.extend(rows)
        pages += 1
        meta = doc.get("metadata") or {}
        cursor = meta.get("nextCursor") or meta.get("next_cursor")
        if not cursor or not rows:
            break
        if log and pages % 50 == 0:
            log(f"  registry: {pages} pages, {len(out)} rows")
    return out


def probeable(entries):
    """Current, active servers that expose a remote endpoint."""
    return [e for e in entries
            if P.is_latest(e)
            and P.registry_status(e) == "active"
            and P.remotes_of(e)]


def run_probes(entries, workers=24, timeout=10, log=print):
    rows = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(P.probe_entry, e, timeout): e for e in entries}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                rows.append(f.result())
            except Exception as exc:
                s = futs[f].get("server") or {}
                rows.append({"name": s.get("name"), "version": s.get("version"),
                             "class": P.DEAD, "detail": type(exc).__name__})
            if log and i % 1000 == 0:
                log(f"  probed {i}/{len(entries)}")
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/mcp_snapshots")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--timeout", type=int, default=10)
    ap.add_argument("--when", help="ISO date; defaults to today (UTC)")
    args = ap.parse_args(argv)

    when = date.fromisoformat(args.when) if args.when else \
        datetime.now(timezone.utc).date()

    print("pulling registry...")
    entries = fetch_registry()
    stats = P.census(entries)
    print(json.dumps(stats, indent=2))

    targets = probeable(entries)
    print(f"probing {len(targets)} remote servers...")
    rows = run_probes(targets, args.workers, args.timeout)

    reading = {"date": when.isoformat(), "census": stats,
               "summary": P.summarize(rows), "servers": rows}
    path = H.store(args.out, reading, when)
    print("stored", path)
    print(json.dumps(P.summarize(rows), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
