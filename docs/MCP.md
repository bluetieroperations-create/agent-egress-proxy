# Blackwall MCP server

Blackwall is available as a **Model Context Protocol (MCP) server** so any
MCP-capable agent can get a pre-signature payment verdict self-serve. It's a thin
stdio transport over the verdict engine — **stdlib-only Python 3.12, no pip
install** (`mcp_server.py`).

## Run it

```sh
python mcp_server.py                 # verdict-only (mock reputation)
BLACKWALL_LEDGER=/data/ledger.jsonl python mcp_server.py   # + report_outcome tool
BLACKWALL_STORE=/data/rep.db BLACKWALL_INGEST=1 python mcp_server.py  # real on-chain reputation
```

- Transport: **stdio**, JSON-RPC 2.0, MCP protocol `2024-11-05`. stdout is pure
  protocol; logs go to stderr.
- Server name: `blackwall`.

## Tools

### `forecast_payment` (always available)
Pre-signature x402 payment verdict: **GO / HOLD / STOP** for paying a counterparty,
with reputation + price-anomaly signals and a signed receipt.

Required inputs: `counterparty`, `amount` (decimal string), `asset`, `chain`.
Optional: `payer` (binds settlement), `resource`, `resource_class` (peer-group),
`agent_id`, `context` (`{quoted_price_history, expected_recipient}`).

Returns `structuredContent` = the full verdict (`verdict`, `score`, `reasons`,
`signals`, `receipt_id`, `report_token`).

### `report_outcome` (only when a ledger is configured)
Report what a prior verdict's payment actually did (settled / delivered /
underdelivered / disputed / refunded / abandoned), keyed by `receipt_id` and
authorized by the `report_token` from that forecast — feeds Blackwall's
reputation.

## Config (env vars)

| Env | Effect |
|---|---|
| `BLACKWALL_LEDGER` | ledger path; enables `report_outcome` |
| `BLACKWALL_STORE` | SQLite reputation store; uses real on-chain reputation |
| `BLACKWALL_INGEST` | with `BLACKWALL_STORE`, self-populate from chain on first sight |

---

## Registry submission notes

### Glama.ai
Glama indexes public GitHub MCP servers. To submit: ensure this repo is public and
that the MCP server is discoverable from the repo (this file + `mcp_server.py`).
Paste the **README block below** into the repo's top-level README (the main README
currently documents the sibling egress-proxy tool, so an explicit MCP pointer helps
Glama's indexer find the server), then submit the repo URL on glama.ai. *Verify
Glama's current submission flow.*

### Smithery.ai
See `smithery.yaml` at the repo root (classic stdio `startCommand` draft).
**Verify against Smithery's current spec** — it has moved between stdio
`commandFunction` and container runtimes; the repo's `Dockerfile` supports the
container path if that's what Smithery currently wants. Then connect the repo on
smithery.ai.

### Blocked
Registries that require a **remote/hosted HTTP MCP endpoint** need the
MCP-over-HTTP transport (ROADMAP) — Blackwall's MCP is stdio today. Don't submit
to those yet.

---

## README block (paste into the top-level README for Glama)

```markdown
## Blackwall MCP server

Pre-signature x402 payment-risk verdict as an MCP tool. Run: `python mcp_server.py`
(stdio, stdlib-only). Tool **`forecast_payment`** returns GO / HOLD / STOP for
paying a counterparty — behavioral reputation + price-anomaly (per-class +
peer-group) + OFAC screening + a signed receipt. Optional **`report_outcome`**
(with `BLACKWALL_LEDGER`) feeds reputation. See `docs/MCP.md`. Live HTTP endpoint
+ discovery: https://blackwall-free.onrender.com/.well-known/x402
```
