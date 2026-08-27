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
