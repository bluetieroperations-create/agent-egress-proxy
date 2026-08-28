# Competitors and money — measured, 2026-08-27

Two questions: do competitors do what Blackwall does, and how does this monetize.
Both answered from the probe corpus joined to `data/directory.json`, not from
search results.

## 1. Yes — and one is doing essentially the same product

Captured tool descriptions, verbatim from the live servers:

**`lionx402.com` — the closest competitor.**
> `lion_wallet_screen`: "Use when you have a 0x address and need **PASS/WARN/BLOCK
> before paying anyone**."

That is Blackwall's core verdict, renamed. It also ships
`lion_multi_sanctions_bundle` (OFAC + entity sanctions + **signed receipt**),
`lion_compliance_bundle`, and `lion_verified_company_file` — a KYB dossier with a
per-field source trail. Sanctions, signed receipts and a counterparty verdict:
three of Blackwall's pillars, live and priced.

**Others overlapping in part:**

| Host | Overlaps with |
|---|---|
| `trust-score.api.klymax402.com` | composite trust score 0-100 for a wallet/endpoint |
| `api.anchor-x402.com` | `decode_calldata` (= `calldata.py`), `attest_decision` + `anchor_hash` (= `traceipt_attest.py`), `intel_wallet` with sanctions verdict |
| `api.solenrich.com` | `wallet_graph` — "detect clusters of coordinated wallets" (= `payer_graph.py` Sybil detection); holder concentration; `due_diligence` SAFE/CAUTION/RISKY |
| `agents.ai-rook.com` | `sign_receipt` (= `receipt_signer.py`), escrow dry-run |
| `api.robinx.io` | deployer reputation scoring — adjacent, not the same |

**What none of them advertise**, and what remains genuinely differentiated:

- **Payload simulation** — recovering the EIP-3009 signer to prove the signed
  payment matches the claim (`payload_sim.py`).
- **Settlement simulation** — simulating `transferWithAuthorization` to catch a
  blacklisted payee before signing (`settlement_sim.py`, `auth_sim.py`).
- **AA co-signing** — withholding a signature as a mandatory 4337/7579 guard
  (`aa_cosigner.py`).
- **Price integrity** — category baselines, advertised-vs-settled divergence.

The structural difference matters more than the feature list: **every competitor
above is an API the agent may choose to call.** Blackwall's wallet, AA and AP
integrations are in the signing path, where calling is not optional.

## 2. The x402 per-call market has essentially no revenue

Prices are real and published. Volume is not there.

| Competitor | Distinct payers | Advertised price range |
|---|---|---|
| `api.anchor-x402.com` | 52 | $0.001–1.77 |
| `lionx402.com` | 12 | $0.001–0.95 |
| `trust-score.api.klymax402.com` | 12 | $0.001–0.02 |
| `api.solenrich.com` | 7 | $0.001–0.10 |
| `api.robinx.io` | 5 | $0.01–0.05 |
| `agents.ai-rook.com` | 3 | $0.002–0.25 |

For scale, the **largest payee in the entire directory** — `api.bitrefill.com` — has
**134 distinct payers**. That is the ceiling of the whole ecosystem, not a typical
figure.

**Measurement caveat, stated because it cuts against a stronger claim:**
`settlement_count` is capped at 150 by our own backfill page limit (median 147,
90th percentile 150, max 150 across all 198 payees), so it is NOT a revenue
measure and the $581 lower-bound gross it implies is meaningless. `distinct_payers`
is uncapped and is the reliable figure. It is the payer counts above — 3 to 52,
against an ecosystem ceiling of 134 — that support the conclusion, not the
settlement totals.

## What this means

**Selling payment verdicts per-call over x402 is not a business today.** The
competitors are real, their products work, their prices are published — and their
customer counts are in the single and low double digits. Winning that market wins
almost nothing.

This does not condemn Blackwall. It relocates where the money is:

- **Wallet providers** (`integrations/wallets/` — Turnkey, Privy shims already
  exist). They have enterprise customers paying in fiat. The signing-guard position
  is what they cannot get from a per-call API.
- **Treasury / AP** (`ap_gate.py`). Real budgets, existing approval workflows.
- **AA co-signing** (`aa_cosigner.py`). Mandatory, not optional.

All three are fiat B2B sales to companies that already spend on payment controls.
None of them are the x402 per-call market the competitors are fighting over.

## Corrections to earlier claims in this session

1. "Sell the MCP feed at $2–5k/month per vendor" — a guess stated too confidently.
   Withdrawn.
2. "Fold the MCP data into Blackwall and it becomes a moat" — still true as
   engineering (see `MCP_INTO_BLACKWALL.md`), but it does not create a market. The
   moat protects a position; it does not prove anyone is buying.
3. The measured finding that supersedes both: **the addressable per-call market is
   currently tiny**, and any monetisation path runs through fiat B2B buyers, not
   through x402 micropayments.

---

# CORRECTION (same day): the market-size claim was overstated

The section above concluded "the x402 per-call market has essentially no revenue."
That over-generalised from the sample I had.

**What I actually measured:** 198 payees in `data/directory.json`, which is a
*targeted crawl of the Bazaar catalog* — the long tail of listed API sellers. Their
payer counts (3–52 for verdict vendors, 134 at the ceiling) are real and the
conclusion holds **for that slice**: selling verdicts to catalog-listed API sellers
is not a business.

**What I did not measure, and wrongly generalised over:** the protocol as a whole.
Third-party figures put x402 at roughly **$600M annualised across chains** and
**35M transactions on Solana by March 2026**. I have not verified those independently
— they are vendor/blog figures and should be treated as unconfirmed — but they are
large enough that "no revenue" is not a defensible statement about x402 overall.

Both can be true: volume concentrated in a few large flows, with a long tail of
catalog sellers earning almost nothing. My sample was the tail.

**Status of the earlier conclusion:** the *recommendation* survives (money is in
fiat B2B, not per-call sales to the tail) but the *reason given* was wrong in scope.

# Wallet-provider incumbency — checked before emailing

| Provider | Risk/screening slot | Verdict |
|---|---|---|
| **Privy** | **Filled.** Privy integrates **Blockaid** — transaction simulation and validation against known malicious addresses, *before the signature is generated*. Blockaid is used by MetaMask, Coinbase and Uniswap. | **Do not pitch the generic guard.** The slot has a well-funded incumbent. |
| **Turnkey** | **No named risk partner found.** Turnkey runs a verifiable policy engine in secure enclaves and publishes an AI-Agents solution page. Its own material describes policy scopes that include *"requiring co-approval from a user, operator, **or risk service**, before executing high-value agent actions."* | **The open door.** They name the third-party slot and appear not to have filled it. |

Absence of a published partnership is not proof there is none — this is a
web-visible check, not an inside view.

## What this means for the pitch

Blockaid answers *"is this transaction malicious?"* — drainer contracts, known-bad
addresses, simulation of asset movement. That is consumer-wallet threat detection.

It does not appear to answer *"is this **payment** sane?"* — the questions
`decide_payment` exists for: is the price wildly off this payee's own settled
history, do the signed EIP-3009 authorisation and the stated claim match, is the
payee's payer set a wash-farm, has this endpoint's advertised capability changed
since yesterday. A clean contract paying a fair-looking address at 40x the going
rate is not malicious; it is a bad payment.

That distinction is the entire pitch, and it must be made honestly — as
*complementary to* transaction-security scanning, not a replacement for it.
