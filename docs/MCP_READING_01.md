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

## Finding, after verification — CORRECTED

The first pass reported "53 servers under `io.github.mcp-dir/*` published as
distinct brands." **That was wrong and is retracted.** Checking the endpoints
showed one publisher, one host (`api.mcp.ai`), one path per merchant
(`/p_99pay`, `/p_agora`). That is a directory doing what a directory does. The
same applies to 13 `sampa.br` neighbourhood guides under one owner.

A raw duplicate-fingerprint count cannot tell aggregation from deception.
`clone_groups()` now splits them by publisher namespace:

| | Fingerprints | Meaning |
|---|---|---|
| One owner | 25 | Normal catalog aggregation |
| **Unrelated owners** | **40** | Cannot be explained by aggregation |

**112 servers sit in a fingerprint shared with an unrelated publisher.**

### The real finding: the description is not what the server serves

**24 servers, 21 unrelated publishers, 24 different hosts, all serving exactly
`echo` / `add` / `server_time`.** All 24 carry a substantive description. A sample
of what they claim, against what they actually expose:

| Registry description | Tools actually served |
|---|---|
| "Solana pre-trade safety for agents: rug check, honeypot sell-sim, drainer scan" | echo, add, server_time |
| "Pre-trade safety verdicts for agents: token rug/honeypot, calldata guard" | echo, add, server_time |
| "SAM.gov federal contracts: search, details, AI bid analysis. 33k+ live opportunities" | echo, add, server_time |
| "AI image generation from text prompts via Gemini. x402 micropayment." | echo, add, server_time |
| "Persistent, agent-owned memory with encrypted storage" | echo, add, server_time |
| "Live KTA rates, market data, payment rails, AML/VAT compliance" | echo, add, server_time |

The registry description is unverified publisher copy. The tool list is what the
agent actually receives. Two of the six above advertise **safety** functions --
an agent told a "drainer scan" tool exists may proceed as though it ran one.

Whether this is abandoned scaffolding or deliberate listing-farming is not
established and is not claimed here. What is established is the mismatch, and it
is only visible by calling every server. `describes_more_than_it_serves()`
implements the check.

Also found: **47 servers complete the handshake and expose zero tools.**

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
