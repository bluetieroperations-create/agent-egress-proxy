# Blackwall — business validation pass

> The engineering is done and audited. None of it proves anyone will **pay**.
> This is the HANDOFF §4 work the conditional spec rests on but that was never
> run. The point of this pass is to find the **"no" cheaply**, not to confirm a
> "yes." Kill criteria are first-class.

## The one question

**Will agent operators pay a per-check fee for a pre-signature payment verdict,
over the FREE facilitator baseline — at enough volume to be a business — and can
Blackwall defensibly own that niche?**

## Load-bearing assumptions (ranked by risk = load-bearing × uncertainty)

Test the riskiest, cheapest-to-test first. Each has an explicit kill criterion.

| # | Assumption (falsifiable) | Test | Signal of life ✅ / Kill ❌ | Owner |
|---|--------------------------|------|----------------------------|-------|
| **A1** | x402 agent-payment **volume is real & growing** | Desk: facilitator tx counts, x402 dashboards / Dune, awesome-x402 adoption | ✅ thousands+/day, rising · ❌ negligible/flat → too early | Desk |
| **A2** | The **free baseline doesn't already solve it** — facilitators don't bundle equivalent reputation/price-anomaly verdicts; **x402-secure** doesn't own the niche | Desk: read CDP / x402.org / x402.rs facilitator docs, x402-secure, sanctions/risk tools | ✅ niche open · ❌ a facilitator gives equivalent risk free, or x402-secure owns it → compete-or-die / pivot-to-partner | Desk |
| **A3** | **Willingness to pay** over free — value is "obviously worth it" | 8–12 customer interviews + a fake-door pricing page | ✅ ≥⅓ "yes, here's what I'd pay" + a reason · ❌ "why pay, facilitator is free?" → core thesis dead | **Founder** |
| **A4** | The **moat is defensible** — reputation data accumulates (✅ proven) *and* is hard to replicate statelessly; the signal is decision-changing | Strategic analysis + A2 evidence | ✅ stateful, decision-changing · ❌ degrades to commodity check a facilitator bolts on | Mixed |
| **A5** | **Distribution reaches agents** — MCP listing + awesome-x402 + facilitator partnership pull users | Ship listing (✅ built), measure pickup; 1 facilitator convo | ✅ inbound / partner bite · ❌ silence → GTM problem | Founder |
| **A6** | **Unit economics close** — sub-cent × plausible volume − costs > viable | Model once A1 volume known | ✅ path to meaningful revenue · ❌ needs implausible volume | Desk |

## Sequenced gates (fail fast, cheap → expensive)

**Sprint 0 — Desk research (days).** A1 + A2 + A6 skeleton. = HANDOFF §4
"competitive map pulled" + "x402-secure read."
→ **GATE:** if A1 (volume) or A2 (free baseline / x402-secure owns it) fails →
**STOP before any customer interviews.** Don't validate demand for a market
that's too early or already served free.

**Sprint 1 — Customer discovery (~2 weeks, founder-only).** A3 willingness-to-pay
+ A4 signal value. = HANDOFF §4 "willingness-to-pay sanity-checked" — the gate
the whole spec is conditional on. See `docs/customer-discovery.md`.
→ **GATE:** A3 is the true kill switch. No willingness over free → pivot (e.g.
sanctions-screening enterprise upsell, or the refund/dispute product, spec §7) or
kill.

**Sprint 2 — Distribution + economics (parallel-ish).** A5 listing pickup + A6
full model.
→ **GATE:** go / no-go on building toward revenue.

## Pivots already on the shelf (if A3 fails)

The spec names adjacent, less-crowded products to fall back to rather than dying:
- **Sanctions screening as an enterprise upsell** (spec §7) — a STOP signal here,
  a standalone compliance product there.
- **Refund / dispute filing** (spec §7) — adjacent, uncrowded, different product.
- **Reputation-as-data** — sell the accumulated counterparty reputation feed to
  facilitators/wallets rather than per-check verdicts.

## What code already settled (so validation isn't muddied by "is it possible")

- Moat is **technically buildable**: indexed reputation store, 0.093 ms hot-path
  lookup, trustless payer-bound settlement confirmation.
- Billing **works against a real facilitator** (Base Sepolia dry-run).
- The fee path, MCP distribution, and service listing all **exist**.

So validation is purely a **market** question now, not a feasibility one — which
is the cleanest possible position to run customer discovery from.

## Honest note on sequence

This pass should have run *before* the build (the spec says so). It didn't. That
means the build was a bet placed ahead of validation. The upside: if Sprint 0–1
clear, there's nothing left between validation and revenue. The risk being
retired now is the one that was skipped — so treat A1/A2/A3 kill criteria as real,
not formalities.
