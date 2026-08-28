# Technical findings from the corpus — things we can actually use

Measured, not speculated. Sources: `data/mcp_snapshots/2026-08-27.json.gz`
(13,901 probed servers) and `data/snapshots/2026-08-25.json` (195 x402 hosts).

## 1. The x402 v2 challenge format in this repo is WRONG — now fixed

`directory_liveness.py` documented that nothing reads `WWW-Authenticate`, and
guessed the format as:

    X402 requirements="<base64 json>"

Probed all 195 surveyed hosts. **11 serve a challenge, and none match that
guess.** The real format is:

    WWW-Authenticate: Payment id="...", realm="...", method="evm",
                      intent="charge", request="<base64url JSON>",
                      description="...", expires="..."

Three concrete breaks in the assumed parser:

| Assumed | Actual |
|---|---|
| scheme `X402` | scheme **`Payment`** |
| parameter `requirements=` | parameter **`request=`** |
| standard base64 | **base64url, unpadded** — `b64decode` raises on `-`/`_` |

**Measured shape** across the 11 live challenges:

- methods: `tempo` (5), `evm` (4), `asterpay` (1), `usdc` (1)
- payload keys: `amount` 11/11, `currency` 11/11, `recipient` 11/11,
  `methodDetails` 10/11, `description` 4, `asset` 1, `settlement` 1
- chains seen: **8453 (Base) and 4217 (Tempo)** — assuming Base is wrong
- `decimals` appears only inside `methodDetails`, and only sometimes

Two traps found by reading the real payloads:

- **One host puts the SYMBOL in `currency`** (`"USDC"`) and the address in
  `asset`. Reading `currency` blindly puts the string "USDC" in the asset slot
  and every address comparison silently fails.
- **`decimals` is often absent.** Defaulting to 6 mis-scales the amount by
  10^n — the same class of error as the PFAS units bug.

`x402_challenge.py` implements this, tested against the real captured headers
(`test_x402_challenge.py`, 17 tests). `accepts_from_response()` is purely
additive: body options first, header option appended, so wiring it into
`discovery_crawl`, `readiness`, `clients/x402_pay.py` and the OpenClaw hook can
only ADD payable endpoints, never change existing behaviour.

## 2. 29.6% of the MCP ecosystem is TWO hosts

| Backend | Registry entries | Distinct hosts | Distinct publishers | Distinct tool fingerprints |
|---|---|---|---|---|
| `gateway.pipeworx.io` | **1,266** | 1 | 1 | 1,192 |
| `api.mcp.ai` | **1,096** | 1 | 1 | 987 |

**2,362 of 7,968 identified servers (29.6%) resolve to two operators.** Each
entry serves genuinely different tools — these are gateways fronting many
upstreams, not clones — which makes it concentration rather than spam.

This is the strong form of the finding I retracted earlier. `io.github.mcp-dir`
is not 53 fake brands; it is **1,096 registry entries on one host**. Compromise
either gateway and roughly 1,200 agent-facing tool surfaces move at once.

## 3. Protocol version distribution (7,969 servers that handshook)

| Version | Servers |
|---|---|
| `2025-06-18` | 6,912 (87%) |
| `2024-11-05` | 589 |
| `2025-03-26` | 355 |
| `2025-11-25` | 88 |
| `2026-07-28` | 22 |

A client pinning only the newest version fails against ~13% of the live
ecosystem. `2024-11-05` is still the second most common.

## 4. Failure taxonomy (what actually breaks, n=13,901)

| Detail | Count |
|---|---|
| HTTP 401 | 3,267 |
| URLError (DNS/refused) | 882 |
| HTTP 404 | 598 |
| no initialize result | 358 |
| tools/list UnicodeDecodeError | 128 (our bug, fixed) |
| **HTTP 402** | **125** |
| HTTP 405 | 75 |

**401 dominates.** Any survey treating auth-gated servers as dead reports 42%
mortality instead of 19%. The 125 402s are x402-charging MCP servers — the
direct overlap between the two datasets.

## 5. Two more, already documented elsewhere

- **98 hosts** appear in both the MCP registry and `data/directory.json` —
  the same counterparties from two sides (`MCP_INTO_BLACKWALL.md`).
- **Zero tool poisoning** across 127,403 definitions, giving reading #2 a clean
  floor to diff against (`POISON_SCAN_BASELINE.md`).

## What to do with this

1. **Wire `x402_challenge.accepts_from_response` into the body-only consumers.**
   It is additive and tested; it makes header-style endpoints payable and
   scoreable for the first time.
2. **Do not assume Base.** Chain 4217 is live and being paid.
3. **Do not assume 6 decimals.** Carry None and refuse rather than guess.
4. **Treat gateway concentration as a real risk input** — a payee behind
   `gateway.pipeworx.io` or `api.mcp.ai` shares fate with ~1,200 others.
