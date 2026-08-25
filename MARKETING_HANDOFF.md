# Blackwall × x402 — Marketing & PR Handoff

**Purpose:** kickoff context for a new session dedicated to **marketing, PR, and
distribution** for Blackwall. This doc is self-contained — a fresh session has
none of the build history, so everything needed to start is here. Snapshot date:
**2026-06-30.**

> **One job for the new session:** get *real agents and developers* calling the
> live endpoint. The product is built, audited, and proven on mainnet. The
> contest now is **distribution**, and (see §6) it is currently **unclaimed**.

---

## 0. CORRECTIONS — read first (post-audit 2026-06-30)

A fact-check against the live service caught real overclaims in an earlier draft
of this handoff. These are the **source-of-truth corrections** — public copy must
respect them:

- ✅ **Real mainnet x402 settlement is REAL and provable on-chain.** Block
  **48022757** on Base mainnet: **0.05 USDC** from the test signer
  `0xc194Bf…EADb` → payTo `0x3ec5…004e1`, tx
  **`0x9ddec827b762303c6f1f351530239f52901c86bb19df41ea6a02e8d276be9fd7`**. This
  is the durable proof the service settles real payments on mainnet. Cite the tx.
- ⚠️ **The demo counterparty's reputation is SEED/DEMO data, not real ingested
  history.** The "34 settlements / 0% disputes / rep 0.972" figure is for
  `0x1111…1111`, a synthetic test address. **Do NOT claim "real on-chain
  settlement history" using those numbers** — a builder who checks BaseScan finds
  nothing, and that's a worse credibility hit than no demo. The behavioral-reputation
  engine is a real *capability*; it has **not** yet been demonstrated on real
  ingested Base data. Frame it as capability, not track record, until real
  ingestion is shown.
- ✅ **Price-anomaly STOP is real and reproducible by ANYONE.** Lead with this. The
  multiplier is **amount-dependent**: vs the demo counterparty's ~$0.50 median, a
  $50 quote reads as 100×, a $5 quote as 10×; the engine trips at ≥~8×. Say "caught
  a price gouge → STOP" and, if citing a multiple, tie it to the quoted amount.
- ✅ **OFAC sanctions screening is now ENABLED on the deploy** (93 addresses from
  the published OFAC list, baked into the image; descriptor advertises
  `screening: ["sanctions-ofac"]`; a sanctioned address returns STOP). It is a
  point-in-time snapshot — refresh with `python sanctions.py sanctions.txt` +
  redeploy. Verify `screening` in the live descriptor before claiming it.

**Honest launch posture:** lead on (1) the **reproducible price-anomaly STOP**
anyone can trigger, (2) the **real on-chain settlement tx** as proof it works on
mainnet, (3) **live OFAC screening**. Do NOT lean on the seed-data reputation
numbers. The canonical, verified facts come from the **`agent-egress-proxy`
deployment** (this is the source of truth, not any older positioning in
`blackwall-mcp-pub`).

---

## 1. What Blackwall is (use this language)

**Blackwall is a pre-signature payment-risk oracle for AI agents.** Before an
agent signs an x402 payment, Blackwall returns a **GO / HOLD / STOP** verdict —
based on:

- **Behavioral counterparty reputation** — Bayesian settlement/dispute history
  from *actual chain-confirmed outcomes* (not self-declared attestations).
- **Price-anomaly detection** — quoted amount vs the counterparty's own median,
  hardened against wash-trading (per-distinct-payer median).
- **OFAC sanctions screening** — folded into the same single verdict.

**Verdict-only, never custody.** Blackwall never touches funds; it returns a
decision + a signed receipt. It is itself an x402 resource (pay-per-forecast,
value-aligned pricing) and an **MCP server**.

**One-liner:** *"A circuit breaker for AI-agent payments — GO / HOLD / STOP
before your agent signs."*

**Elevator (30s):** AI agents are starting to pay for things autonomously over
x402 (HTTP 402 + stablecoins). They sign payments in milliseconds with no human
in the loop. Blackwall is the guardrail that sits in front of that signature: it
scores the counterparty's real payment history, flags price gouging, screens
sanctions, and returns GO / HOLD / STOP — so an agent doesn't blindly sign a
100×-overpriced or malicious payment. Live on Base mainnet today.

