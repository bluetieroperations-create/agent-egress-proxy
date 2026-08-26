# Remote-only re-rank — the sixth test, and why it changes the answer

**Date:** 2026-08-25 · **Supersedes the pick in:** `docs/FMCSA_GTM.md` ·
**Companion to:** `docs/COLD_START_DATA.md`

## The constraint is a test, so let's make it one

The rubric in `COLD_START_DATA.md` had five tests. The operating constraint —
*everything from behind the computer, no travel until revenue exists* — is a sixth,
and it is not a soft preference. It disqualifies whole buyer classes:

> **Test 6 — it closes without a meeting.** The buyer must be reachable, evaluable and
> signable from a desk. That means a buyer who already transacts remotely, a purchase
> that does not require procurement theatre, and a distribution channel that reaches
> them without a booth.

Applied honestly, this **demotes my own previous recommendation.**

## What I got wrong

FMCSA carrier lineage is still the easiest thing to *build*. But its go-to-market
leaned on the IFA transportation rooms, and — worse — Ring 1 is **15–25 mid-market
accounts hand-sold at $2–5k/month**. That is a long, personal, relationship-heavy
motion with a small ACV. It is *doable* remotely (freight is a phone-and-Zoom
industry), but it is the wrong shape for someone who needs revenue before they will
travel: low ticket, slow trust-building, and a buyer who has never heard of you.

Under Test 6, the ranking inverts. The question stops being *"which data is easiest to
get"* and becomes **"which buyer already buys from strangers over email."**

There is exactly one such buyer at scale, and it pays extremely well.

---

## The remote-only buyer universe

### Investment funds buying alternative data

This is the most remote-friendly high-ticket buyer that exists, because funds employ
**data sourcing teams whose entire job is to evaluate cold inbound from vendors**.
They never expect you to fly anywhere. The economics, measured:

| Metric | Value |
|---|---|
| Average buyer works with | **20 data vendors** |
| Average annual alt-data spend per buyer | **$1.6M** |
| Implied **average price per dataset per client** | **~$80,000/year** |
| Largest buyers (quant, multi-strat) subscribe to | ~43 datasets, ~$1M+/yr |
| Average dataset is used by | ~20 clients (down from 25 — *fragmenting*, which favours niche) |
| Datasets tracked by Neudata | ~2,805 in 2025, up from 2,215 |
| Vendor universe assessed | ~120 |

One well-positioned dataset at the average — $80k × 20 clients — is **~$1.6M/year**.
Compare that to the freight plan's $6–15k MRR ceiling on hand-sold factoring accounts.
Same effort profile, an order of magnitude apart.

### The channels that replace a salesperson

- **Neudata** and **Eagle Alpha** — data *discovery* platforms that institutional
  buyers subscribe to specifically to find datasets. You list; funds find you. This is
  the single most important remote channel and it is how a solo operator gets in front
  of a $10B fund without an introduction.
- **Snowflake Marketplace / Databricks Marketplace** — **90% revenue share to the
  seller**, and the buyer is already inside the platform, so delivery is a share, not
  an integration project. **AWS Data Exchange** takes 30% but reaches the largest
  enterprise buyer base.
- **Direct email to data sourcing.** Unusually, this works. It is their job.

### The compliance point that makes this possible for you specifically

Most alt-data vendors die in fund compliance review: scraped data with ToS exposure,
personal data, or possible MNPI. **A dataset built entirely from a federal public
registry sails through.** No PII, no scraping agreement, no exclusivity question, no
MNPI. Under Test 6, "it's public government data" stops being a cost advantage and
becomes a *compliance* advantage — the thing that gets you past the gate that kills
your better-funded competitors.

---

## The new #1: ClinicalTrials.gov point-in-time corpus → biotech alt-data

This was **#08** in the original ten. Under the remote constraint it is #1, and the
reason is precise.

### The gap, stated exactly

The v2 API (rebuilt 2024) exposes **only the current version** of each of 500,000+
study records. Historical versions exist and are retrievable per record — the R
package `cthist` does it and describes it as labour-intensive at cohort scale. Nobody
has assembled the full diff corpus.

Monitoring products already exist. **RxDataLab** runs daily comparisons against
ClinicalTrials.gov catching endpoint modifications, enrollment changes, arm updates and
status shifts, sold to BD and investors. So "we alert you when a record changes" is
taken. Do not build that.

