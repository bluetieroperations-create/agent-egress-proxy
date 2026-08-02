# Catching a counterparty that goes bad

The single most important case for a payment-safety gate is also the one a stateless
engine and a lifetime-averaged reputation both miss: **a trusted merchant that turns
malicious** — compromised keys, a rug, or service that quietly degrades.

## Why the lifetime rate can't see it
`reputation_score` folds `dispute_rate` into a Beta posterior weighted by volume:
`good = settlements·(1 − dr)`, `bad = settlements·dr`. That's correct for "is this an
established, clean counterparty" — but it's **dominated by history**. A merchant with
**1,000 clean settlements** then **5 recent disputes** has a lifetime dispute rate of
0.5% and a reputation of **0.994** → a clean GO, even as it's actively disputing now.

## The recency window
`ledger.aggregate_counterparties` now keeps a per-counterparty **timeline** of
confirmed outcomes and computes `recent_dispute_rate` over the last `RECENT_WINDOW`
(10) — ordered by timestamp, so it reflects *what's happening now*, not the average
over all time. The verdict adds a **`going_bad` gate**:

    going_bad = recent_outcomes ≥ 4  and  recent_dispute_rate ≥ 0.30   →  HOLD

HOLD, never STOP (the outcome is often payer-self-reported, so escalate to a human;
don't hard-block on it), and fail-open (no ledger → no signal). It's **orthogonal** to
`reputation_score` — the lifetime Beta is untouched; this is a separate, explainable
gate for the trend.

## End-to-end (real ledger → verdict)
```
1005 confirmed settlements, lifetime dispute_rate 0.50%  -> reputation 0.994 (still high)
last 10 outcomes: 5 disputed -> recent_dispute_rate 0.50
verdict: HOLD -- "recent dispute rate 50% over the last 10 outcome(s) --
                  counterparty trending bad despite a clean lifetime record; escalating"
```

## Why this matters
Everything else in the reputation stack is **breadth** from public data (who pays
whom, how much, when). This is the first signal that acts on **outcomes** — what a
payment actually *did* — which is the depth only Blackwall's own verdict→receipt loop
produces. It's dormant until real outcome traffic flows (a crawl can't manufacture a
dispute), but the mechanism is built, tested, and correct, so the moment receipts
carry disputes, a counterparty going bad is caught on its **next** payment, not after
another thousand clean-history settlements have diluted the signal away.

`recent_dispute_rate` is surfaced on every verdict under `signals.recent_dispute_rate`.
