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
    try:
        with _LOCK:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:
        return False
    return True


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
