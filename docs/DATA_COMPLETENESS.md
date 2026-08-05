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

**Stage 1 — measure whether coverage clears the false flags (DONE — instrument +
finding).** `coverage_eval.py` is the go/no-go instrument. It models coverage
faithfully — the backfill ingests per target-payee, so at fraction `f` a deterministic
`f`-subset of payees (all their inbound edges) is ingested, the graph/anchors/payer-
reputation are rebuilt within it, and we count how many KNOWN-GOOD payees (ring-band
payees the FULL corpus vouches for) spuriously flag `sybil_ring`. It reports a
convergence curve + a verdict.

Honesty guard baked in: `known_good` is defined at full coverage, so the `f=1.0` rate is
definitionally 0 and proves nothing — the verdict judges the **sub-full tail** (is the
rate already ~0 at the highest `f < 1.0`, and did the prior step barely move it?).

**Finding on the shipped 292-payee corpus** (`data/coverage_report.json`): the false-flag
rate rises mid-coverage (peak ~15% at f=0.4, when there are enough payees to evaluate but
anchors are still under-observed) then converges — **0.74% at f=0.90, 0.00% at f=0.95** —
with a flat tail. Verdict: **gating reachable = True**. The current corpus is already past
the coverage-sensitive regime for `sybil_ring`; Stage 1's completeness goal is met by the
existing backfill, not blocked on a bigger crawl. `test_coverage_eval.py` guards the
instrument (synthetic known-answer) AND re-runs the verdict on the shipped seed, so a
future refresh that regresses convergence fails the suite before anyone gates.

Caveat (honest): this measures convergence *within* the ingested corpus (subsampling it).
It shows the last slice of our coverage no longer moves the flag set — strong evidence the
signal has stabilized at this corpus — but it can't prove a very different/larger ecosystem
wouldn't reveal new sensitivity. The seed-regression test is how we keep watching. No
verdict logic changed; the oracle stays byte-identical.

**Stage 2 — continuous, GUARDED refresh (DONE — infra; no verdict change).** Turns the
one-time backfill into an automated refresh that **cannot ship a bad corpus**. The key
piece is `refresh_guard.py` (`assess_refresh`): a candidate store may replace the
committed one only if it RETAINS the old store's size (≥80% of payees AND edges — a
collapse means a partial crawl) and is GENUINELY FRESHER (progress made, result actually
fresh). A coverage-convergence regression (the Stage-1 property) is surfaced as a WARNING,
not a reject — freshness wins, and `test_coverage_eval`'s seed-regression check
independently keeps `sybil_ring` advisory on a regressed corpus. That separation is the
design: the guard protects the DATA; the gate protects the RULE.

Both refresh paths are guarded: `scripts/refresh_seed.sh` now builds to a TEMP candidate
and PROMOTES over the committed artifacts only on ACCEPT; `.github/workflows/seed-refresh.yml`
runs that guarded refresh on a weekly cron (+ `workflow_dispatch`), and on ACCEPT opens a
PULL REQUEST with the refreshed artifacts (so the full suite — incl. the seed-regression
gate — runs on the PR before a human merges), or on REJECT opens a nag issue and changes
nothing. `chain_backfill` is idempotent, so re-runs are safe. **This kills the stale
cliff** — same root cause as the coverage gap, fixed once — without a human hand-running
the refresh, and without automation ever being able to damage the corpus. Oracle stays
byte-identical. (`schedule:` is dormant until the branch merges to default; `workflow_dispatch`
works now. `test_refresh_guard.py` guards the accept/reject logic.)

