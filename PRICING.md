# Blackwall pricing posture

**Decision (2026-06-29): value-aligned, freemium-on-stakes. Safety is always free.**

## Why (the Ontario finding)

The nearest competitor, Ontario Protocol, exposes a **free** pre-payment
`allow/deny` verdict (`/api/agent/can-pay`). See `COMPETITIVE.md`. So the bare
verdict is commoditizing — you cannot durably charge for "a verdict exists."

But willingness-to-pay tracks **stakes**, not the verdict itself:

| Payment at risk | Value of a verdict | Right price |
|---|---|---|
| $0.50 micropayment | ~cents | **free** — charging adds friction nobody pays |
| $5,000 to an unknown counterparty | thousands (loss avoided) | **$0.10 is trivial**, and a free *shallow* verdict is no substitute |

So: **don't price the commodity; price the stakes and the depth.**

## The model

Blackwall uses **value-aligned pricing** (`x402.PricingPolicy`): free under a
threshold, then a small fraction of the amount-at-risk, clamped.

- **FREE tier** — the full GO/HOLD/STOP verdict for low-stakes payments
  (under `free_below`, default $1). This **includes**:
  - **OFAC sanctions screening** (safety — never withheld; this is the
    "superset of free" baseline, see `sanctions.py`),
  - the behavioral reputation + price-anomaly signals,
  - the signed **receipt** (so free users can report outcomes and *grow the
    reputation moat* — see `ledger.py`).
- **PAID tier** — kicks in with **stakes** (high-value payments) and unlocks
  **assurance depth**, not safety:
  - on-chain **settlement confirmation** / payer-bound trust
    (`settlement_watch.py`),
  - **sessions** / rate-limits / SLA for high-frequency agents,
  - (roadmap) peer-group / distinct-payer-weighted price analysis.

Fee: `bps` of the amount (default 10 bps = 0.1%), clamped to
`[min_fee $0.001, max_fee $0.10]`. A $5k payment is capped at $0.10; a $0.50
payment is free.

### Verified behavior (2026-06-29)

Running `--value-pricing --sanctions <list>`:

- `$0.50` payment → `HTTP 200`, full verdict, **no payment required**.
- `$50` payment → `HTTP 402` challenge (pay to proceed).
- Sanctioned counterparty at `$0.50` (a *free* call) → still `STOP`,
  *"counterparty is on a sanctions list"* — **safety holds on the free tier.**

## Prepaid credits / committed volume

Per-call value-pricing is the default, but two buyer types want to **pay ahead**
instead of per-call: **enterprise / treasury-AP** (predictable billing, one
procurement cycle, a spend cap they control) and **high-frequency agents** (skip
the per-call signing/latency). The primitive already exists — this just formalizes
it as a purchasable balance.

**Two forms, same idea (buy N, burn one per verdict):**

- **Off-chain balance (recommended, SaaS side).** A prepaid counter: buy credits
  (card or USDC), burn one per forecast, top up when low. Simple, no gas,
  procurement-friendly — how every API company bills. Lives on the API-key side
  (`blackwalltier.com` freemium keys), not the x402 path.
- **x402 session (the crypto-native equivalent, already built).** `x402.py` /
  `/v1/session` — "fund once, many checks": pay once, get a **reusable session
  token** good for a budget of verdicts. This *is* prepaid mode for on-chain
  callers; document it as such rather than building a second mechanism.

**The load-bearing rule: credits are NON-TRANSFERABLE — a balance, not an asset.**
A prepaid balance you can't sell to another party is obviously just prepaid service
(a gift-card, not a coin). The moment credits become transferable/tradeable you
re-enter securities/AML territory and invite gaming — the same reason receipts are
**not** tokenized (see `ROADMAP.md` — attest the *proof*, never mint a tradeable
token). Keep it a billing model.

**Do NOT** build an on-chain credit *token* (transferable ERC-20) unless a specific
customer needs on-chain composability — it's more risk (gas, contract, "is it a
security?") than reward versus an off-chain balance plus the existing x402 session.

**How it composes with value-pricing:** credits are the *packaging*, value-pricing
is the *rate*. A credit is priced at (or discounted from) the value-aligned fee for
its stakes tier; committed-volume buyers get a discount for prepaying. Safety
(OFAC) stays free regardless — a sanctioned counterparty STOPs whether or not a
credit is spent.

## What we deliberately do NOT do

- **Do not gate sanctions/safety behind payment.** A free user getting a `GO` on
  a sanctioned counterparty would be a compliance hole and would contradict the
  "superset of free" positioning. Safety signals are free, always.
- **Do not gate the receipt.** Free outcomes feed the moat; we want them.
- **Do not charge a flat per-call fee.** That loses every low-stakes call to a
  free competitor for no gain.

## Tuning knobs (env / CLI)

| Knob | Default | Note |
|---|---|---|
| `BLACKWALL_VALUE_PRICING` | off (flat $0.001) | **on** for public deploys (this posture) |
| `BLACKWALL_FREE_BELOW` | `1.00` | raise (e.g. `10.00`) to be more generous early for adoption; lower later |
| `BLACKWALL_PRICE_BPS` | `10` | 0.1% of amount-at-risk |
| `BLACKWALL_MIN_FEE` / `BLACKWALL_MAX_FEE` | `0.001` / `0.10` | fee clamp |

**Early-stage recommendation:** start **generous** (`free_below` high, e.g. $10)
to win adoption and accumulate counterparty history while the category has no data
moat (Ontario's `total_reports: 0`), then tighten as the flywheel fills.

## Where this goes next

This is posture **A → D** from the strategy discussion: freemium-on-stakes now,
evolving toward **data-as-product** (reputation feeds / settlement attestations
priced off the accumulated history) once the moat compounds. The verdict stays
cheap-or-free; the **data and assurance** are what's sold.
