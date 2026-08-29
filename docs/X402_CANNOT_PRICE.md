# x402 transmits everything needed to SIGN a payment, and nothing needed to JUDGE it

Measured live, 2026-08-29, from **298 `accepts[]` entries** across the payee
directory.

## The finding

| Field | Present |
|---|---|
| `scheme` | 100% |
| `network` | 100% |
| `asset` (contract address) | 100% |
| `payTo` | 100% |
| `extra` (EIP-712 domain) | 100% |
| `amount` (ATOMIC units) | 96% |
| **`decimals`** | **0 of 298 — never** |

`extra` carries `name`, `version`, `chainId` — the EIP-712 domain needed to
*sign*. Only 4 of 210 sampled `extra` objects contained anything decimals-like.

**So a client receives an atomic amount and the asset's address, and nothing that
says what the amount means.** `10000` is $0.01 in 6-decimal USDC and $0.00000001
in an 18-decimal stablecoin. The protocol does not distinguish them.

## Why this is not a bug in x402

It is a scope decision, and a defensible one. To **execute** the payment you do
not need decimals: the atomic value is signed verbatim into the EIP-3009
authorization. Everything required to pay correctly is present.

Decimals are only needed to **interpret** — to answer "is this a lot?" That
question is outside the protocol's job.

The consequence is what matters:

> **An x402 client can execute a payment flawlessly while having no idea whether
> it just spent one cent or ten thousand dollars.**

## Why it bites in practice

**33% of live entries use a non-USDC-6dp asset** — 97 of 298, across 32 distinct
assets. Among them Solana USDC (a base58 mint, not an EVM address at all) and BSC
stablecoins whose `extra.name` gives them away: *"World Liberty Financial USD"*,
*"United Stables"*. Those are **18 decimals**, on-chain verified.

Meanwhile the ecosystem's own guidance is uniform: *"USDC uses 6 decimals, not
18"*, and reference client code reads `parseUnits(terms.amount, 6)`. That is
correct for two thirds of live entries and silently wrong by a factor of 10^12
for a real slice of the rest.

## What it explains

`docs/COMPETITOR_COVERAGE.md` measured that of **913 counterparty-screening tools
in the MCP corpus, none scores an amount against the payee's own settled
history.** This is why.

Nobody built amount-checking because **the protocol never hands you the one field
you would need to start.** To judge a price you must resolve decimals yourself —
from a table, or on-chain — before the comparison is even meaningful.

That is exactly what `payload_sim.resolve_decimals` and `token_decimals.py` do,
and why the three bypasses in `docs/DECIMALS_AUDIT.md` existed in our own code
until they were fixed. We were making the same assumption as everyone else.

## What we do with this — kept, not published

This is an information advantage and is treated as one. The finding is not the
deliverable; the capability is.

**Blackwall can transact with the third of the ecosystem everyone else
mis-reads.** Measured on live entries, correct decimals resolution changes the
verdict on real payments to real companies:

| Payee | Charges | Naive 6dp read | Verdict |
|---|---|---|---|
| `pro-api.coinmarketcap.com` | **$0.01** | $10,000,000,000 | **STOP** |
| `api.nansen.ai` | **$0.01** | $10,000,000,000 | **STOP** |

Same payment, correctly scaled: **GO**, "within 1.00x of the counterparty's
median". Both are 18-decimal BSC stablecoins.

That is the whole advantage in one line: **a client assuming 6 decimals refuses a
one-cent API call from CoinMarketCap.** Blackwall pays it, and still catches a
genuine overcharge, because it resolves the decimals first.

Pinned by `test_token_decimals.TestCrossAssetPricingCapability`, which asserts
both directions -- the correct read clears and the naive read STOPs -- so the
capability cannot regress into agreement without a test failing.

## The honest limits

- **Sampled from the payee directory**, not a census of all x402 traffic.
- **`amount` being atomic is itself an inference** from field naming and observed
  magnitudes; the spec text was not re-read for this note.
- A client that already knows its asset (a single-token integration) has no
  problem here at all. The gap only appears for a *general* client that pays
  arbitrary payees in arbitrary assets — which is precisely the agent case.
- This is a design observation, not a vulnerability. Nothing here is exploitable
  on its own; it is the precondition that made our three bypasses possible.
