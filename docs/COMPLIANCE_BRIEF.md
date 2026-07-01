# Blackwall — pre-payout screening for autonomous payments

**One line:** screen every autonomous payout for **sanctions + counterparty risk +
price fairness** in one call, *before* it's signed — GO / HOLD / BLOCK, with a
signed receipt for the audit trail.

---

## The problem

Payments are increasingly released **autonomously** — by agents, rules engines, or
scheduled treasury jobs, often in stablecoins, with no human in the loop at the
moment of release. The risk lands at one instant: **the payout is about to sign.**
Three exposures, in order of severity:

1. **Paying a sanctioned/blocked address** — an OFAC violation is a *legal*
   exposure, not just a loss.
2. **Paying the wrong or compromised counterparty** — vendor impersonation, BEC,
   a swapped payout address.
3. **Paying the wrong amount** — invoice fraud, a 10×/100× anomaly, a duplicate.

## What Blackwall does

A single pre-signature call returns a verdict from three signals folded together:

- **OFAC sanctions screening** — the counterparty is checked against the published
  OFAC digital-currency address list; a hit is a hard **BLOCK**.
- **Behavioral counterparty reputation** — has this payee been paid reliably
  before, by *distinct* funded payers, with no disputes? (Chain-confirmed outcomes,
  not self-declared attestations.)
- **Price fairness** — is this amount in line with the counterparty's own payment
  history? A large deviation flags a possible wrong-amount / invoice fraud.

**Verdict-only, never custody.** Blackwall returns a decision + a signed
`receipt_id`; it never touches funds. It drops in front of *any* payment rail
without adding money-transmitter risk.

## How it fits your approve-&-release flow

```
payout ready ─►  Blackwall  ─►  RELEASE   auto-approve
                              ─►  REVIEW    route to a human approver
                              ─►  BLOCK     sanctioned / anomalous — do not release
```

- One hook at the "approve & release" step (the `ap_gate` adapter maps a payout to
  the verdict — payee, amount, asset, chain, invoice id).
- **REVIEW** carries a human-confirmation handoff (approve/reject) — the same
  fail-closed confirmation flow used in the agent integration.
- Every verdict returns a **signed receipt** — your provable record that the payout
  was screened, what the decision was, and when. That's the audit trail.

## Why Blackwall vs. a KYT seat

- **Superset of the free/KYT baseline, in one call.** KYT tools screen sanctions;
  Blackwall does that **plus** counterparty payment behavior **plus** price
  fairness, pre-signature, in a single verdict — at a fraction of enterprise KYT
  pricing.
- **Deterministic and auditable.** The decision is transparent math, not a model —
  no drift, no black box, an explainable path from signals to verdict. Stdlib-only
  (no third-party dependency surface). For a compliance function, that's a feature.

## What it is — and is NOT (honest)

- It **is** an automated *pre-payout screen* and an audit-trail generator.
- It is **not** legal advice, and it is **not** a full AML/KYC program — it's the
  screening control that sits inside one. You own your compliance program.
- The sanctions list is the **published OFAC digital-currency list**, refreshed on
  a cadence (auto-refresh on service restart; verify freshness for your
  requirements). Non-EVM designations are out of scope.
- **Config note:** out of the box the engine escalates *any* payout over a low
  threshold to **REVIEW** (human). That's a safe default; for treasury use you'll
  want the auto-release threshold tuned to your risk appetite so routine, in-line
  payouts to trusted vendors can auto-release while anomalies still escalate.

## Pilot ask

A design partner that already releases payouts autonomously (a stablecoin
AP/payout platform or an agentic-finance tool). We wire `ap_gate` into your
approve-&-release step in **observe mode** (score + log, never block) for a couple
of weeks, you see the verdicts on your real traffic, then switch to **enforce**.
