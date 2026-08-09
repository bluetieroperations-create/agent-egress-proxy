# Blackwall — registry submission kit

Ready-to-execute submission package. **You execute these** — each lives in an
external repo/account outside this session's scope (I can't submit for you), and
registry requirements shift, so **verify each target's current docs before
submitting.** Honest eligibility per target below.

## Canonical listing blurb (reuse everywhere)
> **Blackwall** — Pre-signature payment-risk oracle for AI agents. Returns
> **GO / HOLD / STOP** before an agent signs an x402 payment, from behavioral
> counterparty reputation, price-anomaly (per-class + peer-group), and OFAC
> sanctions screening. Verdict-only, never custody. Live on Base. Also an MCP
> server (`forecast_payment`).
> Endpoint: https://agent-egress-proxy.onrender.com ·
> Discovery: https://agent-egress-proxy.onrender.com/.well-known/x402

**Category:** payment-risk / agent-guardrail
**Tags:** `x402` `payments` `counterparty-risk` `price-anomaly` `sanctions` `reputation` `agent-guardrail` `base` `usdc` `mcp`

---

## Targets & eligibility

### ✅ Ready now (low effort)
1. **awesome-x402 (`xpaysh/awesome-x402`)** — **already listed** (PR #667, "Live on Base"). Done.
2. **awesome-x402 (`Merit-Systems/awesome-x402`)** — the second active index. Submit the same entry as a PR (your fork → their repo). *Verify it isn't already there.*
3. **x402 discovery / Bazaar crawlers** — Blackwall already serves a `/.well-known/x402` descriptor, which x402 discovery indexes crawl. Nothing to submit; just confirm the descriptor is well-formed (it is). Announce the endpoint in the x402 community so crawlers/humans find it.

### ⚙️ Ready with packaging (medium effort)
4. **Glama.ai MCP directory** — indexes public GitHub MCP servers. Needs: the repo public + a clear MCP section in the README naming the server entry (`python mcp_server.py`, tool `forecast_payment`) and its stdio transport. Then submit the repo URL on glama.ai. *Packaging: add an MCP usage block to the README if not present.*
5. **Smithery.ai** — ⚠️ **ALREADY PUBLISHED** as `bluetier-operations/blackwall`
   (the **blackwalltier.com** product; one tool, "pre-action risk check"; 99.86%
   uptime). **Do NOT double-publish** the `agent-egress-proxy` `forecast_payment`
   server as a second listing — that fragments your presence. Decide first (see
   the reconciliation note below): if `agent-egress-proxy` is the payment *engine
   behind* blackwalltier, there should be ONE Smithery listing, not two. The
   `smithery.yaml` in this repo is only for a deliberately-separate payment-only
   listing — otherwise ignore it and update the existing Smithery entry instead.
6. **mcp.so / PulseMCP / other MCP directories** — directory submissions (form or PR). Reuse the blurb + repo URL + the `forecast_payment` tool description.

### ⛔ Blocked until MCP-over-HTTP (roadmap)
7. **Registries that require a *remote/hosted* MCP endpoint** — Blackwall's MCP is **stdio** (local). Any registry that wants a reachable HTTP MCP URL needs the **MCP-over-HTTP transport** (ROADMAP item) first. Don't submit to these yet; they'll bounce.

### 📣 Not registries, but the real distribution
8. **Farcaster / x402 Discord-Telegram / Coinbase x402 + Base ecosystem channels** — post the launch thread (see `docs/LAUNCH.md`), builder-to-builder. This is where actual first users come from — higher ROI than any directory.

---

## Execution order (fastest signal first)
1. Post the **launch thread** (`docs/LAUNCH.md`) on X + Farcaster + x402 community. *(No repo work; highest ROI.)*
2. Submit to **`Merit-Systems/awesome-x402`** (quick PR, reuse the blurb).
3. Submit the repo to **Glama** (add the README MCP block first if needed).
4. Author **`smithery.yaml`** and connect **Smithery**.
5. Directory submissions (mcp.so / PulseMCP).
6. Defer the HTTP-only MCP registries until MCP-over-HTTP ships.

## ⚠️ Reconciliation note (read before submitting anything MCP)
There are **two Blackwall MCP surfaces**, and they must not become two competing
listings:
- **`blackwalltier.com`** — already on Smithery. A **generalized pre-action risk
  check** (any high-stakes action: email, payment, SQL, delete, post, API). Broad.
- **`agent-egress-proxy`** — this repo's **`forecast_payment`**: the deep x402
  **payment** verdict engine (per-class + peer-group price, OFAC, x402 billing).
  Narrow but deep.

The clean model is **complementary, not duplicate**: blackwalltier is the broad
action guardrail; `agent-egress-proxy` is the payment *engine* its "making a
payment" check should call. Pin that relationship (the recurring two-backend
reconciliation) **before** publishing a second listing anywhere — otherwise you
split your Smithery/Glama presence across two half-overlapping entries.

## Honest notes
- **I can prep, not submit.** All of the above happen in external repos/accounts I can't reach from this session. The copy + eligibility here is the package; you (or a session scoped to those repos) execute it.
- **No adoption claims** in any submission — capability only.
- **Re-verify requirements** — MCP registries change formats often; treat the packaging notes as direction, confirm against each registry's live docs.
- **Want me to make the repo registry-ready?** I *can* add a README MCP-usage block and a `smithery.yaml` draft here in `agent-egress-proxy` (in-scope) so Glama/Smithery submission is one step for you — just say so and I'll draft them against the current specs (flagging anything I can't verify).
