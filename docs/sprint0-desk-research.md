# Sprint 0 — desk-research findings (HANDOFF §4 gates)

**Method:** deep-research harness — 3 search angles, 14 sources fetched, 60 claims
extracted, **25 adversarially verified** (2-of-3 vote to kill), 5 killed, 9
surviving findings. Date: mid-2026. Confidence is **claim-level**; see caveats.

> **Source-quality caveat up front:** much of the competitive evidence is vendor
> self-description (x402secure.com, vvpro.ai, anchain.ai) and a self-submitted
> community directory (awesome-x402). That proves a competitor **exists and what
> it claims** — NOT its adoption, revenue, or efficacy. Competitor maturity is
> early (npm v0.1–1.0). x402 volume swings month-to-month, so every number is a
> snapshot, not a trend.

## GATE A1 — Market volume & timing → **NO-GO (too early)**

- x402 grew from near-zero (mid-2025) to **100M+ cumulative tx on Base** through
  Q1 2026 — but real recurring volume is **thin and declining**: **~$28K/day
  settlement (Mar 2026), ~92% off the Dec 2025 peak**. Much of the surge was
  **speculative meme-coin minting (PING)**, not agent commerce. Chainalysis:
  *"mass adoption remains distant."* CoinDesk (Mar 2026): *"demand is just not
  there yet."* [3-0 verified]
- **Read:** the headline growth is real but overstates genuine autonomous-agent
  commerce. There is not yet enough *recurring* agentic payment volume for a
  per-check risk service to ride. This is a **timing** no, not a permanent one.
- Sources: chainalysis.com/blog/x402-agentic-payments-adoption, coindesk.com
  (2026-03-11), vvpro.ai.

## GATE A2 — Competitive map & free baseline → **NO-GO (niche taken), with a crack**

- **The pre-signature payee-risk-verdict niche is NOT open.** ≥8 live services
  already return allow/review/block verdicts: **x402-secure (t54 labs)**,
  **AgentRadar (vvpro.ai)**, Frisk, AgentSpendGuard, Revettr, Ontario Protocol,
  x402 Trust Oracle, plus seller-side gates. [3-0 verified]
- **x402-secure** — funded ($5M seed; Ripple / Franklin Templeton / Anagram),
  multi-chain (Base/Solana/XRPL), runs risk *"before money moves."* **But its
  signal mix is agent-side behavioral/intent risk (prompt injection, compromised
  agents) — NOT counterparty settlement/dispute reputation.** (The claim that it
  does counterparty reputation was **refuted 0-3.**) [niche occupancy 3-0]
- **AgentRadar** — *"Stripe Radar for agent payments,"* a 6-signal composite
  payee verdict, **$0.005/check**, free to 100/day. Closest direct competitor.
  [3-0]
- **Free baseline (Coinbase CDP facilitator):** bundles **KYT sanctions
  screening free**, non-custodial — **but does NOT cover counterparty
  settlement/dispute reputation or price-anomaly.** [3-0] **AnChain.AI** screens
  x402 counterparties but framed as **AML/sanctions**, not dispute-history or
  price-anomaly. [3-0]
- **The crack:** Blackwall's *specific* signals — counterparty **settlement/
  dispute reputation** + **price anomaly** — are the **least-covered** dimension.
  Incumbents do sanctions/identity/behavioral; nobody clearly owns
  reputation+price-anomaly. Differentiation may be real but **narrow**, and
  competitors are converging.
- Sources: x402secure.com, github.com/t54-labs/x402-secure, vvpro.ai,
  docs.cdp.coinbase.com/x402, anchain.ai/blog/x402, x402.org/ecosystem,
  github.com/xpaysh/awesome-x402.

## GATE A6 — Unit economics → **CONDITIONAL (viable, but brutal baseline + unresolved true cost)**

- Sub-cent/low-cent pricing is **already being attempted**: AgentRadar
  $0.005/check, x402 Trust Oracle $0.002/call. [3-0]
- Indexing *floor* is plausibly sub-cent: Alchemy $0.45/1M CU, ~27 CU/request →
  **~$0.000012/request** — **but real reputation/log-history queries cost
  meaningfully more CUs**, and self-index/Goldsky/Dune costs were **not pinned
  down**. So the true per-check cost stack is **unresolved**. [floor 3-0; "real
  queries cost more" flagged by verifiers]
- **The brutal baseline:** CDP is **free to 1,000 settled payments/month, then
  $0.001/settled payment, with KYT bundled** (eff. Jan 2026). A paid Blackwall
  check competes against ~free. [3-0]
- Sources: alchemy.com/pricing, docs.cdp.coinbase.com/x402, x.com/CoinbaseDev
  (2026), kucoin.com news.

## Overall: **CONDITIONAL GO to Sprint 1 — but reframe the hypothesis**

Desk research can only tell us if the niche is *taken* or *too early* (both lean
cautionary) — it **cannot** confirm willingness-to-pay. The original thesis
("the niche is open") is **falsified**. The surviving, testable bet is narrower:

> **Will agent operators pay for counterparty settlement/dispute reputation +
> price-anomaly signals *specifically*, on top of free facilitator KYT, when 8+
> (mostly pre-revenue) competitors already exist — and is recurring volume real
> enough to matter?**

**Recommendation:** proceed to Sprint 1 interviews **only to test that narrowed
hypothesis**, and treat A1 (timing) as a live risk. Do **not** build further on
the assumption of an open niche. Two cheap desk follow-ups would sharpen it:
(1) pin Blackwall's *true* per-check query cost (CUs for the actual reputation +
price-anomaly lookups, not the 27-CU generic average); (2) confirm whether any
incumbent actually computes settlement/dispute reputation under a different label.

## Open questions (carry into Sprint 1)

1. True fully-loaded per-check cost for Blackwall's *specific* queries — stays
   sub-cent at scale?
2. Do x402-secure / AgentRadar / AnChain actually compute settlement/dispute
   reputation + price-anomaly, or only sanctions/behavioral/identity?
3. Real adoption/revenue of the named competitors (most npm v0.1–1.0) — is the
   niche contested by anyone with traction, or out-executable micro-projects?
4. Willingness-to-pay above zero on top of free bundled KYT + $0.001 settle —
   and at what ceiling vs. the sub-cent transaction being protected?

## Killed claims (failed adversarial verification — recorded for honesty)

- x402-secure does counterparty/merchant reputation+identity — **0-3**.
- The CDP free baseline leaves the niche *fully* unoccupied — **1-2**.
- PING retention crash (87%→5%) proves volume is speculative — **1-2** (over-claim).
- AnChain's <200ms MCP check exactly occupies Blackwall's niche — **1-2**.
- Sanctions screening is already commoditized at a sub-cent price floor — **1-2**.
