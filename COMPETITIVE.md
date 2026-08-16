# Blackwall — competitive landscape

**Snapshot date: 2026-06-29.** The x402 ecosystem is months-young and moving
weekly; treat this as a point-in-time map, not a standing fact. Each row carries
a confidence level and a source. Re-verify before betting positioning on it.

## TL;DR

A trust/reputation layer for x402 **has formed** and is contested — but **no one
verified here does Blackwall's actual job**: a *pre-signature verdict driven by
**behavioral counterparty reputation + price-anomaly + sanctions**, for the
paying agent*. The nearest neighbor (Ontario Protocol) **does** make a
pre-payment allow/deny decision (`/api/agent/can-pay`, free) — but it decides on
**endpoint readiness + the agent's own budget cap + report integrity**, not on
the counterparty's settlement/dispute history, price-fairness, or sanctions
status. Those signals are Blackwall's wedge.

> **Correction (2026-06-29, byte-verified).** An earlier draft said Ontario
> "does not make a pay/deny decision" and "defers financial risk downstream."
> That was wrong — surfaced by re-pulling the **raw** `openapi.json` (the earlier
> read trusted a model-generated page summary that omitted `/api/agent/can-pay`).
> Ontario *does* return allow/review/deny pre-payment. What it does **not** do —
> confirmed by scanning the full spec, where `dispute`, `price-anomaly`, `ofac`,
> `sanction`, `counterparty`, `median` are all **absent** — is judge the
> counterparty's financial behavior. The differentiation is signal depth, not
> presence/absence of a verdict.

