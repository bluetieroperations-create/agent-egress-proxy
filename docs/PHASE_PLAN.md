# The list, then the site: NIH lab-closure intelligence

**Date:** 2026-08-25 · **Supersedes:** the earlier tracker-first version of this file ·
**Resolves:** `REQUIREMENTS.md` E1 (fast dollar → highest ceiling)

## What changed, and why

The first version of this plan started with a public funding tracker: build a website,
rank it in Google, convert readers into $149/month subscribers. That was wrong, and
working backwards properly is what exposed it.

Working backwards has to start at **the customer's win**, not at your cheque. Do that,
and the website disappears from the critical path entirely:

> **0.** A dealer buys equipment from a closed lab they would never have found otherwise.
> **1.** They got there before the university disposed of it or a competitor arrived.
> **2.** They knew weeks early, because your alert beat every other signal.
> **3.** The alert was **right** — that lab really was winding down.
> **4.** They were subscribed and opening your emails.
> **5.** Your first list contained labs they could independently verify.
> **6.** They opened your cold email, because the subject named their region and a number.
> **7.** You had their name, from a hand-built list of 15–25 firms.
> **8.** You could produce a correct regional list of dying labs.
> **9.** You could tell a dying lab from a healthy one.
> **10.** You could pull and structure RePORTER, and terminations were detectable.

**Nowhere in that chain does anyone visit a website.** Steps 6 and 7 are a list and an
email. The build order is simply that chain reversed: 10 → 0.

So: **the first version of this product is a spreadsheet and an email.** The site gets
built after someone has already paid for the thing it would advertise.

---

## Step 9 is the entire project

Everything else here is plumbing that anyone could do. This is not:

**A grant ending is not a lab closing.** Most principal investigators hold several
grants at once. A lab losing one of four is fine — often it barely notices. If you flag
it, a dealer drives two hours to a thriving lab, wastes the trip, and cancels. One bad
list is enough to lose an account in a segment of twenty-five firms.

So the signal is **net funding at the lab level**: across every grant this PI holds, is
their remaining NIH support at or near zero, and has it stayed there? That derivation is
the hard part, it is the only defensible part, and it is what makes the product worth
$999 a month instead of worthless.

**Week one is that question and nothing else.** Not "can I get the data" — the API is
free and keyless. "Can I separate a dying lab from a healthy one."

---

## Phase 1 — the list

**Target: first revenue in 4–6 weeks. No website. No calls.**

### What you build

1. Pull NIH RePORTER (`api.reporter.nih.gov` — free, keyless, commercial use permitted,
   coverage back to FY1985).
2. Roll every award up **per principal investigator**, not per grant.
3. Detect the end state: awards concluded or not continued, and **no remaining active
   support**. Verify whether terminations carry an explicit flag or must be inferred
   from an award ending before its projected end date with no continuation.
4. Filter by region.
5. Rank by dollar size of what was lost — a $2.4M lab has real instruments, a $150k
   pilot grant does not.

That is the product. It is a script and a spreadsheet.

### Who you email

Fifteen to twenty-five firms, and the list is nearly complete already: **EquipNet**,
**Copia Scientific** (the merged BioSurplus / BioDirect / Boston Microscopes),
**American Laboratory Trading**, **Surplus Solutions**, **The Lab World Group**,
**Lab Liquidators**, **LabX**. Founder, VP of Acquisitions, or head of asset recovery —
small firms, usually the owner.

These are the only people you cold-email. Everyone else comes later and comes inbound.

### What you send

Not a pitch. The list itself.

> **Subject:** 6 labs in the Northeast lost all NIH funding this quarter
>
> Dr. Sarah Chen — University of Michigan, Molecular Biology.
> R01 ended 31 May 2026, $2.4M over 5 years, no continuation. Remaining NIH support: $0.
>
> *(five more)*
>
> I compute this weekly from public NIH data. Reply if you want next week's.

