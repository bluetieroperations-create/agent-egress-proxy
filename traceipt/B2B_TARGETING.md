# B2B targeting — compliance-infra, grounded in real x402 data

_Grounded on a live 724-endpoint x402 Bazaar crawl (Black_Wall's `ecosystem_scan`,
`data/report.json` + `data/candidates.csv`, Aug 2026). This is a target sheet to
work the [OUTREACH.md](OUTREACH.md) playbook against — honest sizing first, so we
don't chase a market that isn't there yet._

## The market reality (don't skip this)

- **724 listed endpoints, ~198 actually transact** (have on-chain settlement
  history). The x402 economy is **small and experimental** — a couple hundred
  real transactors, not thousands.
- **0 of 724 are sanctioned.** The sanctions-compliance pain today is
  **preventive/regulatory, not a live problem** — pitch "before your regulator
  asks," never "you're paying bad actors now."
- **Median price $0.014, p90 $0.15** (max $10,000). **Traceipt at $0.002 is ~7×
  below the market median.** The market bears far more per call than we charge.

**Implication:** the x402-native *seller* universe today is a dozen-ish real
companies plus a long indie tail. That is **not enough for $30k/mo on its own.**
$30k/mo is a broader-enterprise outcome as agent payments grow (12–24 mo); the
x402 data is the "a market is forming" evidence, not the target list itself.

## The buyer vs. seller distinction (the key correction)

The crawl is a list of x402 **sellers (payees)**. Traceipt's highest-value buyer
— a regulated enterprise/platform whose **agents *pay* these endpoints** and who
must prove those payments were screened — is on the **payer side**, which a
seller directory does not capture. So the data **sizes** the market, **names**
ecosystem players, and **prices** the product — it does **not** hand us the
enterprise buyer list. Those are reached via the agent-platform channel.

## ICP — three tiers, most-reachable first

### Tier 1 — Platforms / facilitators (embed, highest leverage)
Sell one integration, get receipts for all their sellers. Don't sell one-off.
- **Merit Systems** — runs the awesome-agentic-commerce directory + x402 tooling
  (appears in the crawl). Distribution partner.
- **Coinbase CDP** — the facilitator already screens sanctions but its trail is
  Coinbase-hosted; Traceipt is the *independent, portable* proof layer on top.
- **TrustBench** — registry/router with basic receipts; Traceipt is the depth
  (verdict-binding, selective disclosure) they lack.
- *Pitch:* "be the neutral receipt/compliance layer your sellers and their
  regulated buyers can rely on — one integration."

### Tier 2 — Regulated data/intel providers (sell-side, named & fundable)
Real companies transacting on x402 that serve institutional/regulated clients
who *will* ask for provable, compliant payment trails.
- **Glassnode** (`x402.glassnode.com`, 143+ settlements) — on-chain market data.
- **Arkham Intelligence** (`api.arkm.com`, max $8) — on-chain intelligence.
- Intel-API startups: `netintel.dev`, `bizintel-api`, `data.greeneris.io`.
- *Pitch:* "hand your agent-customers a verifiable, compliance-ready receipt —
  a trust signal your institutional buyers already expect."
- *Caveat:* Arkham/Glassnode are themselves in the analytics/intelligence
  business — approach as partners/resellers, not as if they need our *analytics*.

### Tier 3 — Enterprise agent-deployers (payer-side, the real budget, off-crawl)
Not in the seller crawl; found via the platform channel. Biggest checks,
longest cycle.
- Fintechs / enterprises piloting agent payments (the **Amazon Bedrock
  AgentCore Payments** ecosystem — compliance-forward, Base+Solana).
- *Pitch:* "prove to your auditor/regulator that every agent payment was
  sanctions-screened before it settled — an independent record you hold, not a
  vendor's dashboard."
- *Reality:* highest willingness to pay, 6–18 mo cycle, needs a real
  relationship. This is where the $30k/mo actually comes from, later.

## Pricing — reprice up to the market

$0.002/call is 7× under the $0.014 median. Options, none requiring a new customer:
- **Per-receipt:** $0.01–0.05 (below p90 $0.15) — 5–25× revenue/call, and it
  signals "premium/compliance," not "cheapest."
- **B2B subscription (the real money):** $500–$5,000/mo per company for API +
  retention + audit exports + SLA. **$30k/mo ≈ 10–60 paying business customers** —
  a normal B2B SaaS count, a real sales effort.
- Keep the x402 micropayment as the **frictionless agent-onboarding channel**,
  not the revenue engine.

## How to use this sheet

1. Start **Tier 1** (platforms) — one partnership beats ten one-off sells, and
   it's the fastest route to real agent traffic in front of the demo.
2. Run **Tier 2** named targets (Glassnode, Arkham, Merit) through the OUTREACH.md
   cold message — led with the live on-chain proof.
3. Treat **Tier 3** as relationship-building for the real revenue, later.
4. Every conversation is **demand discovery** — three "not a priority"s in a tier
   is a finding, not a cue to keep building (see STRATEGY.md).

## Honesty guardrails

- ❌ "There's a huge x402 compliance market today." → ~198 real transactors, 0
  sanctioned. It's early.
- ❌ "These crawl endpoints are our customers." → they're **sellers**; most
  buyers are payer-side/off-crawl.
- ❌ "$30k/mo is close." → not from x402-native sellers today; it's a broader
  12–24 mo enterprise outcome.
- ✅ "A market is forming (165M x402 txns, 198 live transactors, growing), the
  regulatory tailwind is real but early, and we have the only neutral,
  independently-verifiable, on-chain-durable compliance receipt — live on
  mainnet." (all true, and enough to earn a first conversation)

---

_Sources: `data/report.json`, `data/candidates.csv` (Black_Wall `ecosystem_scan`,
Aug 2026 Bazaar crawl); `docs/CATEGORY.md` (category price distributions);
`STRATEGY.md` / `OUTREACH.md` (thesis + cold-outreach playbook)._
