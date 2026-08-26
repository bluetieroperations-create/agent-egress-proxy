# Week two: the dealer product does not work

**Date:** 2026-08-25 · **Gate:** hand-verification, per `docs/WEEK_ONE_FINDINGS.md`
**Verdict: FAIL. Stop building the lab-closure product. The corpus survives; the
product built on it does not.**

Week one asked *can we compute the signal?* — yes, at ~83.5% precision. Week two asked
the question that actually matters: **does the signal mean what we need it to mean?**

It does not.

## The NSF cross-check: the blind spot isn't where I assumed

Swept **1,376 active NSF awards** in Michigan and matched against the 105 flagged labs.
NSF has no crosswalk to NIH's `profile_id`, so matching is by name and all three tiers
are reported:

| match tier | count | share |
|---|---|---|
| Full name + same institution (**defensible**) | 3 | **2.9%** |
| Initial + same institution | 1 | 1.0% |
| Surname only (collisions — Wang, Kim, Smith) | 18 | 17.1% |
| No surname match at all | 83 | 79.0% |

**NSF closes only ~3% of the blind spot**, not the 20% a naive surname match suggests.
And one of the three is almost certainly a different person — the NSF award is a natural
language processing project, not biomedicine.

That is itself a finding: for NIH-funded biomedical labs, **the alternative funding is
not NSF.** It is industry, foundations (AHA, ACS, JDRF), DoD/CDMRP, and institutional
support — and almost none of that is publicly queryable. The blind spot is real, large,
and mostly *unclosable* with public data.

Adjusted precision on "won't get NIH money again soon": **~81%**.

## The hand-verification: 8 of 8 flagged labs are thriving

That 81% measures *"will this PI receive NIH money again."* The dealer needs
*"is this lab winding down."* Week two is where those two questions come apart.

Eight labs from across the ranked list, checked against public faculty and lab pages:

| flagged lab | what is actually true |
|---|---|
| Top-ranked neuroscience PI, $9.1M | Named collegiate professor, active lab with a current members page |
| Pulmonary PI, $6.5M | **Awarded a major national lecture in 2026.** Emeritus — the one plausible wind-down |
| Synthetic biology PI, $6.3M | Division chief, active lab website |
| Neuroengineering PI, $4.2M | Professor, division chief, active multi-species lab |
| Diabetes PI, $1.9M | **Moved to Canada in 2021**; now heads an axis at a Montréal institute |
| Neural-interface PI, $2.1M | Associate professor, runs a named lab with a current team page |
| Cerebrovascular PI, $1.9M | Active departmental faculty page |
| Device company, $6.0M | Live company, DoD-funded, progressing toward FDA clearance |

**Zero of eight are winding down.** One is emeritus. One left the country — a false
positive my national check structurally cannot catch, because it only sees US NIH.

Eight is a small sample and I will not claim a precise rate from it. But 8/8 in one
direction, plus a mechanism that explains it, is enough to stop.

## Why it fails, mechanically

**A gap in NIH funding is a normal event in an academic career, not a terminal one.**
Established investigators bridge gaps with institutional funds, foundation grants,
industry contracts, other agencies, and by simply waiting for the next cycle. Lab
closure is a rare event driven by retirement, death, departure, or institutional
shutdown — **none of which RePORTER reports.**

Worse, the product's own ranking makes this actively harmful:

> **Ranking by cumulative award size is anti-correlated with closure.** A large lifetime
> NIH total selects for senior, decorated, institutionally-cushioned investigators —
> precisely the people most able to survive a funding gap. The product put the least
> likely closures at the top of the list.

Week one's design flaw was thinking dollar size proxies *equipment*. The deeper flaw is
that it proxies *survivability*.

## What this would have cost

The plan's first hard gate was "one paying dealer by week 8." **The product would have
failed earlier and worse than that gate could detect.** The first list sent to a
liquidation firm would have been full of thriving, well-known labs — several of them
nationally recognised. In a segment of twenty-five firms who all know each other, that
is not a lost sale. It is a permanently burned reputation.

Cost of finding out this way: **two weeks and zero dollars.**

## What survives

Three things, and they are not small:

1. **The corpus and the pipeline are correct.** 17,900 awards, stable `profile_id`
   entity resolution, `date_added` point-in-time reconstruction, measured 90-day
   posting lag. All of that still holds. Nothing about the *data* failed.

2. **The institution-level signal is unaffected.** "This university's NIH funding fell
   $14M, −22%" is accurate, verifiable, and requires none of the lab-level inference
   that just broke. That was always the free tracker and the volume-tier product.

3. **The Phase 3 fund product is untouched — and is now the strongest survivor.** Funds
   want institutional funding flows to forecast lab-supply and instrument revenue. That
   thesis never depended on predicting individual lab closures, and the reconstruction
   finding means its backtest can start now rather than in nine months.

## What to do instead

**Kill the dealer beachhead.** The 15–25 liquidation firms were the fast-dollar buyer
precisely because their need was so sharp — but the data cannot serve it honestly.

**Move the fast-dollar buyer to the aggregate signal.** Vendor reps, small suppliers,
distributors and recruiters want to know which *institutions and departments* in their
territory are growing or shrinking. That question the data answers accurately, today.
It is lower-urgency than a liquidation lead, so expect a lower price and a slower first
sale — this is a real cost of the finding, not a free pivot.

**If the lab-level product is ever revived, it needs a different signal:** faculty
directory diffs, emeritus-status changes, departmental page removals, obituaries. That
is a scraping problem with real entity-resolution difficulty, not an API call — and it
should not be attempted until the aggregate product is paying.

## The honest lesson

Week one tested *"can I compute it."* That was the wrong gate to lead with, and it
passed. Week two tested *"does it mean what I need,"* and it failed.

**The verification step should have come first.** Twenty minutes of reading public
faculty pages would have killed the dealer product before a line of code was written.

## Reproducing

```sh
cd labclosure
python3 fetch_reporter.py MI && python3 nsf_fetch.py MI
python3 nsf_tighten.py          # the 2.9% NSF overlap
python3 run.py MI               # the 105-lab list that does not mean what it looks like
```
