#!/usr/bin/env python3
"""
reachability_ledger.py -- did we reach this host, and what happened LAST time?

WHY THIS EXISTS. `apiwitchcraft.duckdns.org` has been probed by this project at
least four times and told a different story every time: "still live", "went
quiet, never fixed", "answers and the payTo is repaired", and today six
consecutive HTTP timeouts against a host whose TLS handshake completes on the
first try. CLAUDE.md documents that confusion at length, which is the tell: the
confusion was never about the host, it was about US. Every probe OVERWRITES the
last one -- `data/liveness.json` is a snapshot with no date, and
`asset_coverage.json` carries a single `generated_at` -- so nobody could ever
answer "have we seen this before?" except from memory.

This is the memory. Append-only, dated, one line per observation.

THE DESIGN DECISION THAT MATTERS, because getting it wrong would manufacture
evidence against innocent sellers: OUR OWN FAILURES ARE RECORDED SEPARATELY FROM
THEIRS. A probe we declined to make (the SSRF guard refused the URL), or one that
failed inside our own network, is `skipped` -- it is a fact about us, and it must
never accumulate into "this seller was down six times". Only an attempt that
genuinely reached the wire and got nothing back is `unreachable`, and even that
is phrased as "we could not reach you", never "you were down": today's six
timeouts came through a proxy while a direct handshake succeeded, and from here
there is no way to tell those apart.

So the ledger records OBSERVATIONS, never uptime. The distinction is the whole
point of keeping it.
"""

import json
import os
import threading
import time

# Outcome classes. The split is on WHOSE failure it is, not on severity.
ANSWERED = "answered"        # a response came back (any HTTP status)
UNREACHABLE = "unreachable"  # we tried, nothing came back -- cause unknown
SKIPPED = "skipped"          # WE did not try, or failed on our own side

OUTCOMES = (ANSWERED, UNREACHABLE, SKIPPED)

# A run of silences only becomes worth reporting once it is both long enough and
# spread over enough time that a single bad afternoon cannot produce it.
RUN_FOR_CONCERN = 3
DAYS_FOR_CONCERN = 3.0

MAX_DETAIL = 200

# BOUNDS. The portal is public, so this file grows from STRANGERS' traffic: 514
# probeable corpus hosts against a 15-minute report cache is ~49k rows/day worst
# case, about 16 MB, and `load` scans the whole file per report -- so an
# uncapped ledger degrades the thing it exists to serve (measured: 0.09s at 50k
# rows, and it only goes up). Keeping the most recent rows per host bounds both
# the file and the read, and costs nothing that matters: `summarize` only needs
# the recent tail to compute a run, and a year-old observation does not change
# whether a host answered this week.
# THE CONTRACT, stated precisely because the loose version misled its own test:
# compaction is SIZE-triggered, so `KEEP_PER_HOST` is the retention floor
# immediately AFTER a compaction, not a per-host ceiling that holds at every
# instant. Between compactions rows accumulate normally. What is actually
# guaranteed is the FILE bound -- roughly COMPACT_ABOVE_BYTES plus whatever
# arrives before the next crossing -- and that is the property that matters,
# since the risk was unbounded growth and a whole-file scan per report.
KEEP_PER_HOST = 200
COMPACT_ABOVE_BYTES = 4 * 1024 * 1024

MAX_DETAIL = 200
_LOCK = threading.Lock()

# Operational data, not a committed artifact -- the root .gitignore excludes
# *.jsonl, which is right for a file that grows on every probe. That does mean a
# container starts with no memory, and the memory is the whole point, so a deploy
# points this at its persistent disk (`BLACKWALL_REACHABILITY=/data/reach.jsonl`
# alongside BLACKWALL_STORE). Falling back beside the module keeps a local run
# working with no configuration.
DEFAULT_PATH = os.environ.get(
    "BLACKWALL_REACHABILITY",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "data", "reachability.jsonl"))


