"""Runtime probe of MCP servers -- reachability and TOOL DEFINITIONS.

WHY THIS EXISTS
---------------
The official MCP registry versions its own metadata (synced every four hours,
120 revisions observed in 88.6 days), so archiving registry rows has no moat --
the publisher already keeps that history. See `docs/WHERE_TO_POINT_IT.md`.

What the registry does NOT record is what a server actually DOES when you call
it. That has to be probed, and it is where the documented attacks live:

- A server can be listed and simply not answer. One security study found 193 of
  464 confirmed servers (41.6%) unreachable three days later.
- The signature MCP attack is the RUG PULL: ship a benign tool, get adopted, then
  change the tool's description or input schema. The description is prompt text
  the model obeys, so editing it is a code change with no code review. Nobody is
  recording, continuously and at scale, what each tool SAID on each day.

This module takes that reading. Paired with `ecosystem_history`, the daily
`tools_digest` becomes a drift alarm: same server, same version, different
digest means the tools changed underneath everyone already installed.

DESIGN
------
- Pure classification and digest functions first; network confined to
  `probe_remote` and injected in tests.
- We record a DIGEST plus the tool names and descriptions, not just a count. A
  rug pull that swaps a description while keeping the name is invisible to a
  count and obvious in a digest.
- `auth_required` is deliberately its own class, NOT lumped with dead. A gated
  server is alive and in use; scoring it dead would overstate ecosystem
  mortality, which is the exact error the x402 liveness survey had to fix twice.
- Unreachable is NOT the same as unlisted. This module only reports what it
  observed; the registry's own delistings are a separate fact.
"""

import hashlib
import json

# Handshake outcomes, best to worst.
TOOLS_LISTED = "tools_listed"     # full handshake, tools enumerated
MCP_ALIVE = "mcp_alive"           # initialize succeeded, tools/list did not
AUTH_REQUIRED = "auth_required"   # alive but gated (401/403)
HTTP_ERROR = "http_error"         # reachable host, non-MCP or failing response
DEAD = "dead"                     # DNS failure, refused, timeout
LOCAL_ONLY = "local_only"         # package-based, no remote endpoint to probe

PROBEABLE = frozenset({TOOLS_LISTED, MCP_ALIVE, AUTH_REQUIRED})

PROTOCOL_VERSION = "2025-06-18"
USER_AGENT = "mcp-history/1.0 (+ecosystem drift survey)"


def remotes_of(entry):
    """Remote endpoint URLs for a registry entry, in listed order."""
    server = entry.get("server") or {}
    out = []
    for r in server.get("remotes") or []:
        url = r.get("url")
        if url:
            out.append(url)
    return out


def registry_status(entry):
    """The registry's own status string for an entry ('active', 'deleted', ...)."""
    meta = (entry.get("_meta") or {}).get(
        "io.modelcontextprotocol.registry/official"
    ) or {}
    return meta.get("status")


def is_latest(entry):
    """True when this row is the registry's current version of the server.

    The registry returns every historical version; counting them all inflates
    the ecosystem several-fold.
    """
    meta = (entry.get("_meta") or {}).get(
        "io.modelcontextprotocol.registry/official"
    ) or {}
    return bool(meta.get("isLatest"))


def normalize_tool(tool):
    """The parts of a tool definition that change agent behavior.

    Description and input schema are included because BOTH are read by the
    model: the description is instruction text, and the schema names the
    arguments it will fill. A rug pull edits these while keeping the name.
    """
    return {
        "name": tool.get("name"),
        "description": tool.get("description") or "",
        "inputSchema": tool.get("inputSchema") or {},
    }