**Positioning:** Blackwall is the **financial counterparty-risk layer**. The
nearest competitor (Ontario's free `can-pay`) decides on **endpoint readiness +
budget policy**; Blackwall decides on **counterparty payment behavior + price
fairness + sanctions**. Others are buyer-scoring gates for sellers (MolTrust /
Larkinsh / Crest), transaction-malice decoders (GPT55 / Blockaid), or enterprise
KYT (Chainalysis / AnChain). Blackwall is **complementary** to most. For endpoint
readiness it **replicates the commodity signal itself** rather than depending on a
competitor: `readiness.py`'s `LocalReadinessSource` scores the same observable
signals (402 implemented, manifest, https, openapi, ...) from public data we fetch
ourselves — no per-request call to Ontario, and no leaking our query stream to a
competitor. (An optional `OntarioReadinessSource` can consume their free `can-pay`
directly, but it is *not* the default precisely because of that dependency and
leak.) Either way the grade folds through the same conservative `apply_readiness`,
so Blackwall is *endpoint-readiness **plus** the financial layer*. On the core
pre-payment verdict it has a **direct, free competitor**, so the pitch is **signal
depth and the data moat**, not "the only one doing this."

## The map

| Project | What it does | Subject scored | Pre-sign? | Custody | Does it judge payee payment-risk + price? | Confidence / source |
|---|---|---|---|---|---|---|
| **Blackwall** | GO/HOLD/STOP before the agent signs, from behavioral settlement/dispute history + price-anomaly + OFAC | **Payee** (for the paying agent) | Yes | No | **Yes — this is the product** | — |
| **Ontario Protocol** | **Free `/api/agent/can-pay` → allow/review/deny** pre-payment decision, *driven by* endpoint readiness + budget cap + report integrity; plus paid trust-scan, EAS-attestation reputation, price benchmarks, and a paid directory | **Payee** (for the paying agent) | Yes | Advisory (+ optional 1.5% settlement proxy) | **Partial** — makes the pay/deny decision, but on readiness + budget, **not** behavioral counterparty reputation / price-anomaly / sanctions | **High** — byte-verified raw `openapi.json` + `x402-trust.json` (2026-06-29) |
| **MolTrust** (`@moltrust/x402`) | Trust-score middleware; reads paying wallet from `X-PAYMENT`, gates the endpoint | **Buyer/agent** (for the seller) | At request time | No | No | **Med** — repo listing |
| **Larkinsh** (`@larkinsh/x402`) | Authorization middleware; gates by a "5-dimension trust score" | **Buyer/agent** | At request time | No | No (dimensions undisclosed here) | **Med** — repo listing |
| **Crest** (data.crestsystems.ai) | Profiles an x402 buyer / EVM wallet: whale score, behavior cluster, spend graph, risk | **Buyer/wallet** | Lookup | No | Partial (wallet risk, not payee payment history or price) | **Med** — repo listing |
| **GPT55** | Wallet signing safety; EIP-712 / Permit2 risk decoding | The **transaction** | Yes | No | No (tx-malice, not counterparty) | **Med** — repo listing |
| **AnChain.AI x402** | KYT / OFAC / sanctions + high-risk wallet patterns into x402 via MCP | Wallet (compliance) | Screen | No | **Overlaps only** the OFAC layer | **Med** — vendor blog |
| **AgentZone** | Explorer: ERC-8004 identity + x402 payment history + reputation, Base/Arbitrum | Agents (browse) | No (explorer) | No | No (read-only explorer) | **Med** — repo listing |
| **ERC-8004** | On-chain identity + reputation **registry standard** for agents | Agents | n/a (standard) | n/a | No (substrate, not a verdict) | **Med** — EIP + repo |
| **Blockaid / Blowfish** | Pre-sign transaction/contract malice detection (wallets) | The **transaction** | Yes | No | No (not x402, not payment-counterparty) | **High** — well-known, but from prior knowledge |
| **Chainalysis / TRM / Elliptic** | KYT, sanctions, illicit-flow tracing | Wallet (compliance) | Screen | No | Overlaps OFAC layer; enterprise-priced, not agent-facing | **High** — prior knowledge |
| **PaySafe** (paysafe-agent.com) | allow/flag/block "payment security firewall" before settlement; Ed25519 attestations; prompt-injection payments, replay, overpay, **PII/secret leakage**, lookalike tokens/address poisoning, reputation registry, velocity limits | **Payment + payload** (for the paying agent) | Yes | No (advisory, non-custodial) | **Partial** — reputation registry + overpay, but **no OFAC/sanctions, no on-chain Sybil/graph, no advertised-vs-settled price**; and it verifies payload metadata, not the recovered EIP-3009 signer | **High** — marketing site + `#pricing`, 2026-08 |

### Unverified — do not cite without checking
A search summary named **"Frisk"** and **"AgentRadar"** as pre-payment allow/block
verdict services. **Neither appeared** when the actual `awesome-x402` repos were
fetched. **ACHIVX** (`@achivx/x402`, an agent reputation system) appeared in
search + a Medium post but was **not** confirmed in the repo listings. Treat all
three as unconfirmed until verified directly.

## Nearest neighbor: Ontario Protocol (direct competitor on the verdict; differentiated on signals)

Ontario is the closest thing to a head-on competitor: it exposes a **free
`/api/agent/can-pay`** endpoint that returns **`allow / review / deny`** *before*
the agent pays — the same product surface as Blackwall's GO/HOLD/STOP, same
pre-payment moment, Base/USDC. The difference is **what drives the decision**,
confirmed by byte-level reading of the raw spec (2026-06-29):

- **`can-pay` decides on readiness + budget, not financial counterparty risk.**
  Its `AgentCanPayDecision` inputs are the endpoint's **readiness grade**
  (`VerificationSummary`), the agent's own **budget cap** (`max_usdc` vs
  `declared_price_usdc`), and `report_integrity_ok`. It is price-*aware* (it sees
  `declared_price_usdc`) but checks it against *your budget*, not a fairness
  baseline.
- **It's free.** Direct pricing pressure on a paid per-verdict model.
- **Zero traction so far.** `x402-trust.json → current_stats` reads
  `total_reports: 0` (ready/close/needs_work all 0) — the surface is built, the
  data is empty. Nobody is ahead on accumulated history.

- **Trust standard = technical readiness + discoverability.** Weighted dimensions
  (from `x402-trust.json`): `payment_challenge` (402 implemented) 20,
  `x402_manifest_present` 15, `x402_manifest_well_formed` 15, `https` 10,
  `endpoint_reachable` 10, `openapi_schema` 10, `homepage_reachable` 10,
  `robots` 3, `bazaar_metadata` 5, `schema_org` 2.
