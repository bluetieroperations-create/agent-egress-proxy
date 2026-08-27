# Bid tabulations: the creative find

**Date:** 2026-08-25 · **Status: verified data, incumbency check owed.**

## The reframe that produced it

I had been hunting for *unowned datasets*. Wrong question. The better one:

> **What was impossible to aggregate before 2023, because it required armies of people
> reading PDFs?**

Incumbents' moats in those spaces were manual labour. LLM extraction collapsed that moat
about two years ago, and most of them have not repriced for it.

## What a bid tabulation is

When a government agency opens sealed bids, the results become **public record within
24–48 hours**. The bid tab lists **every bidder and what they bid.** Cities, counties,
school districts, transit authorities and all 50 state DOTs publish them.

It is the only place the *losing* prices are visible. A contractor cannot get this
anywhere else — competitors do not share bids. **The buyer has no private alternative**,
which is the filter every earlier idea failed.

## Verified, not assumed

Pulled six live Minneapolis bid tabs. **Native PDFs. 6 of 6 parsed cleanly.**

Example — Large Diameter CIPP Lining 2026, opened 17 June 2026:

```
Visu-Sewer, Inc.              $2,984,589.50
Insituform Technologies USA   $3,104,528.54
Michels Trenchless, Inc.      $3,254,768.12
Veit & Company, Inc.          $4,979,724.50
```

Four bidders, exact prices, on a $3M job. The spread between low and high bid is **67%** —
that alone is worth money to anyone bidding this work.

Across the six files: jobs from $173k to $5.0m, 1–18 price points each, all extracted
without a single failure.

## Two tiers of data

| tier | what you get | where |
|---|---|---|
| **City / county** | Total bid per bidder | Minneapolis, Chicago, thousands of others |
| **State DOT** | **Line-item unit prices** per pay item | MnDOT publishes `ItemsUsedForPastProjects`; TxDOT runs a bid-tab dashboard |

The DOT tier is the valuable one — unit prices per item let you benchmark *what concrete,
asphalt, pipe and labour actually cost*, by region and over time.

## Why nobody has aggregated it

Not because it is secret. Because it is **scattered across thousands of separate
government websites as PDFs**, and until recently parsing that at scale was a data-entry
project, not a software project.

That is the same "annoying data is the moat" hypothesis from earlier — except this time
the annoyance is now cheap to overcome, and the incumbents' pricing has not caught up.

## What I have NOT checked — and it has killed five ideas

**Construction data incumbents: Dodge Construction Network, ConstructConnect, BidClerk.**
They sell *project leads* — who is building what, and when. Whether they also sell
*bid results and unit-price history* is unverified.

That is the single question that decides this, and it is one search plus one sales-page
read. **Do it before anything else.**

Secondary unknowns: TxDOT already has a good dashboard, so the strong states may be
covered and only the awkward ones are left; and contractors are price-sensitive SMBs.

## Next, in order

1. **Check Dodge and ConstructConnect** for bid-result and unit-price products. One hour.
2. If open: build a single-metro proof — 100 bid tabs from one city, extracted into a
   table of bidder, project, price. Then show it to three contractors and ask what they
   would pay to see it for their trade.
3. Only then think about coverage.

## Also checked this round

| idea | verdict |
|---|---|
| **SEC material contract exhibits** | **Weak.** Contracts made "in the ordinary course of business" — including most customer contracts — are *not required to be filed*. The customer-and-pricing thesis is gutted at the source. LexisNexis already indexes exhibits. |
| **Public sector union contracts** | **Partly served.** DOL OLMS keeps a CBA file; Cornell, Rutgers, MSU and Berkeley run digitized collections. Not structured or commercial, but the buyers (municipalities, unions) are slow and poor. |
