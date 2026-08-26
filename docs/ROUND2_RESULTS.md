# Round 2 results: #2 alive but flawed, #1 dead

**Date:** 2026-08-25 · ~40 minutes.

## #1 — EU AI Act training-data summaries: DEAD

Two independent reasons, either sufficient:

- **Too small.** Only **11 models from 7 providers** have filed the standard template
  (Google, Meta, Microsoft, OpenAI, Swiss AI, SpeakLeash, Hugging Face). Anthropic,
  Mistral and xAI filed narrative text instead of the template. Eleven records is not a
  dataset.
- **Already done.** Pebblous has published the gap analysis — *"Who Filled the
  Template"* — free. aial.ie runs a GPAI training-transparency research project.

Killed in one search.

## #2 — Unregistered data brokers: real data, real flaw

### What works

California's data broker registry is **free, direct CSV download**, no key:
`cppa.ca.gov/data_broker_registry/registry.csv` plus annual snapshots.

- **603 current registrants**, 543 in the 2024 snapshot.
- Enforcement is live: CalPrivacy has **doubled the fine to $200/day per consumer** for
  failure to register, and 345,000+ deletion requests have flowed through DROP.

Year-over-year churn is computable in minutes. **165 companies registered in 2024 do not
appear in the current registry**, and a liveness check found **27 of the first 30 still
trading** — Plexuss, Compile, SearchBug, Reach Marketing, Lotame, Throtle.

### The flaw that matters

My first pass said **193**. That was wrong.

The registry has a **legal name column and a separate DBA column**, and I matched only on
legal name. Real examples:

```
Versium Analytics   ->  registered as "Versar Data Solutions Inc"
FullContact         ->  a DBA of "J2 Martech Corp"
Dun and Bradstreet  ->  a DBA under "NetWise Data, LLC"
```

Matching on name **or** DBA removed **28 false positives — 15% of the answer** — from a
single missed column.

That number is the finding. If one overlooked column produced a 15% error, the remaining
165 certainly contains more, from causes I have not tested: acquisitions, parent-company
registration, legal-name changes, and legitimate exemptions.

### Why that is worse here than elsewhere

This product tells someone **a company may be breaking the law**. A 15%-and-unknown error
rate on a defamation-adjacent claim is a bad combination — worse than the same error rate
would be on a sales-lead list.

It also means the obvious buyer is wrong. Selling *"here is who is violating"* to law
firms and regulators requires accuracy I cannot yet demonstrate. Selling *"check whether
you are exposed"* to the brokers themselves survives the error rate, because a false
positive there costs the customer five minutes, not their reputation.

### Also untested

- Whether `registry.csv` is a complete current registry or a mid-cycle snapshot. If the
  2026 registration window is still open, some of the 165 have simply not filed yet —
  which would gut the number entirely. **Check this before anything else.**
- Exemption criteria.

## Status

| | verdict |
|---|---|
| #1 AI Act summaries | **Dead** — 11 records, and Pebblous already published it |
| #2 Data broker gap | **Alive, unproven.** Free CSV, live enforcement, real churn — but high and unquantified false positives, and pointed at a legally sensitive claim |

Cost of learning both: about 40 minutes.

## If #2 continues, in this order

1. Confirm whether the registration window is open. One email to CalPrivacy or one
   careful read of the registry page. **This can kill it outright.**
2. Hand-verify 20 of the 165 against state corporate records and acquisition news.
   Measure the real error rate.
3. Only then decide the buyer — and default to selling self-checks to brokers, not
   accusations to lawyers.
