# MCP ecosystem — reading #1, 2026-08-27

First dated reading. Method in `mcp_history/`, stored append-only via
`ecosystem_history/` at `data/mcp_snapshots/2026-08-27.json.gz` (38 MB gzipped,
147 MB raw). Reproduce with `python mcp_history/survey.py`.

## Census

| | |
|---|---|
| Registry rows (all versions) | **83,501** |
| Distinct servers | **25,266** |
| Active, current version | 25,002 |
| Deprecated | 264 |
| Exposing a remote endpoint (probeable) | **13,901** |
| Package-only (not probeable) | 11,101 |

The registry returns every historical revision, so the row count overstates the
ecosystem by **3.3x**. Quoting 83,501 as "servers" would be wrong; published
counts differ by roughly this factor, which is why `census()` reports both.

For context, a published study measured 18,966 active servers on 2026-07-28.
This reading finds 25,266 distinct servers a month later.

## Runtime probe — 13,901 servers

| Result | Count | Share |
|---|---|---|
| `tools_listed` (full definitions captured) | 7,677 | 55.2% |
| `auth_required` (alive, gated) | 3,300 | 23.7% |
| `http_error` | 1,671 | 12.0% |
| `dead` (DNS / refused / timeout) | 960 | 6.9% |
| `mcp_alive` (handshook, would not list tools) | 293 | 2.1% |

**Reachable: 11,270 (81.1%). Unreachable: 2,631 (18.9%).**

`auth_required` is counted as ALIVE. Folding it into "dead" would have reported
42% mortality instead of 19% — the same error the x402 liveness survey had to
correct twice.

Top failure reasons: HTTP 401 (3,267), URLError (882), HTTP 404 (598), no
initialize result (358), HTTP 402 (125).

## What was captured

- **127,403 tool definitions** — name, description AND input schema.
- Median 7 tools per server; max 1,076.
- 7,452 distinct tool fingerprints.

This is the part with no substitute. The registry records metadata; it does not
record what a server's tools *said* on a given day. A rug pull — ship a benign
tool, get adopted, then rewrite the description the model obeys — moves the
digest without moving the version. There is now a baseline to compare against.

## Finding already visible in reading #1: cloned backends

Fingerprinting surfaced something a snapshot of names cannot:

- **65 fingerprints are shared by more than one server**, covering **244 servers**.
- The largest: **53 servers under `io.github.mcp-dir/*`** — published as 99pay,
  agora, asa, atacadao and 49 other distinct brands — all serving the byte-identical
  25 openfinance tools (`openfinance_list_accounts`,
  `openfinance_list_transactions`, ...). One backend, 53 storefronts, in a
  registry where each looks like an independent project.
- 24 servers share an `echo` / `add` / `server_time` template.
- 13 more are one Brazilian city-guide backend under 13 neighbourhood brands.
- **47 servers list ZERO tools** — they complete the handshake and expose nothing.

None of this is visible in registry metadata. It required calling every server
and hashing what came back.

## Bug found and fixed during the run

Streamable-HTTP servers hold the SSE stream open after answering, so a plain
`read()` kept accumulating keepalive frames until the byte cap. This pinned
~3.5 cores and slowed the back half of the run roughly 10x. `_read_sse` now
stops at the first complete JSON-RPC message. Verified against a live server:
identical digest, 2.7s. Three regression tests added.

Also noted, not yet fixed: 128 servers returned `tools/list UnicodeDecodeError`.
Their tool definitions were not captured. Worth handling before reading #2.

## What reading #2 gives that this one cannot

Nothing here is a product yet — it is one frame. The second reading produces the
first differences: which of the 11,270 reachable servers went dark, which of the
7,677 changed their tool definitions, and which changed them **without a version
bump**. That last set is the rug-pull candidate list, and it cannot be computed
by anyone who did not take reading #1.