---

## 2. Status — what's true and provable (these are your proof points)

- ✅ **Live on Base mainnet.** Deployed, monitored (UptimeRobot), settles real
  x402 payments.
- ✅ **Real mainnet settlement driven end-to-end** (2026-06-30): a real USDC
  payment went 402 → EIP-3009 sign → facilitator verify+settle → verdict.
  **Provable on-chain** — tx `0x9ddec8…` (see §0). *This is the durable proof.*
- ✅ **Caught a price gouge → STOP** on that live call, and **anyone can
  reproduce it** by POSTing an overpriced amount. *(Multiplier is
  amount-dependent — see §0. The reputation numbers behind the demo counterparty
  are SEED data, not real history — do not cite them as a track record.)*
- ✅ **Listed on awesome-x402** (the ecosystem index) as "Live on Base."
- ✅ **315 tests, adversarially audited, stdlib-only** (no dependency supply-chain
  surface — a credibility point with security-minded devs).
- ✅ **MCP server** (`forecast_payment` tool) for agent frameworks.

**Honest gaps (don't overclaim):** no traffic yet (you're fixing that); MCP is
stdio-only (no remote HTTP transport yet); persistence is in-memory until there's
data worth a paid disk; peer-group price cross-check not built. Don't claim
"trusted by X agents" — there are none yet. Lead with the *capability proof*
(the live gouge catch), not adoption numbers.

---

## 3. Live assets / facts (verify before publishing)

