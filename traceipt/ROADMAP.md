# Traceipt roadmap — from live demo to category leader

The path from where Traceipt is now (a live, independently-verifiable mainnet
demo with zero customers) to being **the default, neutral compliance-receipt
layer for agent payments** — and what an acquirer would pay at each step.

**The category:** the neutral, independently-verifiable compliance-receipt /
proof layer for agent payments. **Leadership** = being the default everyone
verifies against, *and* anchoring the standard.

**The mental model:** [Sigstore](https://www.sigstore.dev/) / Certificate
Transparency / Let's Encrypt — neutral verification infrastructure that becomes
ubiquitous, standards-based, and nearly impossible to displace *because* it is
neutral. That is the endgame shape, not "another SaaS."

**The one honest through-line:** the value is created by **demand, not code.**
Every stage below gates on a demand milestone, so you cannot feature-build past a
stage without earning it. Traceipt has completed Stage 0; everything since has
been polishing Stage 0. The whole map hinges on the **Stage 1 gate.**

---

## The stages (each with the gate that must be TRUE to advance)

### Stage 0 — Prove it works ✅ (done)
- **Have:** live on Base mainnet, real settled payment, independently verifiable
  (CLI + browser + JS lib), on-chain Merkle anchor, immediate-seal durability,
  two-source settlement cross-check, hybrid post-quantum signatures.
- **Gate passed:** the tech is done enough. **Stop building core.**

### Stage 1 — Prove someone *wants* it ← the real starting line
- **Goal:** 5 design-partner conversations → 1–3 pilots.
- **Prove:** a *repeatable pain statement* in real users' words ("when my
  regulator/auditor asks me to prove a payment was screened, I have nothing").
- **Gate to advance:** one design partner using it on real traffic who says
  "I'd pay for this."
- **Trap:** building more features instead of having conversations. ~90% die
  here — us included, so far.

### Stage 2 — Prove they'll *pay*
- **Goal:** convert pilots → 5–10 paying customers, first **~$100k ARR**.
- **Prove:** a repeatable sales motion + a defined ICP that converts (not
  bespoke one-offs).
- **Gate:** >$100k ARR from a nameable customer profile.
- **Trap:** custom-building for each logo → no leverage, not a product.

### Stage 3 — Prove it's a *standard*
- **Goal:** get *in-path* and *in the standard*. Facilitators embed Traceipt
  receipts; engage `draft-hopley` / `draft-vauban` and land the receipt shape;
  listed in registries; inbound starts.
- **Prove:** ≥2 facilitators/platforms default to it; others reference the verify
  format.
- **Gate:** cited in the emerging spec **and** distribution is no longer 1:1
  sales.
- **Trap:** standards *theater* — a spec nobody implements. Adoption > authorship.

### Stage 4 — Category leader
- **Goal:** the default neutral receipt layer; **network effects** (more receipts
  → more verifiers → more trust → more receipts).
- **Prove:** "compliance receipt" ≈ "Traceipt" in the ecosystem; regulated
  buyers *require* it; it is the reference implementation of the standard.
- **Moat:** neutrality + ubiquity + being the standard. A neutral utility that
  everyone verifies against is extremely hard to displace.

---

## What runs across ALL stages

1. **Enterprise-readiness** (durable persistence → real sanctions feed → KMS keys
   → multi-tenant → SOC 2). **Pulled by each pilot's security checklist, never
   pushed speculatively.** A prospect's questionnaire *is* the backlog. See the
   tiers in the "enterprise-ready" notes.
2. **Standards engagement** — be at the table with Vauban/Hopley from Stage 1,
   not Stage 4. Bring "live, mainnet, on-chain-anchored reference implementation"
   as the credential.
3. **Distribution via facilitators** — the embed play (TrustBench, CDP, the
   AgentCore ecosystem, Bitwave) is how you get in-path without 1:1 selling.
4. **Guard neutrality with your life** — it *is* the moat (see the acquirer map).

## The three things that kill it (honest)

- **Demand never shows** (the compliance-receipt thesis is too early). →
  *De-risk at the Stage 1 gate before spending more.*
- **A hyperscaler/facilitator bundles it** (AgentCore/CDP adds receipts) →
  commoditization. *Defense: be the neutral, cross-platform standard first —
  platform-owned proof isn't neutral, and that's the wedge.*
- **The standard consolidates around a rival** (Vauban/Hopley). *Defense: be the
  live reference implementation in the room now.*

---

## The acquirer map (what a purchase looks like, by stage)

The number in an M&A offer is a function of **traction, not code** (Traceipt's
crypto is standards-based and replicable). Rough, heavily-caveated industry
patterns:

| Stage | What you'd have | Likely offer |
|---|---|---|
| **Today** | 0 customers, live demo | **Acqui-hire** — comp + equity, small/no asset premium. Often just a job offer, or $0 (they build it). |
| **Early** | 3–10 pilots, ~$100–500k ARR, 1–2 logos | **$3–15M** (cash + equity + earnout). Buying a head start + the team that shipped it. |
| **Real** | $1–3M ARR, regulated logos, "the neutral standard" | **$15–60M+** (~10–20× ARR when hot, plus a strategic premium). |
| **Category leader** | the default receipt layer across facilitators | Much more — but you may not sell. |

**The neutrality paradox (important):** a *platform* acquirer (AWS/AgentCore,
Coinbase/CDP) is a **weak** buyer for Traceipt's core, because owning the neutral
notary destroys the neutrality that makes it valuable — and they can build it.
The acquirers who pay a real strategic premium are the ones where neutrality
*survives*: **RegTech / compliance / audit incumbents** (Chainalysis, TRM, a
Big-4-adjacent, **Bitwave** — CFO-facing, needs exactly this proof layer). The
best "category leader" outcome may be **staying independent as critical infra**
(the Sigstore/Let's Encrypt path), an IPO-as-utility, or a compliance incumbent —
not a hyperscaler.

---

## Where we are, and the one move

Stage 0 is complete; every step since has polished it. **The entire map hinges
on the Stage 1 gate, and exactly one action opens it:** get Traceipt in front of
real users (the x402 Slack `#general` post → conversations → one design partner).
No commit advances the map. A pilot does.

_See also: `STRATEGY.md` (competitive map + thesis), `OUTREACH.md` (the
demand-first playbook + the audited posts), `B2B_TARGETING.md` (named targets)._