They can verify every row independently, which is exactly why it works. You are not
asking them to trust you — you are handing them something checkable.

### What you charge

$999/month, **per region**. Dealers work regions, not the country; per-region lets a
one-metro firm start small and a national one pay more. One recovered liquidation
covers a year, so this is defensible and probably still cheap.

### The number that means it is working

Not signups. Not opens. Ask each dealer directly:

> **"Did you buy anything because of a lab we flagged?"**

One yes is worth more than a thousand website visitors. It is the only evidence that
step 0 in the chain ever happens.

---

## Phase 2 — the site, and only now

Build the public tracker **after** a dealer has paid, because now it has two jobs that
Phase 1 does not need:

1. **Reach the volume segments.** Small life-science vendors, independent
   manufacturer's reps and regional distributors, and life-science recruiters — buyers
   too numerous to email by hand, who have to arrive inbound.
2. **Start the archive.** Every daily run stamps what was knowable that day. This is the
   point-in-time history Phase 3 sells, and it cannot be backfilled later.

The public page shows **institution-level totals only** — "University of X is down $14M
this year, down 22%," ranked by state. It ranks for searches researchers, administrators
and journalists are already making, and press citation is free distribution.

### Pricing, corrected

The earlier ladder was priced by *who you are*. The right unit is **how much of the map
you are watching** — territories cannot be shared away, seats can.

| Tier | Price | What it is |
|---|---|---|
| Free | $0 | 1 institution, weekly, aggregate only |
| Standard | $99/mo | 25 institutions, daily alerts |
| Pro | $299/mo | 200 institutions, full history, export |
| Dealer | $999/mo per region | Unlimited + closure feed |

**Gate by kind, not by quantity.** Free is institution-level totals. Paid is lab-level
with the net-funding derivation. Stack a hundred free accounts and you still never get a
single lab name — so multi-accounting becomes pointless rather than policed. That is the
whole anti-abuse strategy; add email verification and an IP rate limit and stop there.

Note the honest weakness: the middle tiers are guesses. Nobody can price them correctly
before real conversations happen. Let inbound users define them.

---

## Phase 3 — the fund product

Unchanged in logic, and it is why Phase 2's daily discipline matters.

**The operator pays you to build the fund asset.** What investment funds demand, and
almost no vendor can supply, is *provable point-in-time history* — a record of what was
knowable on each date with no retroactive revision. Running a daily product **is**
point-in-time collection. Twelve months of paying customers produces exactly the artifact
that unlocks an ~$80,000/year contract, and you were paid to produce it.

- **Buyer:** Head of Data Sourcing at a life-science-focused or generalist fund.
- **Found via:** Neudata and Eagle Alpha (funds subscribe specifically to discover
  datasets); optionally Snowflake or Databricks marketplaces at 90% revenue share.
- **You win on:** time-to-first-backtest. Ship institution-level flows already mapped to
  tickers (Thermo, Danaher, Illumina, Revvity, Bruker, Agilent), numeric features not raw
  JSON, an explicit `as_of` on every row, and a written PIT completeness statement.
- **Expect** fewer than 1 in 5 trials to convert.

**Why yours is credible and a competitor's is not:** a fund's real question is *does this
funding signal translate into purchasing?* Phase 1 and 2 answer it with measured customer
behaviour rather than assertion.

### Move to Phase 3 only when all three hold

1. ≥ 9 months of continuously collected daily snapshots.
2. ≥ 25 paying customers across the volume tiers.
3. The public backtest separates from the base rate.

If funding does not lead tools revenue, you have a good lead-gen business and no
alt-data product. Say so and keep the good business.

---

## Timeline and gates

