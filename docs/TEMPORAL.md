# The temporal axis (`settlement_velocity.py`)

Three questions about a payee's history:
- **how many** payers? — `distinct_payers` (breadth)
- **who** are they? — the payer graph / payer reputation
- **when** did they arrive? — this module

The settlement timestamps were already ingested and **never used**. This adds the
third axis, and the build is a case study in eval catching a bad signal before it
shipped.

## What gates: `stale`
`last_seen` is **robust** — the backfill always fetches the *most recent*
settlements, so "no settlement in `STALE_DAYS` (90)" reliably flags a payee that may
be **dormant or whose endpoint is abandoned**. Paying a dead endpoint is a real risk
the count-based reputation misses entirely (it treats an old `settlement_count` as
current). So `stale` **gates** the verdict: GO → **HOLD**, "confirm it still
operates." Never STOP, fail-open.

Verified live: a clean-GO payee at its own median stays **GO** today, and flips to
**HOLD** at `now + 1yr` (no settlement in 365 days), with the dormancy reason.

## What does NOT gate: `burst_sybil` (and why)
The tempting signal: a wash-farm manufactures N distinct wallets but funds and fires
them in a **burst**, so "30 distinct payers all first-seen within an hour" looks
Sybil-shaped. `peak_day_share` (largest share of distinct payers first-seen on one
UTC day) measures exactly that.

**The eval killed it as a gate.** On the live corpus (198 payees) `burst_sybil`
flagged **11 payees — including aidress.ai (49 payers) and Nansen (34), our most
reputable endpoints**, all showing `age ≈ 1 day`. The cause is the **backfill
window**, not a real burst: a targeted backfill pulls ~150 recent settlements, and a
high-volume payee does ~150/day, so its *entire visible history* is one day and every
payer looks "acquired at once." Gating a history-completeness heuristic on it didn't
help — aidress.ai (143 settlements) is still just one truncated day.

Conclusion: **burst detection needs COMPLETE history**, which a targeted backfill
structurally cannot provide for exactly the high-volume payees that matter. So
`burst_sybil` is computed and **surfaced as a diagnostic**
(`signals.temporal.burst_sybil_advisory`) but **never acted on**. It becomes viable
with a full-history backfill or the live receipt stream — noted for when that data
exists.

## The lesson
A signal that looks strong in the abstract flagged the best payees in practice,
because it was really measuring how we *collect* data, not the payees. Shipping it as
a gate would have HOLD-ed aidress.ai and Nansen. Eval-before-gate is the whole point:
`stale` (robust) ships as a gate; `burst` (confounded) ships as a labelled diagnostic,
honestly, rather than as a plausible-but-wrong control.

## Contract
`temporal_signal(events, *, now)` is pure over injected `(ts, payer)` events. Folds
via `decide_payment(temporal_signal=…)` / `forecast(velocity_source=…)`, conservative
and fail-open, and `mcp_server` builds a `VelocitySource` off the same `--store`.