- **Reputation = attestations.** `reputation/{agent_id}` returns `trust_score`,
  `attestation_count`, `attestations[]` — sourced from on-chain **EAS**
  attestations. Declarative/credential reputation, **not** behavioral
  settlement-outcome reputation.
- **Trust scan = agent identity.** `agent-trust-scan` takes the agent's card /
  surface-area URL and returns `trust_score`, `signals`, `issues`, `evidence`.
- **Business = directory/marketplace.** Paid listings (`list-agent` 0.10 USDC,
  `list-service` 0.50 USDC) + an **optional settlement proxy with a 1.5% take
  rate** — i.e. Ontario is drifting *toward* custody-adjacent settlement, away
  from pure advisory.
- **Confirmed absent across the entire 89KB OpenAPI (byte-scanned):** the strings
  `dispute`, `price-anomaly`, `counterparty`, `median`, `ofac`, `sanction`,
  `twap`, `oracle` do not appear anywhere. So no settlement/dispute reputation, no
  price-fairness baseline, no sanctions screening.
- **Their own words (verbatim `agent_rule`):** *"Agents should check Ontario
  before paying unknown x402 endpoints. Prefer endpoints with grade=ready and a
  public report_id. Treat missing Ontario verification as a risk signal, not a
  fatal error."*

**Read:** Ontario's `can-pay` answers *"is this endpoint set up correctly and
within my budget?"* Blackwall answers *"is this payee trustworthy with this
payment at this price — given their settlement/dispute history, price norms, and
sanctions status?"* Both return a pre-payment verdict; they judge on different
evidence. Blackwall can even take Ontario's readiness grade as one input.

## Where Blackwall is differentiated (confirmed whitespace)

Verified **absent** in the nearest neighbor and not found elsewhere in the
verified set:

1. **Price-anomaly / price-fairness** — quoted amount vs the counterparty's *own*
   median historical price, now **wash-trade-hardened**: the baseline is a
   per-distinct-payer median (`robust_price_median`), so a counterparty can't
   anchor its own "normal" price by paying itself. Not present in any verified
   competitor. (Peer-group cross-check is the remaining, not-yet-built half.)
2. **Behavioral counterparty reputation** — Bayesian settlement/dispute history
   from *actual chain-confirmed outcomes*, vs attestation/credential reputation
   (Ontario/EAS, ERC-8004). Harder to game; harder to bootstrap (the trade-off).
3. **OFAC/sanctions folded into one pre-payment verdict** — only AnChain overlaps,
   as a separate compliance product, not a single agent-facing verdict.
