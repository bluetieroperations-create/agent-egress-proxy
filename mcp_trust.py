#!/usr/bin/env python3
"""
mcp_trust.py -- fold the MCP ecosystem reading into the payment verdict.

WHY THIS EXISTS
---------------
An MCP server that charges via x402 is two things at once: a TOOL the agent calls
and a PAYEE Blackwall scores before signing. Reading #1 (2026-08-27) found 98 hosts
present in BOTH the MCP registry and `data/directory.json`, so these are literally
the same counterparties seen from two sides. See `docs/MCP_INTO_BLACKWALL.md`.

The signal the reading adds, which nothing else in the engine can see:

    THE PAYEE DOES NOT DO WHAT IT SELLS.

Five payment-taking servers serve only `echo`/`add`/`server_time` while advertising
real products -- including one selling "Escrow protection for agent payments --
USDC held in smart contract", and two selling pre-trade SAFETY tooling. Today
`decide_payment` scores such a payee on price and reputation and sees nothing
wrong: the address is clean, the price in range, the settlements real. An agent
paying for escrow receives an echo tool.

HARD BOUNDARY (mirrors blockscout.py and settlement_sim.py)
-----------------------------------------------------------
- **HOLD-only. NEVER STOP.** Sanctions and payload mismatch keep the STOP
  authority. A capability mismatch is strong grounds to pause, not proof of harm,
  and INTENT IS NOT ESTABLISHED for any publisher -- some of these are plausibly
  abandoned scaffolding. Blocking a payment outright on that inference would be
  wrong.
- **Never upgrades / never clears.** It can only add caution.
- **Fail-open.** Missing, stale or corrupt reading -> unknown -> no effect.
- **Never read from the request.** The index is built at startup from the
  COMMITTED snapshot, so a payload cannot inject a trust claim, and no network
  call happens on the hot path (no query leak -- see `rpc_node.py`).

TRIVIAL-TOOL SET
----------------
`echo`/`add`/`server_time`/`ping`/`hello`/`greet` are the scaffolding a template
generates. A server whose ENTIRE surface is a subset of these, while advertising
something substantive, is the mismatch. A server that merely ALSO ships `echo`
alongside real tools is not flagged -- that is a health probe, not a lie.
"""

import json

from urllib.parse import urlparse

# Grades
READY = "ready"                  # serves real tools; no concern
MISMATCH = "mismatch"            # advertises capability, serves only boilerplate
EMPTY = "empty"                  # handshakes and exposes zero tools
UNREACHABLE = "unreachable"      # listed but did not answer when measured
UNKNOWN = "unknown"              # not in the reading; fail-open

_GRADES = (READY, MISMATCH, EMPTY, UNREACHABLE, UNKNOWN)

# Precedence when ONE HOST serves several registry entries. Higher wins.
#
# AUDIT BUG (found 2026-08-27): this used `_GRADES.index()`, which ranks by how
# much we KNOW, not by how much it MATTERS. UNREACHABLE sat above MISMATCH, so a
# host with one lying entry and one dead entry graded `unreachable` and did NOT
# gate -- an unrelated stale registry row silently masked the lie.
#
# Gating grades must therefore always outrank non-gating ones. Between the rest,
# a host demonstrably serving real tools is READY; a dead sibling entry is a
# stale row, not evidence the host is down.
_PRECEDENCE = {UNKNOWN: 0, UNREACHABLE: 1, READY: 2, EMPTY: 3, MISMATCH: 4}
# Only these escalate. UNREACHABLE deliberately does NOT: the reading may be
# weeks old and an endpoint being down when measured is not evidence about a
# payment happening now. It is recorded for the operator, not gated.
_GATING = (MISMATCH, EMPTY)

TRIVIAL_TOOLS = frozenset({"echo", "add", "server_time", "ping", "hello", "greet"})
MIN_DESCRIPTION = 40
TOOLS_LISTED = "tools_listed"
PROBEABLE = frozenset({"tools_listed", "mcp_alive", "auth_required"})


def host_of(url_or_host):
    """Normalized host for joining. Lowercased, port and userinfo dropped.

    Registry URLs and x402 `payTo` resources are written by different parties;
    joining on a raw string misses the majority of real matches (the same class
    of bug that cost 64 of 69 endpoints in advertised_prices.py).
    """
    if not url_or_host:
        return None
    s = str(url_or_host).strip()
    host = urlparse(s).netloc if "//" in s else s
    host = host.rsplit("@", 1)[-1]            # strip userinfo
    host = host.split(":", 1)[0]              # strip port
    return host.lower() or None


def grade_row(row, description=""):
    """Grade one probed server. Pure."""
    if not isinstance(row, dict):
        return UNKNOWN
    cls = row.get("class")
    if cls not in PROBEABLE:
        return UNREACHABLE
    if cls != TOOLS_LISTED:
        return READY          # alive and gated; we simply cannot enumerate it
    names = {t.get("name") for t in (row.get("tools") or []) if isinstance(t, dict)}
    if not names:
        return EMPTY
    if names <= TRIVIAL_TOOLS and len(description or "") >= MIN_DESCRIPTION:
        return MISMATCH
    return READY