| When | Work | Gate |
|---|---|---|
| **Week 1** | Pull RePORTER. Roll up per PI. Build net-funding detection. | **Hard gate.** If you cannot separate dying labs from healthy ones, stop — everything downstream fails. |
| **Week 2** | Generate regional lists. Hand-verify 20 flagged labs against department pages and news. | If the hand-check shows healthy labs, the derivation needs work, not the market. |
| **Week 3** | Build the dealer list — 15–25 firms, named contacts. | — |
| **Week 4** | Send the first regional lists. Free. | — |
| **Week 5–6** | Convert repliers to $999/region. | **Hard gate: 1 paying dealer by week 8.** If nobody pays for a verified list of closing labs in their own region, the premise is wrong. |
| **Month 2–3** | Build the public tracker. Begin daily snapshots — never miss one. | Missing snapshots break point-in-time integrity permanently. Treat the cron as production. |
| **Month 3–9** | Grow the volume tiers to 25+ customers. | — |
| **Month 9–15** | Backtest, tearsheet, Neudata listing, fund trials. | Backtest must separate from base rate. |

**Capital required: an email address.** The website comes later and costs a domain.

---

## Honest risks

- **The derivation may not work.** RePORTER may not make terminations and
  non-continuations cleanly detectable. This is the single biggest technical risk and it
  resolves in week one, for free.
- **Dealers may already know.** They have networks, facilities contacts, and university
  relationships. The pitch depends on being *earlier* than those, which is testable in
  week five and not before.
- **Small segment.** Twenty-five firms is not a business by itself. It is the beachhead
  that proves the signal and funds the volume tiers.
- **Contraction cuts both ways.** Fewer awards means less signal volume, and a sharp
  funding recovery dates the closure framing. Build around *funding change*, not
  *funding loss*, so it survives either regime.
- **Non-stationary history.** The 2025–26 disruption is a structural break sitting in the
  middle of any backtest window. Document it before a quant finds it.
- **Optics.** You are publishing information about researchers losing funding. Keep the
  public page aggregate-only, keep the paid view strictly factual — grant X ended on date
  Y — and never assert that a lab is closing. You do not know that, and it is the safer
  claim as well as the truer one.

## Week one, concretely

1. Hit `api.reporter.nih.gov` and pull five years for one large state.
2. Roll every award up per principal investigator.
3. Determine whether "no remaining active support" is reliably derivable.
4. List the labs it flags. Hand-check twenty against department pages.
5. **If those labs are genuinely winding down, you have a product. If half are thriving,
   you have work to do — and you found out in week one, for nothing.**

## Sources

- **NIH RePORTER** — [API v2](https://api.reporter.nih.gov/), [repoRter.nih R interface](https://cran.r-project.org/web/packages/repoRter.nih/vignettes/repoRter_nih.pdf)
- **Dealers** — [EquipNet](https://www.equipnet.com/), [Copia Scientific](https://www.excedr.com/blog/lab-equipment-from-copia-scientific-formerly-biosurplus), [American Laboratory Trading](https://americanlaboratorytrading.com/), [Surplus Solutions](https://ssllc.com/), [The Lab World Group](https://www.thelabworldgroup.com/), [Lab Liquidators](https://labliquidators.com/)
- **Volume-segment venues** — [SAMPS](https://www.samps.org/), [GenomeWeb](https://www.linkedin.com/company/genomeweb-llc), [LabX Media Group](https://www.labxmediagroup.com/), [Life Science Connect](https://lifescienceconnect.com/communities/)
- **Funding contraction** — [AAU on the grantmaking slowdown](https://www.aau.edu/newsroom/leading-research-universities-report/data-show-dramatic-slowdown-nih-grantmaking), [STAT researcher survey](https://www.statnews.com/2026/03/19/nih-funding-national-researcher-survey-finds-cutbacks-disruptions/), [PMC on 2025 terminations](https://pmc.ncbi.nlm.nih.gov/articles/PMC13037894/)
- **Alt-data mechanics** — [Neudata market report](https://www.neudata.co/blog/state-of-the-alternative-data-market-2026), [Eagle Alpha on point-in-time](https://www.eaglealpha.com/2024/05/06/point-in-time-alternative-data/)
