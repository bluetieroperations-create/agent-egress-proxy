# How the MCP reading plugs into Blackwall

Not a hypothetical. The two datasets overlap on **98 hosts**, measured by joining
`data/mcp_snapshots/2026-08-27.json.gz` against `data/directory.json`.

## The join

An MCP server that charges via x402 is two things at once:

- a **tool** the agent calls (what the MCP reading measures), and
- a **payee** Blackwall scores before the agent signs (what `decide_payment` does).

Same entity, two datasets that until now never touched.

- **729** MCP servers advertise payment (x402 / USDC / pay-per-call) — 5% of the corpus.
- **125** returned a live HTTP 402 challenge during the probe.
- **98 hosts appear in BOTH** the MCP registry and Blackwall's x402 payee directory,
  each already carrying an on-chain payee address and settlement history.
- **84** more match at the organisation level (same eTLD+1, different subdomain).

## Three signals it adds to a verdict

### 1. Capability mismatch — the payee does not do what it sells

Five payment-taking servers serve only `echo`/`add`/`server_time` while advertising
real products:

| Advertised | Actually serves |
|---|---|
| "Escrow protection for agent payments — USDC held in smart contract" | echo, add, server_time |
| "Solana pre-trade safety: rug check, honeypot sell-sim, drainer scan" | echo, add, server_time |
| "Pre-trade safety verdicts for agents, calldata guard" | echo, add, server_time |
| "AI image generation via Gemini. x402 micropayment." | echo, add, server_time |
| "Private, permanent encrypted storage for AI agents. Paid per call." | echo, add, server_time |

Today Blackwall scores such a payee on price and reputation and sees nothing wrong:
the address may be clean, the price in range, the settlements real. It has no way to
know the endpoint does not implement what the agent is paying for. The first one is
the sharpest case — an agent paying for **escrow** receives an echo tool.

### 2. Silent tool drift — the endpoint changed underneath the agent

From reading #2 onward, `probe.drift` yields payees whose `tools_digest` moved while
their published version did not. At payment time that is a direct signal: *this
endpoint's capabilities changed since we last looked, without announcing it.*

This is the rug pull Invariant Labs described, evaluated at the moment money moves
rather than at install time.

### 3. Liveness — paying a host that was dead last month

2,631 registry servers (18.9%) do not answer. Where one is also a known payee, an
imminent payment to it is worth a second look.

## How it folds in, mechanically

Exactly the `blockscout.py` pattern, which exists for precisely this shape of signal:

- An `McpTrustSource` with a `.signal(host)` lookup, built at startup from the
  committed snapshot — **never from the request**, so it cannot be spoofed by the
  payload, and no live call is made on the hot path (no query leak, per `rpc_node.py`).
- Injected into `forecast` as `mcp_source=`, folded via a `signals.mcp_trust` key.
- **HOLD-only, never STOP.** Blackwall's boundary is that only sanctions and payload
  mismatch reach STOP. A capability mismatch is a strong reason to pause, not proof
  of harm, and intent is not established for any publisher.
- **Fail-open.** Missing or stale snapshot -> unknown -> no effect.
- **Never clears.** It can only add caution, matching `enrichment`, which is added
  structurally to the `go` conditions so it cannot reach the STOP path.
- Behind `BLACKWALL_MCP_TRUST=1`.

## Why this changes the strategic picture

Standing alone the MCP reading is weak: the probe is ~400 lines and Snyk (who now
own Invariant Labs) could rebuild it in a week, or start their own clock and reach
parity in months having paid nothing.

Inside Blackwall the calculus inverts. To match the capability-mismatch gate a
competitor needs the probe **and** the payee directory **and** the verdict engine
**and** the accumulated history. The data stops being a product with a 400-line moat
and becomes a signal in a product that already has switching costs.

## Unplanned bonus: the overlap list is competitive intelligence

Several of the 98 are selling what Blackwall sells, and the reading captured their
exact tool surfaces:

| Host | Tools exposed |
|---|---|
| `trust-score.api.klymax402.com` | `trust_score_evaluate`, `trust_score_batch_compare` |
| `api.robinx.io` | `robinx_verdict`, `robinx_deployer`, `robinx_token` |
| `lionx402.com` | `lion_wallet_screen`, `lion_multi_sanctions_bundle` |
| `api.anchor-x402.com` | `attest_decision`, `grade_target`, `decode_calldata` |
| `api.solenrich.com` | `due_diligence`, `whale_watch`, `wallet_graph` |
| `data.greeneris.io` | `screen_sanctions`, `check_insolvency_fr` |

Each also carries a payee address and settlement history in `directory.json` — so
their tool surface AND their revenue footprint are both visible. `COMPETITIVE.md`
was written from search results; this is measured.

## Not yet built

This document is the design, not an implementation. Nothing above is wired into
`decide_payment` yet, and the drift signal needs reading #2 (scheduled 2026-09-27)
before it produces anything at all.
