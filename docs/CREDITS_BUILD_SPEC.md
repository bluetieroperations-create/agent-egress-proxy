# Build spec — prepaid credits (blackwalltier.com)

Implementation checklist for the off-chain prepaid credit balance. **This is built
on `blackwalltier.com` (the API-key SaaS), not `agent-egress-proxy`.** Design
rationale lives in `PRICING.md` → "Prepaid credits / committed volume". Keep it a
**billing feature, not a token.**

## Core rule (non-negotiable)
Credits are a **non-transferable account balance** — a gift-card, not a coin.
No transfer/send between accounts, no on-chain token. Transferability = securities/
AML risk + gaming. Enforce at the data layer (balance is owned by an account, not
a wallet you can move).

## Data model
- `account` — the API-key owner (already exists for `bw_live_xxx` keys).
- `credit_balance` — integer per account. **Never negative.**
- `credit_ledger` — append-only rows: `{account_id, delta, reason, verdict_receipt_id?, created_at}`
  (`reason` ∈ `purchase` | `topup` | `burn` | `refund` | `adjustment`). The ledger
  is the source of truth; `credit_balance` is a cached sum (or derive on read).

## Endpoints
- `POST /v1/credits/purchase` — buy N credits (Stripe card or USDC). On payment
  confirmation, append a `purchase` row, increment balance. Idempotent on the
  payment/session id (no double-credit on webhook retry).
- `GET  /v1/credits/balance` — current balance + recent ledger.
- Existing forecast endpoint — **burn one credit per paid verdict** (see below).

## Burn logic (the important part)
On each forecast, decide the charge path in this order:
1. **OFAC / safety STOP → FREE.** Never burn a credit to deliver a sanctions
   block (mirrors `is_compliance_free` in `agent-egress-proxy`). Return the STOP,
   no debit.
2. **Below `free_below` (value-pricing free tier) → FREE.** No burn.
3. **Paid tier (stakes ≥ threshold):**
   - If `credit_balance > 0` → **burn 1 credit** (append `burn` row atomically),
     serve the verdict. Return remaining balance in a response header.
   - If `credit_balance == 0` → fall back to the normal per-call charge
     (x402 402 challenge / card), OR `402 { reason: "no_credits" }` if the account
     is credits-only.

**Atomicity:** the balance check + decrement must be a single atomic operation
(DB transaction / `UPDATE ... SET balance = balance - 1 WHERE balance > 0`), or
concurrent forecasts double-spend a credit. Reject on 0 rows affected.

## Pricing (from PRICING.md)
A credit is priced at (or discounted from) the value-aligned fee for its stakes
tier. Committed-volume buyers get a prepay discount. Credits are the *packaging*,
value-pricing is the *rate*. Don't invent a separate rate card.

## Edge cases / must-handle
- **Refund on engine error:** if the verdict fails *after* a burn (500, timeout),
  refund the credit (append `refund` row). Never charge for a non-answer.
- **Webhook idempotency:** purchase confirmations retry — key on payment id.
- **Balance never negative:** enforced by the atomic decrement, not app logic.
- **Free calls don't burn:** low-stakes + all safety STOPs are free even with a
  positive balance.
- **Expiry (optional):** if credits expire, make it explicit and generous; expiry
  is a support/goodwill liability — default to non-expiring.

## Reconcile with what exists
- **x402 session token** (`agent-egress-proxy` `/v1/session`) is the crypto-native
  prepaid equivalent — "fund once, many checks." Don't duplicate it; treat
  off-chain credits and x402 sessions as **two transports of the same prepaid
  idea**, and keep the burn/skip rules identical (safety free, low-stakes free).
- **Verdict parity:** whichever backend actually scores, the credit-burn decision
  must sit *in front of* the same verdict engine — see the two-backend
  reconciliation note in `docs/TIER2_HANDOFF.md`.

## Definition of done
- Buy → balance up; forecast (paid) → balance down by 1; safety/low-stakes →
  no change; engine error → refunded; concurrent forecasts never double-spend;
  balance never < 0; credits cannot be transferred between accounts.