def build_index(reading, descriptions=None):
    """{host: {grade, tool_count, digest, name}} from a stored reading.

    `reading` is the dict written by `mcp_history.survey`, or a bare list of
    probed rows. Built ONCE at startup; never from a request.
    """
    if isinstance(reading, dict):
        rows = reading.get("servers") or []
    else:
        rows = reading or []
    descriptions = descriptions or {}
    index = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        host = host_of(row.get("url"))
        if not host:
            continue
        grade = grade_row(row, descriptions.get(row.get("name"), ""))
        prior = index.get(host)
        # A host serving several registry entries keeps the WORST grade: if any
        # entry on it advertises what it does not serve, the host is implicated.
        if prior and _PRECEDENCE.get(prior["grade"], 0) >= _PRECEDENCE.get(grade, 0):
            continue
        index[host] = {
            "grade": grade,
            "name": row.get("name"),
            "tool_count": row.get("tool_count"),
            "digest": row.get("tools_digest"),
        }
    return index


DEFAULT_READING = "data/mcp_snapshots"
DEFAULT_DESCRIPTIONS = "data/mcp_descriptions.json"


def load_source(snapshot_dir=DEFAULT_READING, descriptions=DEFAULT_DESCRIPTIONS,
                as_of=None):
    """Build an `McpTrustSource` from the newest committed reading, or None.

    The index is DERIVED at startup, never committed. An earlier version stored a
    1.21 MB pre-built index; it was removed once the same thing could be rebuilt
    from a 3.3 KB input.

    That removal exposed a real dependency worth stating: the snapshot does NOT
    record registry DESCRIPTIONS, and the capability-mismatch grade needs one --
    so rebuilding from the snapshot alone silently lost all 24 mismatch grades
    and the gate went dead while still looking healthy. `mcp_descriptions.json`
    is the minimal missing input: only a server whose ENTIRE tool surface is
    trivial can ever be a mismatch, so only those need a description (27 of
    13,901).

    Fail-open in every direction: a missing reading, an unreadable descriptions
    file or a corrupt archive all yield None, and the caller simply runs without
    the gate.
    """
    import gzip
    import os
    import re as _re

    # Match ONLY a dated snapshot filename. A `*.json*` glob also matches any
    # other JSON left in the directory -- including the descriptions file -- and
    # `sorted()[-1]` would then pick it as "the newest reading". Found by the
    # test below, which put both files in one directory.
    dated = _re.compile(r"^\d{4}-\d{2}-\d{2}\.json(\.gz)?$")
    try:
        names = sorted(n for n in os.listdir(snapshot_dir) if dated.match(n))
        if not names:
            return None
        path = os.path.join(snapshot_dir, names[-1])
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt") as fh:
            reading = json.load(fh)
    except Exception:
        return None

    descs = {}
    try:
        with open(descriptions) as fh:
            descs = json.load(fh)
    except Exception:
        descs = {}

    try:
        return McpTrustSource(reading=reading, descriptions=descs,
                              as_of=as_of or reading.get("date"))
    except Exception:
        return None


class McpTrustSource:
    """Startup-built lookup. `.signal(resource_or_host)` -> dict or None."""

    def __init__(self, index=None, reading=None, descriptions=None, as_of=None):
        self.index = index if index is not None else build_index(reading, descriptions)
        self.as_of = as_of

    def signal(self, resource):
        host = host_of(resource)
        if not host:
            return None
        entry = self.index.get(host)
        if not entry:
            return None
        out = dict(entry)
        out["host"] = host
        out["as_of"] = self.as_of
        return out


def apply_mcp_trust(verdict, signal):
    """PURE fold. CONSERVATIVE-ONLY: escalates GO -> HOLD when the payee's MCP
    endpoint does not serve what it advertises. NEVER upgrades, NEVER STOPs,
    never mutates the input. A None / non-dict / unknown-grade signal is a
    no-op. NEVER raises."""
    if not signal or not isinstance(signal, dict):
        return verdict
    if not isinstance(verdict, dict):
        return verdict
    grade = signal.get("grade")
    if grade not in _GRADES or grade in (UNKNOWN, READY):
        return verdict

    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    v["signals"]["mcp_trust"] = {
        "grade": grade,
        "host": signal.get("host"),
        "server": signal.get("name"),
        "tool_count": signal.get("tool_count"),
        "as_of": signal.get("as_of"),
        "gated": grade in _GATING,
    }
    if grade not in _GATING:
        return v

    reasons = list(v.get("reasons") or [])
    if grade == MISMATCH:
        reasons.append(
            "MCP capability mismatch: this payee's server advertises a substantive "
            "capability but serves only boilerplate tools -- the agent may be "
            "paying for something the endpoint does not implement")
    else:
        reasons.append(
            "MCP endpoint exposes NO tools: the payee's server completes the "
            "handshake and advertises nothing callable")
    v["reasons"] = reasons
    if v.get("verdict") == "GO":
        v["verdict"] = "HOLD"
    return v
