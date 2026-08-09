# Sprint 0 — desk follow-ups (true per-check cost; competitor signal mix)

**Method:** deep-research harness, 5 angles, 19 sources, 81 claims → 25
adversarially verified (24 confirmed, 1 killed). The harness's auto-synthesis
returned a degenerate placeholder; findings below were **reconstructed from the
verified-claim transcripts** — confidence rests on the per-claim votes cited.

---

## Follow-up 1 — TRUE per-check cost → **YES, comfortably sub-cent**

The Sprint 0 "27-CU generic average" was wrong for Blackwall's queries. Actual,
from **primary Alchemy docs** (compute-unit-costs reference + the transfers-API
OpenRPC `x-compute-units` field):

| Method (Blackwall's real query) | CU | $ at $0.45/1M CU |
|---|---|---|
| `alchemy_getAssetTransfers` (inbound USDC for a counterparty) | **120** | $0.000054 |
| `eth_getTransactionCount` (optional tx-count) | 20 | $0.000009 |
| **Per counterparty refresh** | ~140 | **~$0.000063** |

[getAssetTransfers=120 CU: verified 3-0 against two primary Alchemy pages; the
"150 CU" from AI-summary aggregators was refuted as unreliable. eth_getLogs=60 CU
(3-0). $0.45/1M CU PAYG: 3-0.]

**Per-check cost:** that ~$0.000063 is per *counterparty refresh*, amortized
across every check against that counterparty in the cache window (hot path is a
sub-ms SQLite read, ≈free). **Even at the worst case — reuse rate of 1, no
amortization — per-check data cost is ≈ $0.00006:**

- **~16× cheaper than the $0.001 Coinbase CDP settle fee**
- **~160× under one cent**
- With realistic amortization (many checks per counterparty per refresh),
  effectively free.

Alternatives priced but unnecessary for the verdict: Goldsky (Mirror/Turbo
pipeline bandwidth), Dune (API credits), self-indexing (~$ for a node) — all
viable, but the Alchemy path alone is already deeply sub-cent.

**Verdict: unit economics are NOT the constraint.** COGS is a rounding error
against the fee. The competitive pressure from the free baseline is about
**willingness-to-pay**, not cost-to-serve — exactly where Sprint 1 should focus.

---

## Follow-up 2 — Is Blackwall's signal mix uncovered? → **YES, genuinely uncovered**

Drilling into each rival's *actual* signals (primary sources / repos):

| Vendor | Actual signals | Settlement/dispute reputation? | Price-anomaly vs norm? |
|---|---|---|---|
| **CDP facilitator** (free) | signature/balance/replay + **KYT sanctions** | ❌ | ❌ |
| **x402-secure** (t54) | agent-side behavioral/intent (prompt-injection), merchant **identity**, **transaction-logic** anomalies (hidden auto-renewal, spec mismatch) → Low/Med/High | ❌ (3-0) | ❌ — tx-logic ≠ price-vs-norm (3-0) |
| **AgentRadar** (vvpro) | Identity .25 / **Reputation .15** / Health .20 / **Scam .20** / Fidelity .10 / External .10 | ❌ — "Reputation" = **ERC-8004 registry attestations** (feedback, interaction count), **not observed USDC settlement/dispute history** (3-0) | ❌ (3-0) |
| **AnChain.AI** | AML / sanctions / fund-flow tracing | ❌ | ❌ |
| **Blackwall** | **observed on-chain settlement/dispute reputation + price-anomaly-vs-own-norm** | ✅ | ✅ |

**Key distinctions the evidence draws sharply:**
- AgentRadar *has* a "Reputation" signal — but it's **ERC-8004 registry
  attestations** (canonical feedback / interaction count / on-chain reciprocity),
  a fundamentally different thing from Blackwall's **observed settlement &
  dispute outcomes**. Different data, different failure modes.
- x402-secure detects *transaction-logic* anomalies (auto-renewal, spec
  mismatch) but **not price-anomaly relative to a counterparty's own pricing
  norm** — and **no** settlement/dispute reputation.
- The contrarian angle corroborated independently: *"dispute history and
  price-anomaly are under-addressed dimensions in the published literature, not
  just in vendor marketing."* [2-1]

**Verdict: the "crack" is real.** The pre-signature niche is crowded, but
Blackwall's *specific* pair — behavioral settlement/dispute reputation +
price-vs-norm anomaly — is the one corner no live competitor occupies.

---

## Caveats

- Competitor signals are **vendor self-description / public repos** — proves what
  they *publish*, not adoption, efficacy, or that they won't add these signals
  next quarter. "Uncovered" ≠ "uncontestable."
- The cost model assumes a paid indexer (Alchemy) for background ingest; that's
  the realistic production path and it's clearly sub-cent. Self-index/Goldsky/Dune
  numbers were gathered but not needed to clear the bar.
- One claim killed (0-3): "AgentRadar's core function is scam-DB lookup, not
  settlement reputation" — refuted, i.e. AgentRadar is more than a scam DB; but
  the verified six-signal breakdown still shows it does **not** do Blackwall's
  settlement/dispute or price-anomaly signal.

## Net effect on the bet

Both desk follow-ups land **in Blackwall's favor**, and they narrow the real risk
to one place:

- **Cost: solved** — sub-cent with huge margin; not a constraint.
- **Differentiation: real** — the exact signal mix is uncovered.
- **The remaining risk is entirely A1 (is real recurring volume there yet) and
  A3 (will anyone PAY for *this signal* over free KYT).** Those are the Sprint 1
  questions — and now they're the *only* open ones. Desk research has taken cost
  and differentiation off the table.
