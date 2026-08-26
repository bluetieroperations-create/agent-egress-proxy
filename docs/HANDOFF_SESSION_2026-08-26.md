# Session handoff — 2026-08-26

Everything a fresh session needs to pick up. Written at the end of a long session;
`main` is clean, everything below is merged and deployed unless marked otherwise.

---

## 1. THE ONE PENDING ACTION

**Send the payai outreach email.** It is written, fact-checked, and blocked only on
a tool the previous session could not reach.

- Text: `scratchpad/payai-outreach.txt` (also reproduced in §6 below)
- To: `info@payai.network` — but **Discord is the better channel**:
  `discord.gg/eWJRwMpebQ`. The message asks a technical question about `/verify`;
  a general-enquiries inbox is the least likely place to find someone with an
  opinion on it.
- Blocked because: Gmail reported `enabledInChat: false` all session. See §7.

Nothing else is waiting on anyone.

---

## 2. State of the world

`main` @ `a8d3fb1`. **Zero open PRs** (was five). Full suite **1609 tests OK**,
redteam **25 caught / 2 known gaps / 0 false positives / 0 misses**.

Live and verified in production:

| surface | status |
|---|---|
| `blackwall-free.onrender.com/healthz` | 200 |
| `/v1/price-index` | 200 — serving regenerated values |
| `/v1/screen-payer` | 200 — real payer graph loaded |
| `mcp.blackwalltier.com` | 200 — MCP server, renamed `blackwall-x402-mcp` |

**Render has `autoDeploy: false`.** Merging does NOT deploy. After any merge that
should reach production, trigger *Manual Deploy → Deploy latest commit* on the
`blackwall-free` service, then re-verify. This cost the last session ten minutes of
confused polling.

---

## 3. What shipped this session

**Identity split closed.** The remote MCP server renamed to `blackwall-x402-mcp`
(runtime + GitHub repo; Cloudflare Worker name deliberately unchanged — it carries
the `mcp.blackwalltier.com` custom-domain binding). All five description surfaces
now state auth and scope: MCP registry x2 (`0.1.2`, `1.4.2`), npm `blackwall-mcp`
1.4.2 (+ `repository` and `homepage`, previously null), Smithery, awesome-x402.

**MCP registry signing key rotated.** Old key was lost. New public key is in the
`blackwalltier.com` DNS TXT record AND `/.well-known/mcp-registry-auth` (a Next.js
API route at `Documents/blackwall/app/api/well-known/mcp-registry-auth/route.ts`,
NOT a static file). Private key at `C:\Users\Owner\.mcp-registry-key` on the
operator's machine — **only copy**; it should be in a password manager.

**`x402_challenge.py`** — one parser for the 402 challenge, both carriers (JSON body
and `WWW-Authenticate: X402`). Wired into `directory_liveness`, `discovery_crawl`,
`clients/x402_pay`. Audit found and fixed a HIGH: unbounded read of a hostile 402
body (43.8 MB buffered, 172 MB peak) now capped at 1 MiB.

**`/v1/screen-payer`** — buyer-side scoring over HTTP. Previously MCP-only, which
made it unreachable by its own buyer (facilitators integrate over HTTP). Delegates
to the same `graph_source.screen()`; a test asserts byte-equality between transports.

**`/v1/price-index`** — the per-category settled-price index, published.

**CORS** — `main` had none. The `check.blackwalltier.com` demo page was getting
correct responses that the browser then blocked.

**Sybil fix** — the ERC-20 zero address was counted as a distinct payer. A transfer
from `0x0` is a mint; crediting the payee with breadth is unearned trust.

**PR triage** — #13, #10, #5, #14 merged; #4 and #2 closed with reasoning recorded
on the PRs, branches kept.

---

## 4. Read these before proposing data work

- **`docs/HANDOFF_ZEROCUSTOMER_DATA.md`** — names the strategy, inventories the
  corpus, and **rules out** endpoint-mortality and cohort analysis. The corpus is a
  recent backfill *window* per payee, not complete history: median visible span by
  settlement count runs 28/29/24/**18** days as volume rises, when complete history
  would make it grow. Do not re-derive this.
- **`docs/PAYABILITY.md`** — 44.1% of x402 endpoints (86/195) advertise a price no
  client can read.
- **`docs/REGISTRIES.md`** — every listing surface and the canonical copy, verified
  against each live registry.

**Moat framing, corrected:** the raw chain data is NOT defensible — anyone can read
Base with a keyless Blockscout call. The join (advertised × settled × still-
resolvable), the accumulated outcome labels, and the calibration discipline are.

---

## 5. Open threads, in the order worth doing

1. **Send payai** (§1), then `facilitator.xpay.sh` — they already merged Blackwall
   into `awesome-x402`, so there is a prior touch. Coinbase CDP last, once there is
   a reply to quote. **Skip `facilitator.x402.rs` and `x402.org/facilitator` —
   both testnet-only**, and our corpus is Base mainnet.
2. **The 86 `opaque_402` endpoints.** Is there a third carrier nobody implemented,
   or are they genuinely unpayable? Nobody has inspected more than a sample by hand.
3. **PR #4 leftovers** — `/stats` + `ledger.usage_stats` / `counterparty_flow` /
   `payer_flow` on `claude/deploy-receipts-cors-to-main`. Genuinely unmerged, needs
   its own review. `main` already has a `/stats` route implemented differently.
4. **Phase 2 post-quantum receipts** (ML-DSA-65 hybrid) — scoped in
   `docs/RECEIPT_SIGNING_SCOPE.md`, not built.
5. **MCP HTTP transport has no rate limit** (merged in #5). Binds 127.0.0.1 by
   default and nothing deploys it, so nothing is exposed — but it must not go on a
   public bind without a limiter.

Branches still ahead of main: `claude/x402-product-ideas-6adgah` (107 — **Traceipt,
do not merge**), `claude/deploy-receipts-cors-to-main` (22), `deploy/stats-only`
(17), and five smaller ones.

---

## 6. The payai email

Recipient, subject and body are in `scratchpad/payai-outreach.txt`. Headline:
**75.2% of the buyers paying payai-listed sellers are `established`**, vs ~20%
ecosystem-wide. Derived by sampling 1,751 resources from their own
`/discovery/resources`, joining 11 overlapping payees against our corpus, and
screening their 141 distinct buyers.

**Keep the caveats paragraph.** 6% overlap, biased toward high-volume sellers, and
we cannot confirm those payments were relayed through payai (the facilitator is not
recorded on-chain). Those caveats are what make 75% credible rather than a number
being sold.

---

## 7. Enabling Gmail and GitHub in the new session

**Gmail.** It is already authenticated at org level (`connected: true`); the
per-chat switch was off all session (`enabledInChat: false`), and connector changes
generally only take effect in a NEW session. So: enable Gmail when starting the
session, then have the assistant run `ListConnectors` — if it reports
`enabledInChat: true`, the send tools will be loaded. If it still reports false in a
brand-new session, Gmail may not attach to remote/cloud sessions at all, and the
email should be sent by hand.

**GitHub.** Scope is set by the session's environment config, currently limited to
`bluetieroperations-create/agent-egress-proxy`. To widen it, add repositories to the
GitHub App installation (github.com/settings/installations → the Claude app →
Configure → Repository access).

**GitHub cannot reach payai regardless.** Creating an issue on `PayAINetwork`'s repo
requires the app installed on THEIR org, which only they control. No permission on
our side unlocks it. Do not spend time on this.
