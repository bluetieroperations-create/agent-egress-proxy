# Blackwall — listing surfaces & canonical copy

Where every public description of Blackwall lives, what it currently says, and
what it should say. **Verified live 2026-08-25** by querying each registry.

Most of these live in external repos/accounts outside this session's scope —
the copy here is ready to paste; you execute the publish.

---

## The identity split (why the copy matters)

There are **two** Blackwall MCP servers. They are complementary, not duplicates,
and every description must make the difference unmissable — otherwise a user
installs the wrong one and hits an auth wall or the wrong scope.

| | generalized | x402 payments |
|---|---|---|
| MCP registry name | `com.blackwalltier/blackwall` | `com.blackwalltier/blackwall-x402-guardrail` |
| distribution | npm `blackwall-mcp` (stdio) | hosted `https://mcp.blackwalltier.com` (streamable-http) |
| auth | **`BLACKWALL_API_KEY` required** | **keyless** |
| backend | `blackwalltier.com/api/v1/forecast` | the engine directly (`blackwall-free.onrender.com/v1/forecast-payment`) |
| scope | any irreversible action (money, SQL, delete, email) | pre-signature x402 payment verdict |
| source repo | `bluetieroperations-create/blackwall-mcp` | `bluetieroperations-create/blackwall-x402-mcp` |

**The two discriminators to lead with in every description: auth and scope.**

### Known naming inconsistency (deliberate, do not "fix")
The x402 server carries three names: registry `blackwall-x402-guardrail`,
runtime `serverInfo.name` `blackwall-x402-mcp`, Cloudflare Worker `blackwall-mcp`.
Only the first two are user-visible and neither collides with the generalized
server, so the ambiguity that mattered is already gone. Renaming the registry
entry would **publish a second listing** (the name *is* the identity there) and
orphan the version history; renaming the Worker would move the
`mcp.blackwalltier.com` custom-domain binding. Both cost more than they buy.

---

## Canonical copy

Registry descriptions are capped at **100 characters**
(`ServerDetail.properties.description.maxLength`), so these are counted.

**x402 server** — replaces "Pre-signature x402 payment verdict (GO/HOLD/STOP) for AI agents, with a verifiable signed receipt."
> Keyless hosted MCP: pre-signature x402 payment verdict (GO/HOLD/STOP) with a signed receipt.

*(92 chars — adds "keyless" and "hosted", the two facts that separate it from the npm package.)*

**generalized server** — replaces "Pre-action risk gate: AI agents call before any irreversible action (money, SQL, delete)."
> API-key pre-action risk gate: any irreversible agent action (money, SQL, delete, email).

*(88 chars — leads with the auth requirement so nobody installs it expecting keyless.)*

**Long-form blurb** (npm, directories, awesome-lists — no length cap):
> **Blackwall** — pre-signature payment-risk oracle for AI agents. Returns
> **GO / HOLD / STOP** before an agent signs an x402 payment, from behavioral
> counterparty reputation, price-anomaly, and OFAC sanctions screening, with an
> independently-verifiable Ed25519 receipt. Verdict-only, never custody. Live on Base.
> Hosted MCP (keyless): `https://mcp.blackwalltier.com` · tool `forecast_payment`
> Engine: `https://blackwall-free.onrender.com` ·
> Discovery: `https://blackwall-free.onrender.com/.well-known/x402`

**Category:** payment-risk / agent-guardrail
**Tags:** `x402` `payments` `counterparty-risk` `price-anomaly` `sanctions` `reputation` `agent-guardrail` `base` `usdc` `mcp`

---

## Surfaces

### 1. MCP registry — `com.blackwalltier/blackwall-x402-guardrail` (v0.1.1, latest)
Two things are stale:
- description doesn't say keyless/hosted → use the x402 copy above
- `repository.url` still points at `blackwall-mcp-remote`, renamed 2026-08-25 →
  `https://github.com/bluetieroperations-create/blackwall-x402-mcp`

Edit `server.json` in the x402 repo, bump `version`, republish with `mcp-publisher`.

### 2. MCP registry — `com.blackwalltier/blackwall` (v1.4.1, latest)
Description doesn't state that a key is required, though the entry does declare
`BLACKWALL_API_KEY` as `isRequired: true` — a reader skimming descriptions misses it.
Use the generalized copy above.

### 3. npm `blackwall-mcp` (1.4.1)
`description` in `package.json`; also `repository` and `homepage` are **null/absent**,
so the npm page links nowhere. Set all three.

### 4. Smithery — `bluetier-operations/blackwall`
The generalized product. One listing only; do **not** publish the x402 server as a
second Smithery entry under a near-identical name.

### 5. awesome-x402
- `xpaysh/awesome-x402` — already listed (PR #667).
- `Merit-Systems/awesome-x402` — the second active index; verify then submit.

Both should point at the **hosted keyless** endpoint, which is what the listing
promises and what now actually answers.

### 6. x402 discovery / Bazaar crawlers
Nothing to submit — `/.well-known/x402` is crawled. Note the descriptor moved to
`blackwall-free.onrender.com`; the old `agent-egress-proxy.onrender.com` path
returns **404** (verified 2026-08-25). Anything still advertising the old URL is broken.

### 7. Glama.ai / mcp.so / PulseMCP
Directory submissions. Reuse the long-form blurb. The "blocked until MCP-over-HTTP"
note that used to live here is **obsolete** — a hosted streamable-http endpoint has
been live since the Worker shipped, so the HTTP-only registries are eligible now.

---

## Honest notes
- **I can prep, not submit.** These live in external repos/accounts.
- **No adoption claims** in any submission — capability only.
- **Re-verify formats** — registry schemas change; the 100-char cap above was read
  from the live schema on 2026-08-25.
