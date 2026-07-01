# Blackwall for Autonomous Treasury & AP

The highest-margin home for the verdict engine: **screen every autonomous payout
for sanctions + counterparty risk + price fairness before it signs.** This is the
one niche with an *existing budget line* — compliance/AML — and procurement that
already writes checks for outbound-payment screening.

> **Why this niche (vs the crypto-native ones):** treasury/AP buyers have the
> highest willingness-to-pay. Firms already pay Chainalysis/TRM $10k–100k+/yr for
> sanctions screening. Blackwall is a **superset of that baseline** (OFAC *plus*
> behavioral counterparty reputation *plus* price-fairness), delivered as one
> pre-signature verdict. One mid-size customer outweighs thousands of micropayment
> fees. The trade-off is a longer, more enterprise sales cycle.

---

## The wedge

AI agents (and rules engines) are starting to **release payments autonomously** —
vendor invoices, payroll, payouts, treasury rebalances — increasingly in
stablecoins. The moment of risk is the **same as everywhere else Blackwall plays:
the instant before the payment is authorized.** Nothing changes about the engine;
only the caller changes.

What an AP/treasury buyer is afraid of, in order:
1. **Paying a sanctioned/blocked counterparty** (OFAC) — a *legal* exposure, not
   just a loss. This alone justifies the spend.
2. **Paying the wrong/compromised counterparty** — vendor-impersonation, BEC,
   swapped payout addresses.
3. **Paying the wrong *amount*** — invoice fraud, 10×/100× anomalies, duplicate
   payouts.

Blackwall answers all three in one call, pre-signature, verdict-only (never
custody). That maps exactly to the existing signals:
- **OFAC sanctions screening** — the compliance floor (now ON by default).
- **Behavioral counterparty reputation** — has this payee been paid reliably
  before, by distinct funded payers, with no disputes?
- **Price-anomaly** — is this amount in line with this counterparty's own history?

---

## Ideal customer profile (ICP)

- **Crypto-native fintechs / treasuries** paying vendors/contractors in USDC who
  *can't* afford or don't want enterprise KYT seats — Blackwall is the affordable
  superset.
- **Stablecoin AP / mass-payout platforms** (B2B payments, creator/affiliate
  payouts, marketplace settlements) that need per-payout screening as a feature.
- **Autonomous treasury agents** (the emerging "AI CFO" / agentic finance tools)
  that release payments with no human in the loop and need a guardrail.

Buyer: Head of Compliance / Controller / Eng lead on the payments team. Budget:
the AML/compliance line, or risk reduction on the payments product.

---

## Integration shape (verdict → payout gate)

One hook, in front of the signature/release step. No new engine.

```
invoice/payout ready
      │
      ▼
  Blackwall forecast  ──►  GO       → release the payment
  (payee, amount,         HOLD/CAUTION → route to a human approver
   asset, chain,          STOP / sanctioned → block, log, alert compliance
   resource=invoice_id)
```

- **Where it hooks:** the AP system's "approve & release" transition, or the
  agent's pre-sign step. Same place the x402 client or the ElizaOS guardrail sits.
- **Two transports, same verdict:** x402 (pay-per-call in USDC) for crypto-native
  callers, or the API-key SaaS (`blackwalltier.com`) for teams that don't want a
  wallet. Keep both backends returning the *same* verdict (see the reconciliation
  note below).
- **Human-in-the-loop:** reuse the **confirmation flow already built in the
  ElizaOS guardrail** — a CAUTION/CONFIRM verdict opens a human-approval request
  with a pollable `poll_url`; large or anomalous payouts fail *closed* until a
  human approves. That HITL gate is exactly what an AP approval workflow wants;
  it's already designed and security-reviewed (strictest-wins, same-origin,
  fail-closed). Lift it.
- **Auditability:** every verdict returns a signed `receipt_id`. That's the audit
  trail compliance needs — "we screened this payout, here's the proof." Pair with
  the EAS attestation roadmap item (proofs only, never the corpus) for
  publicly-verifiable receipts.

### Payload mapping
AP payments map cleanly onto the existing request:
- `counterparty` = the payee address
- `amount` / `asset` / `chain` = the payout
- `payer` = the treasury/agent wallet
- `resource` = the invoice/PO id — drives **per-invoice-class** price comparison:
  a large invoice is priced against the vendor's *same-class* history (once it has
  ≥3 distinct-payer same-class observations via the ledger flywheel), not the
  pooled full history — so a legit first large invoice isn't a false gouge. Falls
  back to pooled when there's not enough same-class evidence, and on-chain-only
  histories (reputation store) always pool. Cross-counterparty **peer-group**
  comparison is still a roadmap item.

No engine changes required to pilot.

---

## Pricing posture

Different from the agent-micropayment world — here the value-per-call is high and
the volume is low, so **don't price like x402 micropayments.** Options, roughly in
order of fit:

1. **Per-payout screened** — a few cents to a few dollars per high-value payout,
   scaling with amount-at-risk (the value-pricing policy already does this).
2. **SaaS seats / tiers** — monthly per-team, with a screened-volume cap. Matches
   how compliance tools are bought.
3. **Free under a low at-risk threshold** (already the default) — keeps small
   payouts and the sanctions safety check free, monetizes the high-stakes ones.

The compliance framing supports a **much higher price than the agent-guardrail
framing** for the same call. Sell the OFAC + audit-trail outcome, not the verdict
mechanics.

---

## Why Blackwall wins here (positioning)

- **Superset of free KYT, in one call** — OFAC *plus* the signals KYT doesn't have
  (behavioral payee reputation, price-fairness), pre-signature.
- **Deterministic, stdlib-only, adversarially audited** — for a compliance buyer
  this is a *feature*: no model drift, no nondeterminism, an auditable decision
  path, no dependency supply-chain surface. Lead with it.
- **Verdict-only, never custody** — drops in front of *any* payment rail without
  taking on money-transmitter risk. Complements the rails, doesn't compete.

---

## Honest gaps / what a pilot needs

- **No new engine** — but the *go-to-market* is enterprise, so it needs trust
  signals: a one-page security/compliance brief, the audit story, and ideally one
  reference design partner.
- **Two-backend reconciliation (blocking):** the ElizaOS guardrail calls
  `blackwalltier.com` (API-key, `recommendation/gate/confirmation/hard_blocks`
  schema); this repo's `agent-egress-proxy` is x402 with a `verdict/reasons/signals`
  schema. **An AP buyer must get the same verdict regardless of door.** Pin one
  canonical engine and make the other a thin transport, or the "one verdict
  engine" story breaks under scrutiny.
- **Sanctions list freshness** — the OFAC snapshot is point-in-time; for a
  compliance product, wire the periodic refresh (and document the cadence) before
  selling the OFAC claim.
- **Invoice price baseline** — price-anomaly compares a payee to *its own* history;
  for AP it's worth adding the peer-group cross-check (roadmap) so a first-time
  vendor with no history isn't a blind spot.

---

## First concrete moves

1. **One-page compliance brief** — "pre-payout OFAC + counterparty-risk screening,
   one call, signed receipts, deterministic & auditable."
2. **A pilot adapter** — a thin wrapper over the existing forecast call that an AP
   system calls at the approve-&-release step, returning GO / HOLD(→human) / STOP,
   reusing the ElizaOS confirmation HITL.
3. **Find one design partner** — a stablecoin payout/AP platform or an "AI CFO"
   tool that already releases payments autonomously.
