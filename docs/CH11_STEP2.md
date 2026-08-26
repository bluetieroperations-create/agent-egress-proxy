# Step 2: INCONCLUSIVE — and the reason is the finding

**Date:** 2026-08-25
**Verdict: could not measure recurrence.** Not because parsing failed, but because
**most filings do not contain a usable business creditor list at all.**

## What I ran

Downloaded 12 filings across ~10 distinct cases from three independent sources — Epiq,
Stretto and CourtListener. All 12 downloaded clean, all valid PDFs.

Then parsed them all. **10 of 12 returned zero creditors.**

That number is not a recurrence result. It is a coverage failure, and chasing it down
is what produced the real finding.

## Why 10 of 12 were empty

They are not all the document I assumed. Inspecting each:

| filing | what it actually is | usable? |
|---|---|---|
| SYX (24pp) | Certificate of service **with the matrix attached** | ✅ 207 creditors |
| CFX (19pp) | Same shape | ✅ 34 creditors |
| WPT (7pp) | Narrative certificate — *"forwarded to all parties on the Debtor's mailing matrix"*. **No matrix attached.** The zip codes I counted were the law firm's. | ❌ |
| STR14513a (175pp) | A real service list — of **individual names only**: `Abigail Dodson`, `Abigail Douglas-Foy`… A retail debtor's list is employees and customers. | ❌ for a supplier graph |
| CL188450 | A *Notice of Filing of* Creditor Matrix — a motion **about** the matrix, not the matrix | ❌ |
| LBI, TEF, GTM, DHG, STR10654, STR14494 | Motions, orders, narrative certificates | ❌ |

So the SYX filing I found in step 1 was **lucky**: an industrial debtor whose business
matrix happened to be attached to a served document.

## The three barriers this exposes

1. **Narrative certificates.** Many certificates of service merely *reference* the
   matrix without attaching it. Nothing to parse.
2. **Sealing.** Courts routinely permit debtors to file creditor matrices with personal
   information **under seal**. An earlier search surfaced exactly such an order. Where
   that happens, the list is not public at all.
3. **Wrong kind of debtor.** A retail or consumer-facing debtor's list is mostly
   individuals — 175 pages of them in one case here. Individuals are useless for a
   supplier graph, and are privacy-redacted anyway.

The bottleneck is **not parsing**. Step 1 proved parsing works. The bottleneck is
**finding the right document in each case, and accepting that in many cases it does not
exist publicly.**

## What that costs

The plan said step 2 was "about a day." It is not. Doing it properly requires walking
each case's full docket to locate a matrix-bearing filing, across agents with different
site structures, two of which (Kroll, Omni) block automation outright.

That is weeks, not a day — and it must be paid **before** you learn whether the graph
exists.

## The one data point, and why it worries me

Across the two usable cases, exactly one creditor recurred: **Cox Business** — a
telecom utility.

n=2 proves nothing. But it points at a structural risk worth naming now:

> **The creditors most likely to recur across bankruptcies are the least interesting
> ones.** Utilities, FedEx, UPS, landlords, state tax authorities — everybody owes them.
> The valuable signal (a specialised supplier burned repeatedly by customers in one
> sector) is precisely the long tail most likely to appear once.

If that holds, the graph could be simultaneously *real* and *worthless*: dense at the
boring centre, empty where the money is.

## Honest status

| step | result |
|---|---|
| Incumbency check | Passed — no cross-case creditor graph found on sale |
| Access | Partial — Epiq, Stretto, CourtListener open; Kroll, Omni blocked |
| **Step 1 — can we parse?** | **PASS** — 207 creditors from a modern filing |
| **Step 2 — do creditors recur?** | **UNKNOWN** — could not assemble enough usable matrices |

This is the first dataset that has not been killed. It is also not yet validated, and
the cost estimate went **up**, not down.

## What step 2 actually requires now

1. Pick 50 cases with **industrial or wholesale debtors** — they have business matrices.
   Retail and consumer debtors are the wrong sample.
2. For each, walk the docket to find a matrix-bearing filing. Expect a meaningful share
   to be sealed or narrative-only; **measure that share, because it caps the whole idea.**
3. Only then compute recurrence — and compute it *excluding* utilities, carriers and tax
   authorities, since those recur trivially and prove nothing.

If step 3 of that list shows specialised suppliers recurring, the product is real.
If the only recurrence is Cox Business and FedEx, it is not.
