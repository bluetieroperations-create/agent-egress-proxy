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

A wash-farm payee the per-payee distinct count would have waved through is now caught.

## The payer reputation layer (`payer_reputation.py`)

Breadth-counting has a blind spot: a Sybil **ring** — N sockpuppet payers paying N
sockpuppet payees — gives every member breadth ≥ 2, so none look captive, yet the
whole cluster is fake. Counting breadth isn't enough; you have to ask whether a
payer's breadth touches anything *real*. So we propagate trust from an anchor set:

1. **anchors** — payees with many distinct on-chain payers (≥ `ANCHOR_MIN_DISTINCT`).
   Funding that many independent USDC-holding wallets is a real, ongoing cost.
2. **payer reputation** `r(payer)` ∈ [0,1] — saturates on the number of *distinct
   anchors* a payer pays. Paying several independently-established services makes a
   wallet a proven real agent; a ring member that only pays its own cluster scores 0.
3. **payee corroboration** — `reputable_payers` (payers with `r ≥ REPUTABLE_PAYER_MIN`)
   and a **`sybil_ring`** flag: clears the distinct gate, yet *not one* payer is
   reputable → a closed, unvouched cluster.

`PayerReputationSource.cross_signal(payee)` is a **superset** of the graph source's
(it adds the reputation fields + `sybil_ring`), so it drops straight into
`forecast(graph_source=…)`. `sybil_ring` joins the GO-blocking gates alongside
`captive_sybil` — HOLD-only, never STOP, fail-open — and `mcp_server` now builds this
source off the same `--store`.

### Live result (seeded corpus)
- 17 anchors; 759 payers scored; **72 reputable** (pay ≥2 anchors), 24 maxed at 1.0.
  The busiest agent pays 35 payees across 6 anchors.
- `sybil_ring` flags **5** payees vs `captive_sybil`'s 1 — **4 brand-new catches**
  the breadth signal missed. Example `0x1f81…`: 8 payers, **6 with breadth ≥ 2** (so
  `captive_sybil` says fine) but **0 reputable** → it flips **GO → HOLD** only once
  the payer-reputation layer is on. Real payees are corroborated (aidress.ai: 28
  reputable payers) and big merchants are protected by the distinct ceiling.

### Honesty
Anchors and "reputable" are measured over **ingested** data, so this **raises the
cost** of a Sybil — every fake payer must also pay several genuinely-high-diversity
services, at which point it behaves like a real agent and the payee earns real
corroboration — rather than making Sybils impossible. Both flags only tighten
(HOLD), and both sharpen as ingestion covers more of the ecosystem.

### Signal-stability eval → `sybil_ring` is ADVISORY (not a gate)
A scaled run (724 endpoints crawled, **200 backfilled, 30 anchors, 27% coverage**)
plus a convergence readout changed the posture:

- **It converges.** Restricting the graph to the top-50 most-active payees at rising
  coverage (k = 50 → 100 → 150 → all): anchors grew 13 → 30, reputable payers 84 →
  226, and **100% of the low-coverage `sybil_ring` flags on major payees cleared** as
  coverage rose — they were coverage artifacts, and more data resolved them.
- **But at partial coverage it over-flags.** Full-corpus `sybil_ring` grew 5 → **14**
  as coverage rose, several on payees with **150 settlements** whose payers simply
  don't pay one of the *ingested* anchors yet. Gating on that would HOLD real
  merchants.

So `sybil_ring` is now **advisory** — surfaced under
`signals.cross_counterparty.sybil_ring_advisory` with a non-blocking note, but it
does **not** block GO. `captive_sybil` (stricter, `established == 0`; never fired on a
top-50 payee at any coverage) **remains the gate**. `sybil_ring`
graduates to a gate once a full-set stability check shows its false-positive rate is
low — i.e. once ingestion coverage is high enough that "pays no anchor" is meaningful.

### Adversarial audit (2026-08-02)
An independent audit confirmed the verdict fold is **safe**: the graph signal is
tighten-only (GO→HOLD, inside the `go` conjunction), never touches STOP/`hard_stop`/
`score`, is fail-open on a missing/partial/raising source, and `screen_payer`'s input
validation is sound. All findings were about **detection strength**, not unsafe
verdicts:

| # | Sev | Finding | Status |
|---|-----|---------|--------|
| F1 | High | A wash farm **larger than the ceiling** escapes the graph Sybil gate | **Documented** — the layer adds recall for *small* captive/ring clusters; it is not a complete Sybil defense (baseline missed these too). Larger farms are left to coverage + the price/thin gates. |
| F3 | Med | **Self-edges** (a payee paying its own address) let it vouch for itself — inflating `established`/`distinct` and disabling `captive_sybil` | **Fixed** — `build_index` drops `payer == payee` (13 such edges were in the live corpus) |
| F5 | Med | `captive_sybil` ceiling (8) < `sybil_ring` ceiling (12) → a 9–12 captive farm slipped a graph-only deployment | **Fixed** — one shared ceiling (12) |
| F2 | Med | `sybil_ring` clears if **one** payer pays two anchors — cheaper than the docstring implied | **Documented** + it's advisory anyway (doesn't gate) |
| F4 | Med | Anchors are **Sybil-mintable** (~20 wallets) | **Documented** in the honesty note — raises cost, not impossible |
| F6 | Low | `PayerReputationSource` didn't thread `min_distinct` to the ring flag | **Fixed** |
| F7 | Low | Store's distinct-payer `COUNT` was case-sensitive vs the lower-cased graph | **Fixed** — `lower(payer)` in the SQL |

Regression tests added for F3 (self-edge dropped; self-dealing farm still flagged)
and F5 (shared ceiling). Net: the fixes harden signal integrity; F1/F2/F4 are honest
limits of on-chain-only reputation, now stated plainly rather than oversold.
