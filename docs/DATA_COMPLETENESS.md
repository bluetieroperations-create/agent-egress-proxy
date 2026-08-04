# Data completeness → gating advisory Sybil signals (safely)

Two Sybil signals are built but **advisory-only** because the reputation corpus is a
partial, windowed backfill, not continuous coverage:

- **`sybil_ring`** (`payer_reputation.py`) — a mutually-paying sockpuppet ring: clears
  the distinct-payer gate, payers even have breadth ≥ 2, yet NOT ONE pays a trusted
  anchor. Over-flags at partial **ingestion coverage**, so it is surfaced, not gated.
- **`burst_sybil`** (`settlement_velocity.py`) — many distinct payers all first-seen in
  one day. A targeted backfill captures only a recent **window**, so a high-volume
  payee's whole visible history compresses into ~1 day and it flags the *most* reputable
  payees. Needs complete per-payee **history depth**, which targeted backfill can't give.

This is the **latent-coverage** risk: work that reads as protection but doesn't gate.
The same root weakness — partial, non-continuous data — is what causes the free-tier
**stale cliff** (~90-day frozen store). So closing the data gap the right way
(continuous ingestion) buys two fixes at once.

## The one principle

**Separate the DATA change from the LOGIC change; never ship them together.**
Widening ingestion changes what's *in* the store. Promoting a signal changes the
*verdict rule*. If both move at once and a verdict shifts, you can't attribute it.
Each stage is a separate commit, measured against a fixed baseline.

## Stages

**Stage 0 — pin current behavior (DONE).** `verdict_oracle.py` snapshots the whole
decision surface (DB-free, deterministic) to `data/verdict_oracle.json`;
`test_verdict_oracle.py` diffs against it and fails on ANY unintended drift.
`test_calibration_lock.py` pins every threshold so miscalibration can't recur silently.
An intentional behavior change ships by regenerating the golden in the *same* commit —
the golden diff is the auditable record of exactly which verdicts changed. (Verified:
simulating the Stage-3 flip moves exactly the two `sybil_ring_advisory` rows, nothing
else.)

**Stage 1 — widen ingestion coverage (data only; signals stay advisory).** Extend
`chain_backfill` breadth so the payer graph is dense. **Measurable gate:** track the
advisory `sybil_ring` false-flag rate on KNOWN-GOOD payees as coverage grows. When it
converges to ~0, the data is "complete enough." No verdict logic changes — the oracle
must stay byte-identical (an advisory signal flipping does not change a verdict). This
is where you *earn* the right to gate; you do not gate on faith.

**Stage 2 — continuous refresh (infra; still no verdict change).** Turn the one-time
backfill into a scheduled incremental refresh (`scripts/refresh_seed.sh` +
`check_seed_age` + the nag workflow are the seed). `chain_backfill` is idempotent, so
re-runs are safe. **This kills the stale cliff** — same root cause, fixed once. Oracle
stays green.

**Stage 3 — promote `sybil_ring` to a gate (the only behavior change, isolated).** Only
after Stage 1's false-flag rate is ~0. One branch: add `sybil_ring` to the `go`
conditions, HOLD-only, structurally unable to reach STOP/hard_stop or clear a sanction
(mirrors `enrichment_review`). Then: a fuzz invariant (ring gating never STOPs / never
clears a sanction), flip the redteam "Sybil ring (advisory only)" scenario KNOWN-GAP →
CAUGHT, and regenerate the oracle golden. **The golden diff must show ONLY genuine
rings moving to HOLD.** If a single known-good payee flips to HOLD, the data isn't
complete — demote to advisory and return to Stage 1.

## Guardrails (protect "how it should work")

1. **Reversible by config, not redeploy.** Gate the promotion behind a flag/threshold
   so a live false positive demotes to advisory instantly.
2. **`verdict_digest` computation stays frozen** — no new field enters the digest input
   (byte-identical with Traceipt). A verdict that legitimately flips GO→HOLD naturally
   has a different digest; that's correct.
3. **Fail-open preserved** — ingestion pipeline down serves last-known data, never
   blocks a verdict.
4. **Leave `burst_sybil` advisory.** It needs complete per-payee history depth that
   targeted backfill structurally can't provide. Promoting it is where you'd damage
   things; retire-or-keep-advisory is the honest resolution, not gating.

## What this does NOT fix

- **Complexity (Risk 1)** is capped, not reduced, by the calibration lock — promoting a
  signal adds gating surface. A separate simplification pass is the real lever there.
- **The Traceipt drop bug (Risk 3)** is external and stays *contained* (ingest only on
  sealed proof; broader direct on-chain ingestion reduces reliance) — not fixed here.
