# Outreach targets — the named list

Companion to `OUTREACH.md` (strategy + message) and `TALKING_POINTS.md` (the hard
questions). This is the **who**: ~20 real x402 operators curated from
[awesome-x402](https://github.com/xpaysh/awesome-x402), the Bazaar, and the
ecosystem — ranked by the segment hypotheses in OUTREACH.md. Same guardrails:
this is a hypothesis list, the goal is learning whether the pain is real, and
one honest "no, here's why" beats ten polite listens.

**The per-target hook rule:** every message must name *their* thing in the first
sentence (what they run, from the list below) and connect it to ONE question —
"when someone asks you to prove a payment was screened/authorized,
independently, what do you hand them?" Then the verify link. Then shut up.

Primary channel: **x402 Foundation Slack (slack.x402.org)** — most of these
builders are in it; a DM referencing their listed service is warm, not cold.
Fallback: the contact surface listed per target.

---

## Tier 1 — likeliest to feel the pain (start here)

| # | Who | What they run | Hook (their thing → our question) | Contact |
|---|---|---|---|---|
| 1 | **Yield.xyz** (AgentKit) | Agents moving money into 3,300+ yields via MCP | Agents *moving funds into yield products* is the highest-stakes x402 flow there is — when an allocator's compliance team asks how a transfer was screened before settling, what's the artifact? | yield.xyz site/contact · Slack |
| 2 | **Finance District (Prism)** | Payment gateway for agentic commerce; TS/Python/Java SDKs; multi-chain settlement incl. Base | A *gateway* inherits every customer's audit demands. Prism could hand each merchant a neutral, offline-verifiable receipt per settlement — partnership angle, not sale. | developers.fd.xyz · Slack |
| 3 | **swerver** | High-perf x402 gateway proxy: per-route USDC pricing, payment verification, managed Stripe payouts | They already do payment verification + **Stripe payouts** — the fiat leg means bookkeeping is real for their sellers today. Receipts as a gateway feature. | x402.swerver.net · Slack |
| 4 | **melis.ai** | 23 audit-verified pay-per-call endpoints on Base (incl. KYA wallet-trust oracle, xAudit response auditing) | They literally market endpoints as "audit-verified" and run a **KYA Oracle** — same buyer instinct. Do their enterprise callers ask for proof the payment leg was screened? | melis.ai (Operator link) · GitHub `mizukaizen` |
| 5 | **Sentinel (Valeo)** | "Enterprise audit & compliance layer for x402" — budgets, audit trails, payment explorer | Closest neighbor in the list. Their audit trail is **platform-hosted**; ours is the neutral/offline-verifiable layer. Partner or learn: how do their customers phrase the audit ask? | sentinel.valeocash.com · valeocash.com |
| 6 | **Orbis API Marketplace** | x402-native marketplace, 1,000+ APIs at $0.01/call on Base | A marketplace's sellers each face month-end with thousands of unexplained micro-outflows. One integration = receipts for every seller. | orbisapi.com · Slack |
| 7 | **AIsa** | Payment network for AI agents (multi-rail) | Network-level player serving platforms — the "regulated buyer needs a portable proof" wedge from OUTREACH.md §1 applies directly. | aisa.network · Slack |

## Tier 2 — money-moving sellers (concrete dispute/audit needs)

| # | Who | What they run | Hook | Contact |
|---|---|---|---|---|
| 8 | **Veles Finance Agent** | Finance agent gateway (fly.dev) | Finance-flavored agent flows — disputes are a when, not an if. | veles-finance-gateway.fly.dev · Slack |
| 9 | **AI Rook** | Trading intelligence + general agent APIs | Trading signals bought by agents: when a signal purchase is disputed, the receipt is the record. | agents.ai-rook.com |
| 10 | **Stratalize** | Trading/analytics APIs over x402 | Same as above — high-value calls, real money consequences. | stratalize.com |
| 11 | **Arch Tools** | 58 production API tools for agents, 15+ chains | Volume seller; their buyers' finance teams see the 10k-lines problem first. | archtools.dev · GitHub `Deesmo` |
| 12 | **agentsvc.io** | 20 utility tools, $0.001–0.008/call, auto-discovery | Prolific, standards-minded builder (well-known/agent-services.json) — good for an honest "is this real pain?" conversation. | GitHub `jakobautomation` |
| 13 | **BitBooth** | 6 endpoints incl. `approval-safety` pre-flight risk checks | They already sell a *pre-flight risk check* — kindred safety instinct, natural Black_Wall + Traceipt conversation. | GitHub `Drock91` |
| 14 | **Hive Civilization** | 52-service agent fleet on Base mainnet, publishes "verifiable Spectral receipts" | Already rolling their own receipts — either a user or the best possible critic. Standards conversation. | GitHub `srotzin` · Slack |

## Tier 3 — tooling/infra: partnerships & standards conversations

| # | Who | What they run | Hook | Contact |
|---|---|---|---|---|
| 15 | **ScoutScore** | Trust scoring for 1,700+ x402 services (4-pillar model) | Complementary: they score services, we prove payments. Their "Identity & Safety" pillar could cite receipt issuance. | scoutscore.ai · npm `@scoutscore` |
| 16 | **Crest Verify** | Conformance checks + "signed trust receipts" for x402 endpoints | They issue *trust receipts* for endpoints — adjacent primitive. Converge or differentiate; either answer is learning. | verify.crestsystems.ai · GitHub `andysalvo` |
| 17 | **Tessera** | Public deterministic credit scores for agents on Base | Credit needs history; signed receipts are the highest-grade history input. Data partnership angle. | tesseracredit.com · GitHub `cmxdev1` |
| 18 | **Paybound** | OSS governance proxy: budgets, circuit breakers, SQLite audit trail | Their audit trail is a local SQLite file — the "prove it to a third party" gap is exactly ours. OSS-to-OSS conversation. | GitHub `pando-b` |
| 19 | **Mycelium Trails** | Post-execution accountability receipts (dual-chain anchor, Kleros disputes) | Fellow receipts builder, different bet (disputes/reputation vs compliance/accounting). Standards + mutual-listing conversation, not competition. | GitHub `giskard09` |
| 20 | **x402 List / SmartFlow Mapper** | Live directories of x402 services (uptime, payment-success metadata) | Listing partnerships: a "issues verifiable receipts ✓" field in their directories helps both sides. | x402-list.com · api.smartflowproai.com |
| 21 | **KaelAi** | Wallet trust scoring API for x402 servers | They screen wallets; we prove the screen happened. Verdict-binding is literally our product; theirs could be an input. | kaelai.io |
| 22 | **Tate Programs** | x402 launch/readiness surface checks | They audit x402 surfaces pre-launch — could recommend receipts as a launch checklist item. | tateprograms.com |

*(From OUTREACH.md's original hypotheses, still standing: TrustBench and AEON as
facilitator/router partnerships — find them in Slack; the Bedrock AgentCore
Payments ecosystem for the slow enterprise lane.)*

---

## The messages (adapted from OUTREACH.md — keep their shape)

### Slack DM (primary; personalize the first sentence per the table)

> Hey — saw <name> on the x402 list (<their thing, one clause>). One question
> I'm trying to answer honestly: when someone — a customer, an auditor, a
> counterparty — asks you to *prove* a payment through your service was
> screened/authorized before it settled, independently of any vendor dashboard…
> is that a thing you've hit, or not yet?
>
> Context: I built the neutral-artifact take, live on Base mainnet — you can
> break it in your browser, it calls my server zero times:
> https://traceipt.xyz/verify (try "Load the OFAC Tornado Cash STOP").
> Not selling — validating whether this pain is real. A "no, because…" is
> genuinely useful.

### Email (use OUTREACH.md's cold message verbatim; it's good)

Subject: `proof your agent didn't pay a sanctioned party — now live on mainnet`
— body in `OUTREACH.md` §"Cold message". Add the per-target first sentence from
the table.

### The badge close (append to any variant when there's interest)

> One more thing — issuing services get the "Receipts by Traceipt" badge
> (traceipt.xyz/badge). It's not a logo: it has to link to a live verification
> your customers can run themselves, so it's a checkable claim, free with the
> testnet cohort.

### Partnership variant (Tiers 1 gateway/marketplace + Tier 3)

> Different angle than a sale: you have distribution to x402 sellers; we have a
> neutral receipt layer none of them wants to build. "Receipts by Traceipt" as a
> per-seller feature — their finance teams get reconcilable, offline-verifiable
> records; you get a differentiator. Worth 15 minutes to see if it composes with
> <their product>?

---

## Cadence + tracking (keep it honest)

- **Batch of 5, not 22.** Send Tier 1 first; wait for signal before widening.
  Every conversation updates the hypothesis — that's the point.
- Track in this file: append `→ sent <date, channel>` / `→ reply: <gist>` per row.
- Success metric per OUTREACH.md: not replies — **one person who says "yes, I
  need this, here's my budget/timeline"**, or three good-faith "not a priority"
  answers from Tier 1 (which is a finding: the thesis is early).
- Rate-limit yourself in Slack: DMs to people whose services you can name are
  warm; blasting all 22 in one day reads as spam and burns the room.
