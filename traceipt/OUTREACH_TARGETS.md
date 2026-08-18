# Outreach targets — the named list

Companion to `OUTREACH.md` (strategy + message) and `TALKING_POINTS.md` (the hard
questions). This is the **who**: real x402 operators — ranked by the segment
hypotheses in OUTREACH.md. Same guardrails: this is a hypothesis list, the goal
is learning whether the pain is real, and one honest "no, here's why" beats ten
polite listens.

**Two lists, measuring two different things — read this first.**

- **Tier 0 (below)** is *measured*: hosts observed live and machine-parseable in a
  Bazaar-wide liveness survey (195 hosts probed 2026-08-18; ranked by
  `distinct_payers` — the count of independent counterparties that have settled
  with them, the single hardest-to-fake number in the dataset). Source:
  `data/liveness.json` on branch `survey/directory-liveness` (Blackwall side).
- **Tiers 1–3** are the original *hypothesis* list, curated from
  [awesome-x402](https://github.com/xpaysh/awesome-x402), the Bazaar, and blogs —
  i.e. *who talks about x402*, which is not the same as *who has observed
  settlements*.

When the survey cross-referenced the two, **only 3 of the 19 original named
targets appear in the measured map** (Stratalize, Yield.xyz, AI Rook — annotated
inline below). That does **not** mean the other 16 are dead or fake — the survey
corpus is a partial Bazaar crawl and plenty of real operators never list there.
It means the two lists are nearly disjoint, and Tier 0's leads are backed by
*observed on-chain settlement* rather than self-description. **Lead with Tier 0.**

Caveats that bound every Tier 0 number (from the survey write-up):
- **`distinct_payers` is the ranking key**, not raw settlement count — an operator
  can pay themselves to inflate volume, but not the number of independent
  counterparties.
- **This measures supply (reachability + format), not demand.** That an endpoint
  is live and parseable says valid inputs exist; it says *nothing* about whether
  its operator wants a receipt layer. Necessary, not sufficient.
- **Single-host `dead`/`other` is noisy** — one host TLS-failed in one pass and
  returned a clean challenge the next. Re-probe before writing anyone off.
- Prices below are the advertised per-call charge (USDC base units → dollars).

**The per-target hook rule:** every message must name *their* thing in the first
sentence (what they run, from the list below) and connect it to ONE question —
"when someone asks you to prove a payment was screened/authorized,
independently, what do you hand them?" Then the verify link. Then shut up.

Primary channel: **x402 Foundation Slack (slack.x402.org)** — most of these
builders are in it; a DM referencing their listed service is warm, not cold.
Fallback: the contact surface listed per target.

---

## Tier 0 — measured, settlement-backed (lead with these)

Recognizable businesses live *and* parseable on Base, ranked by distinct payers.
These are companies large enough to plausibly have someone who owns the audit
question Traceipt sells against — and their counterparty counts are observed, not
claimed. All `body_accepts` (our body-only 402 emit is compatible) unless noted.

| # | Who | Host | Payers | Price/call | Why they're a Traceipt lead |
|---|---|---|---:|---|---|
| A | **Bitrefill** | `api.bitrefill.com` | **134** | $0.001 | Gift-card/crypto commerce with real fiat-adjacent bookkeeping and the most independent counterparties in the entire survey. If anyone's finance team already asks "prove this payment was screened," it's a commerce operation at this scale. |
| B | **Nansen** | `api.nansen.ai` | 34 | $0.01 | Enterprise crypto-analytics brand; sells to funds/desks whose compliance teams live the audit ask. Warm because they *are* the audience for "portable proof." |
| C | **Apify** | `agi.apify.com` | 18 | $1.00 | Established scraping/automation platform (enterprise customers). Highest per-call price in the top set → high-value calls → disputes are a *when*. |
| D | **CoinMarketCap** | `pro-api.coinmarketcap.com` | 16 | $0.01 | Household-name data API; enterprise data buyers who reconcile spend. |
| E | **Vaults.fyi** | `api.vaults.fyi` | 15 | $0.002 | DeFi-yield data; buyers are allocators moving money — the highest-stakes reconcile flow. |
| F | **Interzoid** | `api.interzoid.com` | 13 | $0.01 | Data-quality SaaS (a real company, not an x402-native experiment); 37 priced resources. |
| G | **0x** | `agent.api.0x.org` | 12 | $0.01 | Core DEX infrastructure; a name every counterparty recognizes — strong standards-conversation partner. |
| H | **CoinGecko** *(via x402atlas)* | `coingecko.use.x402atlas.com` | 25 | $0.005 | Recognizable data brand, fronted by an x402 proxy — 25 distinct payers. |
| I | **DefiLlama** *(via x402atlas)* | `defillama.use.x402atlas.com` | 7 | $0.005 | Same proxy pattern; DeFi analytics buyers. |
| J | **CoinCap** | `rest.coincap.io` | 9 | $0.01 | Market-data API with a recognizable name. |
| — | **Stratalize** | `www.stratalize.com` | 13 | $0.50 | *Already Tier 2 #10* — and the ONLY original target that scores strong here (187 priced resources, high price). Promote it: start here among the originals. |

**High-counterparty unknowns** (big payer counts, unverified as businesses — treat
as "who is this?" research leads, not warm intros):

| Host | Payers | Price | Note |
|---|---:|---|---|
| `blockrun.ai` | 61 | $0.0085 | 2nd by payers — **but header-style (`WWW-Authenticate`)**. Doesn't affect our emit; flagged because it's the newer spec. |
| `x402.asterpay.io` | 53 | $0.01 | "pay"-branded — possibly a payments operator worth identifying. |
| `api.anchor-x402.com` | 52 | $0.005 | High volume, unknown operator. |
| `api.loyalspark.online` | 50 | $0.01 | 29 resources; unknown. |
| `company.payapi.market` | 48 | $0.001 | "payapi" — payments-adjacent; identify. |
| `2s.io` | 28 | $0.0025 | 29 resources; unknown. |

**How to use Tier 0:** the recognizable brands (A–J) get the *partnership/enterprise*
framing — they have distribution and finance teams. The unknowns get a research
pass (who runs this?) before any DM. Contact surface for the brands is their site's
enterprise/API contact plus the x402 Slack; for the x402atlas-fronted ones, reach
the atlas operator too.

---

## Tier 1 — likeliest to feel the pain (start here)

| # | Who | What they run | Hook (their thing → our question) | Contact |
|---|---|---|---|---|
| 1 | **Yield.xyz** (AgentKit) · *measured: `mcp.yield.xyz`, `wellknown`, 13 payers* | Agents moving money into 3,300+ yields via MCP | Agents *moving funds into yield products* is the highest-stakes x402 flow there is — when an allocator's compliance team asks how a transfer was screened before settling, what's the artifact? *(In the survey its inline route 404s and it serves a `/.well-known/x402.json` catalog instead — reach it via the well-known, not a bare probe.)* | yield.xyz site/contact · Slack |
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
| 9 | **AI Rook** · *measured: `agents.ai-rook.com`, `other` (not a 402 today), 3 payers* | Trading intelligence + general agent APIs | Trading signals bought by agents: when a signal purchase is disputed, the receipt is the record. *(Weak signal in the survey — endpoint didn't serve a parseable 402; re-probe before spending effort.)* | agents.ai-rook.com |
| 10 | **Stratalize** · **★ measured strong: `www.stratalize.com`, `body_accepts`, 13 payers, 187 priced resources, $0.50/call** | Trading/analytics APIs over x402 | The **only** original target that scores strong in the measured map — parseable challenge, real counterparties, high-value calls. **Start here among the originals.** | stratalize.com |
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

- **Batch of 5, not 30.** Lead with **Tier 0 A–J** (measured, settlement-backed)
  plus **Stratalize** — those are the highest-confidence starts. Wait for signal
  before widening into the Tier 1–3 hypotheses. Every conversation updates the
  hypothesis — that's the point.
- Track in this file: append `→ sent <date, channel>` / `→ reply: <gist>` per row.
- Success metric per OUTREACH.md: not replies — **one person who says "yes, I
  need this, here's my budget/timeline"**, or three good-faith "not a priority"
  answers from Tier 0/1 (which is a finding: the thesis is early).
- Rate-limit yourself in Slack: DMs to people whose services you can name are
  warm; blasting all of them in one day reads as spam and burns the room.

### Keep the measured data fresh

The survey is a **snapshot** (2026-08-18). Payer counts move; single-host `dead`
is noisy. Re-pull `data/liveness.json` from `survey/directory-liveness` (or re-run
`directory_liveness.py` on the Blackwall side) periodically — *which* hosts are
**gaining** payers is a better lead signal than any one-time rank. When you
refresh, re-sort Tier 0 by `distinct_payers` and note movers. Two number caveats
when quoting: `settlement_count` saturates at ~150 (ordering sound, absolute
figures are floors — never quote as volume), and everything here measures supply,
not demand.