What does not exist is the thing that actually decides whether a fund buys:

> **A verified, complete, point-in-time historical corpus of every change ever made to
> every trial record — deep enough to backtest.**

### Why that distinction is the entire business

Two measured facts about how funds buy:

1. **Fewer than 1 in 5 vendor trials end in a purchase.**
2. **Time-to-first-backtest is the single biggest predictor of trial success.** If a
   provider cannot quickly prove its history is point-in-time and complete — no
   retroactive revisions, no overwrites, no look-ahead bias — systematic investors will
   not seriously evaluate it at all.

A daily-monitoring vendor that started collecting last year *cannot offer history*.
That is not a product gap they can close by working harder; the past is gone.

**ClinicalTrials.gov is the rare source where the past is not gone.** The registry
keeps every version with its submission date. That means you can reconstruct **a decade
of true point-in-time history on day one** — the exact asset that converts trials, held
by a vendor with no track record, produced from a free federal registry.

That is the whole thesis, and I have not found anyone doing it.

### The signal itself

Labels that already exist in the diffs, each dated and attributable:

- Enrollment target revised **downward** — the classic pre-failure tell.
- Primary completion date slipped, repeatedly.
- Status → terminated / withdrawn, **with the sponsor's stated reason**.
- Primary endpoint changed *after* enrollment started (a known integrity concern).
- Sites added or dropped; enrollment closed early.

Registry changes can precede a press release by days. The tradeable object is a
liquid, catalyst-driven equity — biotech is the sector where a single trial outcome
moves a stock 60% in a morning, which is why funds pay for edge here more than almost
anywhere.

---

## Working backwards from a remote purchase

Seven links again. Read down; execute up. Note that **not one of them requires leaving
the desk.**

### T-0 · The signature

A **Head of Data Sourcing** or **Director of Alternative Data** at a fund signs an
annual subscription, typically **$50–100k**, entirely over email and Zoom. Secondary
buyer: a biotech-focused PM or the fund's healthcare analyst team. Renewal is annual
and largely automatic if the signal held.

### T-1 · The trial that closes it

They take a sample, load it, and try to reproduce a result. You win or lose on **time
to first backtest**. So ship trial data that is already evaluation-ready: standard
tickers mapped (NCT ID → sponsor → ticker, which is real work and part of your moat),
numeric features not raw JSON, an explicit `as_of` timestamp on every row, and a
written statement of completeness and PIT integrity.

Expect a **<20% conversion rate** and price accordingly — you need trials in the
double digits, which is another reason the discovery platforms matter.

### T-2 · How they find you

- List on **Neudata** and **Eagle Alpha**. This is the closest thing to a sales team
  you can rent, and it is the reason this plan works without travel.
- List the derived dataset on **Snowflake** and **Databricks** marketplaces (90% to
  you). Buyers already in-platform can subscribe with no integration work.
- Direct email to fund data-sourcing addresses, which are public and monitored.

### T-3 · The thing that makes the email worth opening

A **public backtest**, not a product page. One chart, one table, one methodology note:
"trials with an early downward enrollment revision terminated at X% versus a Y% base
rate; median lag to the sponsor's own announcement was Z days." Published openly, with
the false-positive rate stated.

This doubles as the credibility asset for the discovery-platform listing, and it is
written from a desk in a week.

### T-4 · The study behind it

Take a defined universe — say all Phase 2/3 oncology trials 2015–2023 — fetch every
version, and test whether an early downward enrollment revision predicts termination.
Strictly point-in-time: no fact that postdates the revision. If it does not separate
from the base rate, **stop** — you have a corpus but not a signal, and you have spent
three weeks, not a year.

### T-5 · The build

Version fetch → normalized diff records → per-trial event stream → `as_of`-stamped
feature table → NCT-to-ticker mapping. Rate-polite fetching with backoff and read
caps; `http_util.py` is already the right shape.

### T-6 · The data

ClinicalTrials.gov. Free, federal, no key, no ToS negotiation, no PII.

---

## Runner-up: interconnection queues + air permits → AI capex and power