def tools_digest(tools):
    """Stable digest over a tool list, order-independent.

    Sorted by name so a server reordering its tools does not read as drift --
    that would bury real changes in false alarms.
    """
    norm = sorted((normalize_tool(t) for t in tools),
                  key=lambda t: t["name"] or "")
    blob = json.dumps(norm, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def classify_status(code):
    """Map an HTTP status to a probe class."""
    if code in (401, 403):
        return AUTH_REQUIRED
    return HTTP_ERROR


def summarize(results):
    """Counts per class, plus how many servers yielded tool definitions."""
    out = {}
    for r in results:
        out[r["class"]] = out.get(r["class"], 0) + 1
    return out


def drift(prev_rows, curr_rows):
    """Servers whose tool digest changed between two probe readings.

    Keyed on server name. A changed digest at the SAME version is the rug-pull
    signature: the published version did not move, but the tools did.
    """
    a = {r["name"]: r for r in prev_rows if r.get("tools_digest")}
    b = {r["name"]: r for r in curr_rows if r.get("tools_digest")}
    out = []
    for name in sorted(set(a) & set(b)):
        if a[name]["tools_digest"] != b[name]["tools_digest"]:
            out.append({
                "name": name,
                "was": a[name]["tools_digest"],
                "now": b[name]["tools_digest"],
                "version_was": a[name].get("version"),
                "version_now": b[name].get("version"),
                "same_version": a[name].get("version") == b[name].get("version"),
            })
    return out


# --- network half ---------------------------------------------------------

import urllib.error
import urllib.request

TIMEOUT = 15
MAX_BODY = 2_000_000


def _read_sse(resp, limit, deadline):
    """Read an SSE response only until the first complete JSON-RPC message.

    MEASURED BUG (first live run): streamable-HTTP servers keep the event
    stream open after answering, so a plain `read(N)` blocks accumulating
    keepalive frames until the byte cap or the socket timeout. On the first
    13,901-server survey this pinned ~3.5 cores and slowed the back half of the
    run by roughly 10x. Stop at the first parseable `data:` payload instead --
    that is the whole answer; everything after it is the server idling.
    """
    import time as _t
    buf = b""
    while len(buf) < limit and _t.monotonic() < deadline:
        chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
        if not chunk:
            break
        buf += chunk
        for line in buf.split(b"\n"):
            if line.startswith(b"data:"):
                payload = line[5:].strip()
                if payload:
                    try:
                        return json.loads(payload)
                    except json.JSONDecodeError:
                        continue
    return _parse_body(buf, "text/event-stream")


def _parse_body(raw, content_type):
    """Decode an MCP response body.

    Streamable-HTTP servers may answer a POST with either a plain JSON object
    or an SSE stream carrying the JSON in `data:` lines. Handling only JSON
    mis-reads every SSE server as broken.
    """
    text = raw.decode("utf-8", "replace").strip()
    if "text/event-stream" in (content_type or "") or text.startswith("event:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    try:
                        return json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _rpc(url, payload, session=None, timeout=TIMEOUT):
    """One JSON-RPC POST. Returns (parsed, session_id, status)."""
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": USER_AGENT,
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    import time as _t
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        sid = resp.headers.get("Mcp-Session-Id") or session
        ctype = resp.headers.get("Content-Type") or ""
        if "text/event-stream" in ctype:
            parsed = _read_sse(resp, MAX_BODY, _t.monotonic() + timeout)
        else:
            parsed = _parse_body(resp.read(MAX_BODY), ctype)
        return parsed, sid, resp.status


def probe_remote(url, timeout=TIMEOUT):
    """Handshake with one remote MCP endpoint and list its tools.

    Returns dict with class, and on success tool names/descriptions + digest.
    Never raises: an unreachable server is a measurement, not an error.
    """
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcp-history", "version": "1.0"},
        },
    }
    try:
        parsed, sid, _ = _rpc(url, init, timeout=timeout)
    except urllib.error.HTTPError as e:
        return {"class": classify_status(e.code), "detail": f"HTTP {e.code}"}
    except Exception as e:
        return {"class": DEAD, "detail": type(e).__name__}

    if not parsed or "result" not in parsed:
        return {"class": HTTP_ERROR, "detail": "no initialize result"}

    info = (parsed["result"] or {}).get("serverInfo") or {}
    base = {
        "class": MCP_ALIVE,
        "server_name": info.get("name"),
        "server_version": info.get("version"),
        "protocol": (parsed["result"] or {}).get("protocolVersion"),
    }

    try:
        _rpc(url, {"jsonrpc": "2.0", "method": "notifications/initialized"},
             session=sid, timeout=timeout)
    except Exception:
        pass  # optional notification; a server may reject it and still serve tools

    try:
        parsed, _, _ = _rpc(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                            session=sid, timeout=timeout)
    except urllib.error.HTTPError as e:
        base["detail"] = f"tools/list HTTP {e.code}"
        return base
    except Exception as e:
        base["detail"] = f"tools/list {type(e).__name__}"
        return base

    if not parsed or "result" not in parsed:
        base["detail"] = "no tools result"
        return base

    tools = (parsed["result"] or {}).get("tools") or []
    base.update({
        "class": TOOLS_LISTED,
        "tool_count": len(tools),
        "tools": [normalize_tool(t) for t in tools],
        "tools_digest": tools_digest(tools),
    })
    return base


def probe_entry(entry, timeout=TIMEOUT):
    """Probe a registry entry, trying each listed remote until one answers."""
    server = entry.get("server") or {}
    row = {
        "name": server.get("name"),
        "version": server.get("version"),
        "registry_status": registry_status(entry),
    }
    urls = remotes_of(entry)
    if not urls:
        row["class"] = LOCAL_ONLY
        return row
    best = None
    for url in urls:
        res = probe_remote(url, timeout=timeout)
        res["url"] = url
        if res["class"] == TOOLS_LISTED:
            row.update(res)
            return row
        if best is None or res["class"] in PROBEABLE:
            best = res
    row.update(best or {"class": DEAD})
    return row


def census(entries):
    """Registry composition from a raw pull (all versions included).

    Reports the version-inflation factor explicitly: the registry returns every
    historical revision, and counting rows rather than latest-version servers
    overstates the ecosystem several-fold.
    """
    names = {}
    latest = [e for e in entries if is_latest(e)]
    for e in entries:
        n = (e.get("server") or {}).get("name")
        if n:
            names[n] = names.get(n, 0) + 1
    active = [e for e in latest if registry_status(e) == "active"]
    remote = [e for e in active if remotes_of(e)]
    return {
        "rows_all_versions": len(entries),
        "distinct_servers": len(names),
        "latest_rows": len(latest),
        "active": len(active),
        "deprecated": len(latest) - len(active),
        "with_remote": len(remote),
        "package_only": len(active) - len(remote),
    }
