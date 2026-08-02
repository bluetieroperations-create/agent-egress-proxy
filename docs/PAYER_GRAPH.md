# The cross-counterparty payer graph

A per-payee `distinct_payers` count answers *how many* payers a payee has — not
*who* they are. That gap is exploitable: a wash-trader funds N throwaway wallets that
each pay only its one payee, clearing the naive Sybil gate (`distinct >= 3`) with
wallets that are **captive**. A legitimate payee is paid by wallets that also pay
*other* services — real agents spending across the ecosystem.

`payer_graph.py` builds the bipartite **payer ↔ payee** graph from ingested
settlements and turns "who pays whom" into a signal.

## What it derives (per payee)
- **`established_payers`** — payers proven to also pay ≥2 known payees. The *robust*
  half: seeing a payer at two ingested payees is real regardless of sample size, it
  only grows with ingestion, and it's hard to fake (you'd have to actually pay other
  real services).
- **`captive_ratio`** — share of payers that (as ingested) pay only this payee.
- **`cross_score`** — 0..1 corroboration = established / distinct.
- **`captive_sybil`** — a conservative flag: the payee clears `distinct >= 3` yet
  **not one** payer is seen paying any other known payee (bounded to ≤8 distinct, so
  a large captive set — more likely an ingestion-coverage artifact than an affordable
  farm — isn't auto-flagged).

## How it folds into the verdict
`decide_payment(..., payer_graph_signal=…)` / `forecast(..., graph_source=…)`:
- `captive_sybil` joins the GO-blocking gates → **HOLD**. It **never** creates a
  STOP, never overrides one, and is **fail-open** (no graph → unchanged verdict).
- The full signal is surfaced under `signals.cross_counterparty` for the caller.
- Wired into `mcp_server` off the same `--store`, so the live engine uses it.

This is *tighten-only* corroboration, in the house style (like `readiness` and the
seller-audit floor): it can escalate an over-eager GO to human review, nothing more.

## Honesty
"Captive" is measured over **ingested payees only** — a payer that pays services we
haven't ingested looks captive here. So `captive_ratio` is a **floor** on Sybil-risk,
not proof, which is exactly why it only ever escalates to HOLD (the safe direction),
and its precision **rises** as ingestion covers more of the ecosystem.
`established_payers` is the robust half: it can be understated, never overstated.

## Live result (on the seeded corpus)
Built from `data/reputation_seed.db` (759 payer wallets, 73 payees):
- **141** payers are active at ≥2 payees; the busiest pays **35** different payees.
- Corroboration is rich: aidress.ai has 49 payers, **32 established** (cross_score
  0.65); one payee has 17 payers, **all 17 established** (cross_score 1.0).
- The gate fires on exactly **1** payee — 4 distinct payers, **0 established, all
  captive**. Live, that payee flips **GO → HOLD** with the graph enabled, while a
  richly-corroborated payee's verdict is unchanged and a STOP stays STOP.

A wash-farm payee the per-payee distinct count would have waved through is now caught
— and the same graph is the seed of a payer-reputation layer (a payer's ecosystem
breadth is itself a signal) that compounds as more of x402 is ingested.