Originally **#05**. Under Test 6 it is the strong second, for one reason: it points at
**the most heavily-traded theme in the market** — datacenter buildout, power demand,
and who captures the capex. Tradeable against utilities, and against equipment makers
with brutal lead times (transformers, switchgear, gensets, cooling).

Air permits for backup generators are filed **before** any announcement, which is a
genuine pre-announcement leading indicator. Queue data is standardized and free.

**Why it is second, honestly:** point-in-time reconstruction is harder here. Queue
snapshots and permit dockets are not archived with the same fidelity as
ClinicalTrials.gov versions, so your PIT history has to be built forward from today for
some sources. That undercuts exactly the advantage that makes #1 work — but it does not
kill it, because the theme demand is so strong that funds will take shorter history.

---

## Demoted under Test 6

Stated plainly so the reasoning is auditable:

| Was | Now | Why |
|---|---|---|
| **#02 FMCSA** | 3rd | Buildable in a weekend, but Ring 1 is 15–25 hand-sold mid-market accounts at low ACV. Its *alt-data* form (net carrier revocations → freight capacity → trucking equities) is partly crowded by FreightWaves SONAR. |
| **#01 Chapter 11 graph** | Later | Best long-term asset; buyer (trade credit insurance) is relationship-driven enterprise. Its distressed-fund form is remote-sellable — revisit once you have a fund logo. |
| **#06 CMS ownership**, **#09 GovCon**, **#10 TiC** | Parked | Enterprise or public-sector buyers, long procurement, in-person norms. |
| **#07 Construction liens** | Parked | SMB buyers who *would* buy remotely, but county-by-county acquisition is a full-time job before the first dollar. |

---

## This week, all of it from the desk

1. **Fetch the version history for 200 trials.** Confirm the archive depth and the diff
   quality. Half a day, and it validates the entire premise.
2. **Build the NCT → ticker map** for the sponsors that matter. This is unglamorous and
   it is a real part of the moat.
3. **Run the study** on Phase 2/3 oncology, 2015–2023. This is the kill gate — it fires
   in week 3, not month 9.
4. **Read Neudata's and Eagle Alpha's listing requirements** and write the one-page
   dataset tearsheet against them.
5. **Email three fund data-sourcing teams** with the backtest, before the product
   exists. Their job is to reply. The responses tell you what to build.

Nothing above needs a plane, a booth, or an introduction.

## The honest risk

Alt-data sales cycles run **3–9 months**, and fewer than one trial in five converts.
This is a higher-ceiling, slower-first-dollar path than the freight plan. If cashflow
timing matters more than ceiling, run the FMCSA build in parallel as the fast lane —
the two share nothing technically, so pick one or accept the split, but do not pretend
the alt-data path pays in month two.

## Sources

- Alt-data buyer economics — [Neudata: state of the alternative data market 2026](https://www.neudata.co/blog/state-of-the-alternative-data-market-2026), [Capital Ranking vendor universe](https://capitalranking.com/news/2026/05/202605288687), [Grand View market size](https://www.grandviewresearch.com/industry-analysis/alternative-data-market)
- How funds evaluate and why trials fail — [Eagle Alpha on point-in-time data](https://www.eaglealpha.com/2024/05/06/point-in-time-alternative-data/), [vBase on selling alternative data](https://www.vbase.com/alternative-data/), [Deloitte on alt-data discovery to integration](https://www.deloitte.com/us/en/insights/industry/financial-services/alternative-data-for-investors-from-discovery-to-institutionalization.html)
- Vendor channels — [Eagle Alpha provider guide](https://www.eaglealpha.com/alternative-data-provider-complete-guide/), [Snowflake Marketplace mechanics](https://www.flexera.com/blog/finops/snowflake-marketplace/), [Databricks Marketplace](https://www.databricks.com/blog/announcing-new-opensharing-and-marketplace-capabilities-ai-era)
- ClinicalTrials.gov — [API v2 announcement](https://www.nlm.nih.gov/pubs/techbull/ma24/ma24_clinicaltrials_api.html), [cthist registry-history package](https://pmc.ncbi.nlm.nih.gov/articles/PMC9249399/), [RxDataLab (the existing monitoring product)](https://rxdatalab.com/pricing/), [biotech alt-data strategy overview](https://www.drugpatentwatch.com/blog/a-strategic-guide-to-alternative-data-for-biotech-investors/)