4. **Payee-side, for the buyer.** Most x402 trust tooling scores the **buyer** so
   the **seller** can gate (MolTrust, Larkinsh, Crest, ERC-8004's "sellers check
   an agent's history"). Blackwall scores the **payee** so the **buyer** can
   decide to pay — the rarer direction.

## Direct competitor: PaySafe (2026-08) — same job, differentiated on depth + compliance

`paysafe-agent.com` is the closest thing yet to Blackwall's *actual* job: an **advisory,
non-custodial, pre-settlement payment firewall for AI agents** on x402, emitting signed
**Ed25519 attestations** with machine-readable reasons. Positioning is nearly
interchangeable ("scan before you pay" vs "call before you sign"). The differences:

**Where PaySafe leads (gaps for us):**
- **Broader framework reach** — drop-in packages for LangChain, **CrewAI, Vercel AI SDK,
  Coinbase AgentKit, NVIDIA NeMo** (we have LangChain + Turnkey/Privy wallets + OpenClaw +
  MCP). More GTM surface.
- **PII/secret-leakage detection** (private keys, seed phrases, API keys, SSNs) — a real
  capability we lacked. **→ ADOPTED: `secret_scan.py`** (HIGH credential → STOP, PII →
  HOLD; redacted, free-text-only to avoid flagging tx hashes).
- **Published perf** — advertises "0.60ms/scan" and "100% uptime / 90d". Treat as
  marketing (0.6ms *round-trip incl. HTTP* is implausible over a network — likely
  in-process). We publish neither yet; our pure `decide_payment` can likely beat it
  honestly + verifiably.

**Where Blackwall leads:**
- **Actual compliance.** PaySafe's site never mentions sanctions/OFAC. Blackwall treats
  OFAC as a hard-STOP authority with a strict "crowd-tags are not compliance" boundary.
- **Depth of on-chain counterparty analysis** — graph Sybil (captive/ring/anchor-isolation),
  advertised-vs-settled bait-and-switch, going-bad recency. PaySafe's reputation is a
  report-driven registry; ours self-populates from public Base history (zero-customer).
- **Verifies the actual signed payment** — recovers the EIP-3009 signer + calldata drainer
  detection + AA co-signing, not just scanning metadata strings.
- **Tamper-evident audit trail** (Traceipt Merkle anchoring) beyond a bare signature.

**Pricing philosophy is opposite.** PaySafe: flat **per-scan** ($0.01→$0.002) + subscription
tiers ($4.99 / $19.99 per 30d), 100 free calls/key. Blackwall: **value-aligned** — free
under $1, else 0.1% of amount-at-risk clamped $0.001–$0.10 (`PRICING.md`). PaySafe is
cheaper on high-value payments and simpler to predict; Blackwall is free on the long tail
and captures more on genuinely risky payments. Neither strictly wins — depends on the
customer's payment-size distribution.

**Confidence: High** (marketing site + `#pricing`, 2026-08). Claims (latency/uptime) are
self-reported, not verified.

## What is NOT a moat (honest)

- **Any single signal is copyable.** Ontario (or anyone) could add a price-anomaly
  check or an OFAC list in a sprint. The defensible asset is the **accumulated,
  chain-confirmed settlement/dispute history** (the `ledger.py` verdict→outcome
  flywheel) — data that compounds with runtime, not code.
- **Behavioral reputation has a harder cold-start** than attestation reputation.
  That is simultaneously the moat (hard to fake) and the near-term burden (thin
  early data → conservative HOLDs). On-chain ingestion mitigates but doesn't erase
  it.
- **Nothing structural stops Ontario adding** behavioral/price/sanctions signals
  to its existing `can-pay` verdict. They already have the verdict surface and a
  price-benchmark dataset — closing the signal gap is incremental for them.
- **Free-verdict pressure is real and specific.** Ontario gives the pre-payment
  `allow/deny` away **free** and monetizes listings + trust-scans instead. That
  undercuts a "charge per verdict" model directly, and is itself a caution about
  pricing the core verdict rather than the data/integration around it.

## Sharpened awesome-x402 listing entry

> **Blackwall** — Pre-signature **financial counterparty-risk** verdict for x402.
> Returns **GO / HOLD / STOP** before an agent signs a payment, from *behavioral*
> counterparty reputation (chain-confirmed settlement/dispute history),
> **price-anomaly** vs the counterparty's own median, and **OFAC sanctions**
> screening. Complements endpoint-readiness (Ontario) and KYT (AnChain): it judges
> *whether to trust this payee with this payment at this price*, not whether the
> endpoint is set up. Itself an x402 resource (pay-per-forecast, sub-cent;
> reusable sessions) and an **MCP server**. Verdict, never custody.
> `POST /v1/forecast-payment` · MCP `forecast_payment` · Base / USDC.

Category: **payment-risk / agent-guardrails**.
Tags: `x402` `payments` `counterparty-risk` `price-anomaly` `sanctions`
`reputation` `agent-guardrail` `base` `usdc` `mcp`.

> Listing target note: the live, active directories are
> [`xpaysh/awesome-x402`](https://github.com/xpaysh/awesome-x402) and
> [`Merit-Systems/awesome-x402`](https://github.com/Merit-Systems/awesome-x402)
> (the `coinbase/awesome-x402` URL in older notes may be stale — verify before
> submitting).

## Tokenized-RWA transfer-restriction readiness (the `rwa_readiness.py` wedge)

**Snapshot: 2026-08-16, web-researched + source-cited.** A distinct capability from the
x402 payment-verdict map above: a **pre-trade, buyer/agent-side check that predicts
whether the SECURITY leg of an RWA purchase will REVERT** (receiver not KYC'd/whitelisted,
frozen, or token paused) before the agent signs the stablecoin payment. "Will I pay USDC
and receive nothing?" See `docs/TOKENIZED_RWA.md`.

**Verdict: the exact cell is empty.** No one productizes a buyer-side, agent-facing,
RWA-restriction-*aware* pre-payment settlement-readiness verdict. The adjacent categories
are NOT this:

| Category | Players | What they do | Why it's not us |
|---|---|---|---|
| **(a) Issuer-side enforcement** | Tokeny/ERC-3643, **Securitize (DS Protocol)**, Chainlink ACE+CCID, Dinari, Predicate, Swarm | Own the on-token compliance module that *reverts* a non-compliant transfer | Issuer-authored, on-chain-enforced. Never hand the **buyer** a pre-payment "you'll receive nothing" warning |
| **(b) Generic tx simulation** | Tenderly, Blockaid, Blowfish, Alchemy `simulateAssetChanges`, Pocket Universe | Predict *any* tx revert | **RWA-restriction-blind** — a compliance revert is an undifferentiated "tx will fail"; no "receiver not KYC'd/frozen/paused" label; **no notion of the paired USDC-out leg** |
| **(c) OUR CELL** | — | Buyer/agent-side, pre-*payment*, restriction-*aware* verdict tied to the stablecoin leg, folded into the pre-signature guard | **Nobody occupies it** |

**The pre-trade READ primitives exist and are public across every standard** — that's why
the moat is thin, not why it's occupied: ERC-3643 `isVerified`/`canTransfer`, Securitize
`preTransferCheck` (gas-free, **buyer-callable by design**, returns a reason string), ERC-1404
`detectTransferRestriction`/`messageForTransferRestriction` (restriction code + message),
ERC-1400 `canTransfer`. Any issuer (esp. Securitize) or simulator (Blockaid/Tenderly) could
add an "RWA-restriction-aware" label with modest effort.

**So the differentiation is NOT the read.** It is: **(i)** normalizing across heterogeneous
standards (ERC-3643 / ERC-1404 / Securitize-DS / allowlist+frozen+paused — `rwa_readiness.py`
covers all four), **(ii)** the **buyer/agent framing tied to the paired USDC-out leg** (the
pay-and-receive-nothing harm no simulator models), and **(iii)** **folding it into the
pre-signature payment verdict** in the x402/agent-guard path — the integration nobody else
has wired. This is a **land-grab-and-integrate** play (be in the agent's decision path first),
not a defensible-primitive play.

**Demand is live and unguarded** (the reason the cell matters): Ondo × Virtuals × Treasures
opened **430+ tokenized stocks to ~40,000 AI agents** (Jun 2026); Yield.xyz's AgentKit put
3,300+ onchain yields (incl. tokenized treasuries) behind x402 — both with **no eligibility
preflight in the loop**. Agents are already pointed at hard-gated securities with nothing
checking settlement.

**Value concentrates on HARD-gated assets** — Dinari-style embedded KYC, ERC-3643/T-REX
permissioned securities, frozen/paused states. The "freely transferable, KYC-at-mint-only"
models (Ondo, Backed xStocks) have little per-transfer restriction to predict.

### Adjacent standard: ERC-8226 "RAMS" (Regulated Agent Mandate Standard, Brickken)

**Complement, not competitor — and a future consumable, not a threat.** RAMS (Draft ERC,
filed 2026-04-12; reference impl merged 2026-06-29; active thread + a Sepolia deployment, no
mainnet adoption yet) is an on-chain **agent-authorization** layer: a signed, time-bounded,
amount-capped, revocable "mandate" keyed by `(agent, principal)` that a regulated token
validates atomically via `canExecute(agent, principal, asset, action, amount) → (bool,
ExecutionReason)`. Crucially, **RAMS explicitly does NOT check KYC/whitelist/transfer-
restrictions** — it leaves receiver eligibility to the token's own ERC-7943/ERC-3643 hook and
runs *in parallel*. So it sits strictly **ABOVE** our eligibility axis on a different revert
cause (authorization: `OVER_TX_CAP`/`AGENT_FROZEN`/`REVOKED`/expired-mandate). It cannot
replace or block our check; instead its `canExecute` view is a **second pre-transfer
revert-predicate** we could read exactly like `detectTransferRestriction` to make an
agent-side readiness check complete. Posture: **watch + be ready to consume** (a thin, opt-in,
fail-open `RamsReadinessSource` fired only when an asset advertises an `IAgentMandate`
registry); do NOT hard-integrate yet — real assets exposing a queryable RAMS registry are
effectively zero today, and Brickken (which also authors the ERC-7943 substrate RAMS rides)
is a single-vendor champion. Trigger to build: a mainnet token shipping a RAMS hook.

**Strategic risk here is speed, not an incumbent:** the cell is empty in front of live demand,
but an issuer-side player (Securitize already exposes buyer-callable `preTransferCheck`) or a
simulator could extend into it once agent-RWA volume makes labeling worth it. The moat is
coverage + being in the agent's decision path, not the primitive.

### Sources (tokenized-RWA section, accessed 2026-08-16)
- ERC-3643 `canTransfer`/`isVerified`: https://docs.erc3643.org/erc-3643/smart-contracts-library/compliance-management · https://eips.ethereum.org/EIPS/eip-3643
- Securitize DS `preTransferCheck` (gas-free, publicly callable): https://medium.com/securitize/understanding-transfer-restrictions-for-digital-securities-4652ef97813f
- ERC-1404 `detectTransferRestriction`/`messageForTransferRestriction`: https://github.com/ethereum/EIPs/issues/1404
- ERC-1400 `canTransfer`: https://github.com/SecurityTokenStandard/EIP-Spec
- Chainlink ACE + CCID (Jun 30 2025): https://blog.chain.link/automated-compliance-engine-technical-overview/
- Dinari dShares embedded KYC; Ondo/Backed models: https://eco.com/support/en/articles/15254023-tokenized-equities-2026-backed-dinari-robinhood
- ERC-8226 RAMS: https://eips.ethereum.org/EIPS/eip-8226 · https://ethereum-magicians.org/t/erc-8226-regulated-agent-mandate/28208 · https://github.com/ethereum/ERCs/pull/1844
- Predicate (Plume): https://plume.org/blog/predicate
- Generic sim (generic revert, not RWA-labeled): https://docs.tenderly.co/simulations · https://www.blockaid.io/transaction-security
- Live unguarded agent-RWA demand: Ondo×Virtuals×Treasures (Jun 26 2026) https://finance.yahoo.com/markets/crypto/articles/ai-agents-expand-tokenized-stocks-095650150.html · Yield.xyz AgentKit on x402

## Method & honesty notes

- **Ontario claims are byte-verified.** The raw `openapi.json` (89KB),
  `x402-trust.json`, `x402.json`, and `mcp.json` were fetched with `curl` and
  parsed locally — *not* via a model-generated page summary. An earlier draft
  trusted such a summary and **missed the `/api/agent/can-pay` endpoint** entirely
  (see the Correction box above); re-pulling the bytes caught it. Lesson: for
  decision-grade competitive facts, read the raw artifact, not a summary of it.
- "Confirmed absent" here means the **literal string** is absent from the full
  fetched spec — a vendor could still compute something internally and not expose
  it. For Ontario it's corroborated by the verbatim `agent_rule` and the full
  byte-scan, not a single silent omission.
- Other rows (MolTrust, Larkinsh, Crest, AnChain, etc.) rest on **repo listings /
  vendor blogs**, not byte-level reads — lower confidence, as marked in the table.
- **Not** exhaustive; weighted to the x402 niche. Stealth/very recent entrants may
  be missing. **No paid API calls were made** — all from public manifests + free
  endpoints.

### Sources
- awesome-x402: https://github.com/xpaysh/awesome-x402 ·
  https://github.com/Merit-Systems/awesome-x402
- Ontario Protocol: https://ontarioprotocol.com (service descriptor,
  `/.well-known/openapi.json`, `/.well-known/x402-trust.json`)
- ERC-8004: https://eips.ethereum.org/EIPS/eip-8004
- AnChain.AI x402: https://www.anchain.ai/blog/x402
- AgentZone: https://agentzone.fun/
- Crest: https://data.crestsystems.ai