def _safe(text, limit=MAX_DETAIL):
    """Third-party error strings land in this file and are rendered later."""
    value = text if isinstance(text, str) else str(text)
    if len(value) > limit:
        value = value[:limit] + "..."
    return repr(value)[1:-1]


def classify(probe):
    """(outcome, detail) for a `seller_report.probe_endpoint` result.

    A probe carrying `status` answered. A probe whose error says we declined --
    the SSRF guard's "not probed:" prefix -- is OURS, not theirs. Everything else
    reached the wire and got nothing, which we call unreachable WITHOUT claiming
    whose fault it was.
    """
    if not isinstance(probe, dict):
        return SKIPPED, "no probe performed"
    error = probe.get("error")
    if not error:
        if probe.get("status") is None:
            return SKIPPED, "probe returned no status"
        return ANSWERED, "http %s" % _safe(probe.get("status"), 12)
    if str(error).startswith("not probed"):
        return SKIPPED, _safe(error)
    return UNREACHABLE, _safe(error)


def record(host, outcome, detail="", path=None, now=None, source="probe"):
    """Append one observation. Fail-soft: logging must never break a report."""
    if not host or outcome not in OUTCOMES:
        return False
    path = path or DEFAULT_PATH
    event = {"host": str(host).lower(), "outcome": outcome,
             "detail": _safe(detail), "source": _safe(source, 40),
             "ts": float(now if now is not None else time.time())}
    line = json.dumps(event, sort_keys=True) + "\n"
    oversize = False
    try:
        with _LOCK:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
            oversize = os.path.getsize(path) > COMPACT_ABOVE_BYTES
    except Exception:
        return False
    if oversize:
        # Outside the lock -- compact() takes it itself, and holding it across
        # a whole-file rewrite would stall every concurrent report.
        compact(path)
    return True


def compact(path=None, keep=None):
    """Rewrite the ledger keeping only the most recent `keep` rows per host.

    Called automatically once the file crosses COMPACT_ABOVE_BYTES. Written to a
    temporary file and moved into place with `os.replace`, which is atomic on
    POSIX, so a reader always sees a complete file.

    HONEST LIMITATION: this is an append-only log written by more than one
    process (the engine and the portal are separate services). A row appended by
    another process during the rewrite is lost. That is acceptable HERE and would
    not be in a ledger of record -- these are observations, the loss window is
    milliseconds, and losing one probe result cannot change a run that needs
    several observations over several days. It is called out rather than hidden
    because the same shortcut in `rwa_ledger` would be a defect.
    """
    # Resolved at CALL time, not bound as a default argument: a default captures
    # the constant when the function is DEFINED, so an operator (or a test)
    # retuning the module constant would silently keep the original value. The
    # same trap as any mutable-default bug, and here it would make a documented
    # knob inert.
    path = path or DEFAULT_PATH
    keep = KEEP_PER_HOST if keep is None else keep
    with _LOCK:
        try:
            if os.path.getsize(path) <= COMPACT_ABOVE_BYTES:
                return 0
        except OSError:
            return 0
        events = load(path)
        per_host = {}
        for event in events:
            per_host.setdefault(event.get("host"), []).append(event)
        kept = []
        for host_events in per_host.values():
            kept.extend(host_events[-keep:])
        kept.sort(key=lambda e: e.get("ts") or 0.0)
        tmp = path + ".compact"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                for event in kept:
                    fh.write(json.dumps(event, sort_keys=True) + "\n")
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return 0
        return len(events) - len(kept)


def load(path=None, host=None):
    """Every observation, oldest first; optionally for one host.

    Tolerant by contract -- a corrupt line is skipped, not raised. This file is
    append-only from several processes and a torn write must not blind the
    reader to the other 500 rows.
    """
    path = path or DEFAULT_PATH
    needle = str(host).lower() if host else None
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if needle and str(event.get("host") or "").lower() != needle:
                    continue
                out.append(event)
    except FileNotFoundError:
        return []
    except Exception:
        return []
    out.sort(key=lambda e: e.get("ts") or 0.0)
    return out


