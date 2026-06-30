# Tier-2 (Treasury/AP) — Session Handoff

Kickoff context for a session dedicated to **Blackwall's most-profitable niche:
pre-payout compliance screening for autonomous treasury / AP**. Self-contained —
a fresh session has none of the build history. Snapshot: 2026-06-30.

> **The decision is already made.** Of the four Tier-2 niches (AA guard module,
> A2A commerce, DePIN/M2M, treasury/AP), **treasury/AP was chosen as most
> profitable** — it's the one niche with an existing compliance/AML *budget line*
> and high willingness-to-pay. Don't re-litigate the choice; execute it.

## Read first
- **`docs/TREASURY_AP.md`** — the full go-to-market + technical integration shape
  (wedge, ICP, verdict→payout gate, payload mapping, pricing, positioning, gaps).
  This is the spec. Start here.
- `BLACKWALL.md`, `PRICING.md`, `COMPETITIVE.md` — engine, pricing posture, the
  "superset of free KYT" positioning.

## What Blackwall is (one line)
A pre-signature payment-risk verdict — **GO / HOLD / STOP** before a payment is
signed — from **behavioral counterparty reputation + price-anomaly + OFAC
sanctions**. Verdict-only, never custody. Live on Base mainnet. Stdlib-only,
deterministic, adversarially audited (327 tests).

## The job for this session
Make treasury/AP real, in this order:
1. **One-page compliance brief** — "pre-payout OFAC + counterparty-risk screening,
   one call, signed receipts, deterministic & auditable." Sales-ready.
2. **Pilot adapter** — a thin wrapper over the existing `forecast()` call that an
   AP system invokes at the *approve-&-release* step, returning GO / HOLD(→human)
   / STOP. **No engine changes** — the payload already maps (`counterparty`=payee,
   `amount/asset/chain`=payout, `resource`=invoice id). Reuse the **ElizaOS
   guardrail's human-confirmation flow** (fail-closed, same-origin, strictest-wins)
   for the HOLD→human path.
3. **Design-partner target list** — stablecoin payout/AP platforms and "AI CFO" /
   agentic-finance tools that already release payments autonomously.

## ⚠️ Blocking cross-cutting issue (surfaces everywhere)
There are **two Blackwall backends with different schemas**, and an AP buyer must
get the *same verdict regardless of door*:
- **`agent-egress-proxy`** (this repo) — x402-billed, returns
  `{verdict, reasons, signals, receipt_id}`. This is where OFAC, compliance-free
  billing, and the integrity fixes landed.
- **`blackwalltier.com`** — API-key SaaS (`bw_live_xxx`), returns
  `{recommendation, gate, confirmation, hard_blocks}`. This is what the **ElizaOS
  guardrail** (`blackwall-eliza-guardrail` repo) actually calls.

**Before selling a compliance story, pin ONE canonical engine** and make the other
a thin transport, or document clearly that they're distinct. This keeps surfacing
(Eliza review, Treasury doc) — treat it as the first real task if the session has
scope over both backends.

## Provable status (don't overclaim)
- Live on Base mainnet; real on-chain settlement (tx `0x9ddec8…`).
- OFAC screening ON (93-address snapshot; descriptor advertises
  `screening:["sanctions-ofac"]`; verified live). Snapshot is point-in-time —
  wire a refresh cadence before selling the OFAC claim.
- Reputation demo data for `0x1111…` is **seed data, not real history** — never
  cite it as a track record.

## Constraints (carry these in)
- Code changes for `agent-egress-proxy` go on branch
  `claude/blackwall-x402-integration-j3rdab`; don't open PRs unless asked.
- The Eliza plugin and `blackwalltier.com` live in **separate repos** — needs the
  session's GitHub scope to include them, or work from pasted files.
- Never paste private keys/secrets; no invented adoption numbers; deterministic/
  audited is a selling point — lead with it for compliance buyers.

## Kickoff prompt to paste into the new session
> I'm starting a session focused on **Blackwall for autonomous treasury / AP** —
> pre-payout OFAC + counterparty-risk + price-fairness screening, the
> highest-margin Tier-2 niche. Read `docs/TREASURY_AP.md` and
> `docs/TIER2_HANDOFF.md` for full context, then help me execute: (1) a one-page
> compliance brief, (2) a thin pilot adapter over the existing forecast() call for
> the approve-&-release step reusing the ElizaOS confirmation HITL, (3) a
> design-partner target list. First, flag whether the two-backend reconciliation
> needs resolving before any of it. Keep it honest about gaps; deterministic/
> audited is the lead for compliance buyers.