| Thing | Value |
|---|---|
| Live endpoint | `https://blackwall-free.onrender.com` |
| Verdict API | `POST /v1/forecast-payment` |
| Discovery descriptor | `https://blackwall-free.onrender.com/.well-known/x402` |
| Health | `GET /healthz` |
| MCP tool | `forecast_payment` (stdio) |
| Network / asset | **Base mainnet**, USDC (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`) |
| Pricing | Value-aligned — **free under $10 at-risk**, paid on real stakes |
| Ecosystem listing | `xpaysh/awesome-x402` (PR #667, via fork `bluetieroperations-create/awesome-x402`) |

> Re-fetch the discovery descriptor before quoting it — it's the canonical,
> always-current product summary.

---

## 4. Ready-made copy (drafts — refine, don't ship blind)

**X / launch post (pick one):**

> Your AI agent is about to sign a payment. Should it?
>
> Blackwall returns **GO / HOLD / STOP** before the signature — on-chain
> counterparty reputation, price-anomaly, OFAC screening.
>
> Now live on Base. 🛡️

> Everyone's racing to make AI agents pay *faster*.
> We built the thing that makes them pay *safely*.
> GO / HOLD / STOP before your agent signs an x402 payment. Live on Base.

**The killer demo tweet (verified-only — see §0 before editing):**

> Drove a real payment through Blackwall on Base mainnet today.
> It flagged an overpriced quote and returned **STOP** before signing.
> Settlement is on-chain (tx 0x9ddec8…), and *you can reproduce the STOP
> yourself* — POST an inflated amount to the live endpoint. A guardrail that
> actually fires. 🛡️ [link to verdict JSON + BaseScan tx]

*(Do NOT write "real on-chain settlement history / 34 settlements" — that's seed
data, see §0. Lead on the reproducible STOP + the real settlement tx instead.)*

**Taglines:** "A circuit breaker for agent payments." · "GO / HOLD / STOP before
the signature." · "Verdict-only, never custody."

---

## 5. Distribution plan (highest-leverage first)

1. **The "agent rule" / norm-capture move.** Get the framing *"agents should
   check Blackwall before signing an x402 payment"* into (a) the discovery
   descriptor, (b) the X bio/pinned post, (c) the awesome-x402 entry. Ontario
   (competitor) does exactly this in their service descriptor — it's their
   smartest play and it's copyable. Make *absence of a risk check* feel like the
   risk.
2. **Ship the live-gouge writeup.** A short blog/thread: "We pointed Blackwall at
   a real mainnet payment and it caught a 100× gouge." Concrete > abstract.
   Include the verdict JSON.
3. **Hit the x402 community surfaces.** x402 Discord/Telegram, Farcaster (the
   Base/x402 crowd lives there), Coinbase x402 ecosystem channels, the
   awesome-x402 maintainers. Builder-to-builder, not press-release tone.
4. **Framework dev capture.** LangChain / CrewAI / Vercel AI SDK communities —
   "call Blackwall before you sign" as a middleware pattern. A thin code snippet
   beats a pitch.
5. **MCP registries** (Smithery / Glama) — note these want a *reachable HTTP* MCP
   endpoint; today's MCP is stdio-only, so this is gated on a small build. Flag it
   to the eng session if it becomes the bottleneck.

---

## 6. Competitive reality (your wedge in the messaging)

- **Nearest competitor: Ontario Protocol.** Also does a free pre-payment verdict
  (`can-pay`), BUT decides on *endpoint readiness + the agent's budget cap* — NOT
  counterparty payment behavior, price fairness, or sanctions. Those three are
  Blackwall's wedge.
- **Crucially: Ontario has ZERO traffic.** Their own live stats read
  `total_reports: 0` (re-verified 2026-06-30). The category's distribution is
  **unclaimed** — nobody is ahead on data. This is the single most important
  strategic fact: *first to real traffic wins*, and the seat is empty.
- **Don't punch down or name-call competitors publicly.** Position on *what
  Blackwall judges* (financial counterparty risk + price + sanctions), not on
  "X is bad." Complement, don't trash.

Full analysis lives in `COMPETITIVE.md` in this repo — read it for the detailed,
byte-verified breakdown.

---

## 7. Voice & guardrails

**Voice:** technical, credible, builder-to-builder. No hype, no "revolutionary,"
no fake urgency. Security-minded devs are the audience; they smell BS. Lead with
proof (the live demo), concede the gaps honestly.

**Hard constraints (carry these into the new session):**

- **Never paste or generate private keys / secrets** in any artifact, post, or
  transcript. The funded wallets and keys stay off-screen.
- **Don't publish internal-only details** — facilitator secrets, receipt-signing
  keys, env vars, internal hostnames.
- **Don't overclaim adoption.** No "trusted by," no invented user counts. Capability
  proof only until there's real traffic.
- **GitHub scope** (if the session touches the repo): work stays within
  `bluetieroperations-create/agent-egress-proxy`. Code changes go on branch
  `claude/blackwall-x402-integration-j3rdab`. Don't open PRs unless explicitly
  asked. The awesome-x402 PR is edited manually by the user (it's an external
  fork) — guide, don't push.
- **Verify before publishing.** Re-fetch live endpoints/stats before quoting
  numbers; the x402 ecosystem moves weekly.

---

## 8. Suggested first moves for the new session

1. Read `COMPETITIVE.md`, `PRICING.md`, `BLACKWALL.md`, `DISCOVERY.md` in this
   repo for depth.
2. Draft + ship the **launch X post** and the **live-gouge demo thread** (§4).
3. Write the **"check Blackwall before you sign" norm line** into the discovery
   descriptor and the awesome-x402 entry.
4. Make a target list of **x402 / Base / agent-framework community channels** and
   a posting cadence.
5. Set up a way to **watch for first real traffic** (Render logs on
   `/v1/forecast-payment` + the payTo wallet on BaseScan) — first real call is a
   milestone worth announcing.

---

### Kickoff prompt to paste into the new session

> I'm starting a session focused on **marketing, PR, and distribution for
> Blackwall** — a pre-signature payment-risk oracle (GO/HOLD/STOP before an AI
> agent signs an x402 payment) that's **live on Base mainnet**. Read
> `MARKETING_HANDOFF.md` in the repo for full context, then help me execute the
> distribution plan in §5 — starting with the launch X post and the live
> "caught a 100× gouge on mainnet" demo thread. Keep the voice
> builder-to-builder, honest about gaps, no hype.