def summarize(events, now=None):
    """What our observations of one host actually support.

    `skipped` rows are counted and then EXCLUDED from every judgement: they are
    our failures, and letting them into a run of silences would let a broken
    proxy or an over-strict URL guard build a case against a seller who was fine
    the whole time.
    """
    now = float(now if now is not None else time.time())
    tried = [e for e in events if e.get("outcome") in (ANSWERED, UNREACHABLE)]
    skipped = [e for e in events if e.get("outcome") == SKIPPED]
    answered = [e for e in tried if e.get("outcome") == ANSWERED]

    summary = {
        "attempts": len(tried),
        "answered": len(answered),
        "unreachable": len(tried) - len(answered),
        "skipped": len(skipped),
        "first_seen": tried[0]["ts"] if tried else None,
        "last_answered": answered[-1]["ts"] if answered else None,
        "consecutive_silent": 0,
        "silent_days": 0.0,
        "state": "unobserved",
    }
    if not tried:
        return summary

    run = []
    for event in reversed(tried):
        if event.get("outcome") == UNREACHABLE:
            run.append(event)
        else:
            break
    summary["consecutive_silent"] = len(run)
    if run:
        # Span of the silent run, and how long since the last time it DID answer
        # -- the second is the honest number, because a run that started an hour
        # ago is not evidence of anything.
        summary["silent_days"] = max(
            0.0, (now - min(e["ts"] for e in run)) / 86400.0)

    if not run:
        summary["state"] = "answering"
    elif not answered:
        # Checked BEFORE the run rule, because it is the more specific claim: a
        # host we have NEVER reached is a different statement from one that
        # answered and stopped, and the vaguer label would swallow it.
        summary["state"] = "never_answered"
    elif (summary["consecutive_silent"] >= RUN_FOR_CONCERN
            and summary["silent_days"] >= DAYS_FOR_CONCERN):
        summary["state"] = "silent_run"
    else:
        # It has answered before and is not answering now. That is FLAPPING,
        # which is a real and distinct thing -- and the state this project kept
        # mistaking for "fixed" or "dead" depending on which day it looked.
        summary["state"] = "flapping"
    return summary


def describe(summary):
    """One sentence an operator or a seller can read. Never claims uptime."""
    state = summary.get("state")
    attempts, answered = summary.get("attempts", 0), summary.get("answered", 0)
    if state == "unobserved":
        return "We have no recorded attempts to reach this host."
    base = "We have reached it on %d of %d recorded attempts." % (answered, attempts)
    if state == "answering":
        return base + " The most recent attempt succeeded."
    if state == "silent_run":
        return (base + " The last %d attempts, over %.1f days, got no response."
                % (summary["consecutive_silent"], summary["silent_days"]))
    if state == "never_answered":
        return ("None of our %d recorded attempts reached it." % attempts)
    return (base + " It answered before and did not on the last %d attempts, so "
            "it is intermittent rather than simply up or down."
            % summary["consecutive_silent"])


def observe(host, probe, path=None, now=None, source="probe"):
    """Classify a probe result and record it. Returns (outcome, detail)."""
    outcome, detail = classify(probe)
    record(host, outcome, detail, path=path, now=now, source=source)
    return outcome, detail


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(description="Reachability history for a host.")
    p.add_argument("host", nargs="?", help="host to summarize (default: all)")
    p.add_argument("--path", default=DEFAULT_PATH)
    args = p.parse_args(argv)

    if args.host:
        events = load(args.path, args.host)
        summary = summarize(events)
        print("%s\n  %s" % (args.host, describe(summary)))
        for event in events[-10:]:
            print("  %s  %-12s %s"
                  % (time.strftime("%Y-%m-%d %H:%M",
                                   time.gmtime(event.get("ts") or 0)),
                     event.get("outcome"), event.get("detail")))
        return 0

    hosts = {}
    for event in load(args.path):
        hosts.setdefault(event.get("host"), []).append(event)
    for host in sorted(hosts):
        print("%-42s %s" % (host, describe(summarize(hosts[host]))))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
