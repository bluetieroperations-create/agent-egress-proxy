# Advisory-signal re-evaluation (full 292-payee corpus)

Three Sybil signals are deliberately NOT gated: `sybil_ring` (advisory), `burst_sybil`
(diagnostic), and `captive_sybil` is capped at `CAPTIVE_SYBIL_MAX_DISTINCT=12`. Those
calls were made on a THIN store (~40 payees). This is the re-eval after the store grew
to the full 292-payee corpus (`data/reputation_seed.db.gz`, ~23k settlements) — asking:
can any of them now graduate to a HOLD gate?

**Answer: no. Keep all three as-is.** The conservative calls were correct and stay
correct at 7× the data. The blocker is data COMPLETENESS, not VOLUME (see below).

## Method

Build the payer-reputation + velocity sources off the full store; for every seed payee
with graph data (281), measure how often each signal fires on this presumed-legit
corpus (real, active, on-chain-paying Bazaar endpoints), and — decisively for a gate —
how many currently-GO payees it would NEWLY block.

## Findings

| Signal | Fires | Broad (≥20 distinct) flagged | Would newly block a GO |
|---|---|---|---|
| `sybil_ring` | 19/281 (7%) | **0** (was the disqualifier at thin coverage) | **12** currently-GO payees |
| `burst_sybil` | 15/281 (5%) | — | flags a 43-distinct payee, peak_day_share 1.0 |
| captive-ceiling gap (distinct>12 & established==0) | 2/281 | — | e.g. the #1 payee (89 distinct) |

- **sybil_ring → stay advisory.** Good news: the old failure mode (flagging our BROADEST
  payees) is gone — 0 payees with ≥20 distinct are flagged now. But gating it would still
  newly HOLD **12** currently-GO presumed-legit payees (thin payees, 3–12 distinct, whose
  payers don't happen to pay a known anchor). That's real friction on legit endpoints.
- **burst_sybil → stay diagnostic.** Still confounded by the backfill window: a targeted
  backfill captures only a recent slice, so a high-volume payee's whole visible history
  compresses into ~1 day. It flags a payee with **43** distinct payers (`peak_day_share`
  1.0) — clearly the artifact, not a burst attack. Real burst detection needs COMPLETE
  per-payer first-seen history.
- **captive ceiling → keep it.** Only 2 payees have distinct>12 & established==0, and one
  is the corpus's **#1 payee** (89 distinct payers, trust-directory top). Removing the
  ceiling to close the "large captive farm" gap would false-gate it — its 89 payers are
  real, they just don't overlap our other 291 seed payees. The gap is a coverage problem.

## The root cause: completeness, not volume

The store ingests each seed payee's INBOUND settlements, so the graph knows which payers
pay which of the **292** seed payees — but NOT a payer's payments to the thousands of
payees outside the seed set. A legit payee whose payers transact elsewhere therefore
looks captive / ring-like / anchor-less. Growing the seed set 40→292 removed the
worst false positives (broad payees) but can't remove this structural one.

**What WOULD let these graduate:** ingesting payers' COMPLETE on-chain activity (their
full outbound USDC graph), not just their payments to seed payees — a much larger data
effort than extending the seed manifest. Until then, all three stay conservative.

## Reproduce

Decompress `data/reputation_seed.db.gz`, then per payee compare
`PayerReputationSource.cross_signal` (`sybil_ring`, `captive_sybil`, `established_payers`,
`distinct_payers`) and `VelocitySource.temporal` (`burst_sybil`) against the payee's
current `forecast` verdict. A signal is a graduation candidate only if gating it blocks
~0 currently-GO payees.