**Stage 3 — promote `sybil_ring` to a gate (DONE — the only behavior change, isolated).**
`sybil_ring` now GATES via `ring_gate = sybil_ring and SYBIL_RING_GATES`, folded into
`graph_sybil` so it joins the existing HOLD-only Sybil path — structurally unable to
reach STOP/hard_stop or clear a sanction. Behind the reversibility lock `SYBIL_RING_GATES`
(flip to False to demote to advisory instantly, no logic rewrite — guardrail #1). The
response surfaces `sybil_ring` + `sybil_ring_gated` (was the misnamed `sybil_ring_advisory`).

Shipped with the full evidence chain:
- **Oracle golden regenerated** and the diff verified surgical: exactly **2 verdict
  changes**, both `sybil_ring` rows GO→HOLD (`good|empty_hist`, `good|fair`); every other
  changed row differs only in `num_reasons` (the advisory note is no longer tacked onto a
  STOP), with hard_stop/score/blast_radius unchanged. No verdict got less restrictive.
- **Fuzz invariant P9** (`fuzz_verdict.py`): differential — turning `sybil_ring` on may
  only tighten GO→HOLD, never introduce a STOP or change hard_stop. 120k cases, 0
  violations; mutation-verified (routing the ring to STOP → 345 P9 hits).
- **Redteam**: the ring moved from a documented GAP to CAUGHT (**17 caught / 2 gaps / 0
  false-positive**). The obsolete "established w/ ring advisory" control was reworked into
  a boundary control (a ring-band payee WITH a reputable payer → GO), proving the gate
  keys on `reputable_payers==0`, not on a low distinct count.
- **Unit tests**: HOLD-only boundary (ring alone → HOLD; ring + sanction → STOP unchanged)
  and the `SYBIL_RING_GATES` demote-to-advisory path.

If a known-good payee ever flips to HOLD in production, `SYBIL_RING_GATES=False` demotes
instantly, and `test_coverage_eval`'s seed-regression check independently blocks gating on
a corpus whose convergence has regressed.

**Stage 3.1 — precision refinement (DONE, from the post-Stage-3 corpus audit).** Running
the live gate over the shipped 281-payee corpus found it HOLDing **6.8%** (19 payees).
All had `reputable_payers==0`, but a deep look showed **6 were not closed clusters** —
their payers each paid a real anchor, just a *single* one, so none cleared the `reputable`
bar (which needs ~2 anchors via saturation). The gate's own reason says "closed cluster",
but `reputable==0` also caught these anchor-CONNECTED-but-under-saturated payees. (Note: an
earlier idea — suppress when `established_payers` is high — was *rejected*: a mutual
sockpuppet ring shows high `established` too, so that would gut the signal's core purpose.)

The fix tightens the definition in `payer_reputation.payee_corroboration` from
`reputable == 0` to `anchor_connected == 0` (a payer's reputation is >0 iff it pays ≥1
anchor, so no new data needed): sybil_ring now means a truly anchor-ISOLATED cluster.
Impact: 19→13 HOLDs (6.8%→**4.6%**), clearing all 6 under-saturated payees (incl. the 3
with real cross-breadth) while keeping the 13 genuinely closed clusters. The change is
UPSTREAM of `decide_payment`, so the oracle golden, redteam, and fuzz invariants (which
use synthetic `sybil_ring` values) are byte-unchanged; only the live signal computation
tightens. Convergence re-eval still `gating_reachable=True` (0% at f=0.95).
`test_payer_reputation` gains a discrimination test (under-saturated payee → NOT flagged).

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

## `burst_sybil` — adjudicated: stays advisory (measured, not asserted)

`sybil_ring` graduated to a gate because its false-flag rate on known-good payees
*converged to ~0* (Stage 1). The obvious next question — should `burst_sybil` graduate
too? — is now **settled with a measurement, so it isn't re-litigated.**

`burst_sybil` fires when ≥ `BURST_MIN_DISTINCT` (5) payers are first-seen with
`peak_day_share ≥ 0.8` (≥80% acquired in one UTC day). Run over the shipped corpus it
flags **15/281 payees (5.3%) — including 3 of 37 anchors (8.1%)**. Anchors are the
*most*-reputable payees (≥20 distinct on-chain payers, hard to fake), so an anchor being
flagged is the documented failure mode: a **targeted backfill captures only a recent
window**, compressing a high-volume payee's visible payer-acquisition into ~1 day. Where
`sybil_ring`'s false-flag rate went to 0, `burst_sybil`'s is a material 8.1% on
ground-truth-good payees — gating it would HOLD real established merchants.

**Decision: keep it, advisory.** It is NOT retired — it is a valid signal that would work
given *complete* per-payee history (which targeted backfill structurally cannot provide;
that needs a full-history source, not just more coverage). Retiring loses that future
value; gating it now blocks real merchants. So it stays surfaced as `burst_sybil_advisory`
(a reviewer sees the pattern), never gated — `test_burst_is_diagnostic_never_gates` locks
that behavior. Reproduce the 8.1% with the snippet in this commit's message / the corpus
measurement above. This ledger is closed; do not re-open without a complete-history source.

## What this does NOT fix

- **Complexity (Risk 1)** — the calibration lock caps it; the `decide_payment` HOLD-gate
  unification (single gate table) reduced the worst of it. Further consolidation is
  optional, and now safe (the oracle catches any drift).
- **The Traceipt drop bug (Risk 3)** is external and stays *contained* (ingest only on
  sealed proof; broader direct on-chain ingestion reduces reliance) — not fixed here.
