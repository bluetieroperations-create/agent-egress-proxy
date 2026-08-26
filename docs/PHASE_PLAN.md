# Fast dollar → highest ceiling: one dataset, two buyers, sequenced

**Date:** 2026-08-25 · **Resolves:** `docs/REQUIREMENTS.md` E1 · **Answer:** NIH RePORTER

## The structural move

"Fast dollar then ceiling" has an obvious reading — build a cheap thing, make money,
abandon it, build the expensive thing — and that reading is wrong. It splits your
attention, and the fast product teaches you nothing about the slow one.

The version that works: **pick one dataset that has two buyers at different price
points and different speeds, where serving the fast buyer is how you manufacture the
asset the slow buyer requires.**

> **The operator pays you to build the fund asset.**

That is only possible because of a specific technical fact. The thing funds demand and
almost no vendor can supply is **provable point-in-time history** — a record of what was
known on each date, with no retroactive revision. Running a daily alerting product *is*
point-in-time collection. Every alert you send is a timestamped observation. Twelve
months of serving $199/month customers produces exactly the artifact that unlocks an
$80,000/year contract, and you were paid to produce it.

## The pick: NIH RePORTER

Verified this session:

- **Free, authoritative REST API. No authentication. Commercial use permitted.**
- Coverage back to **fiscal year 1985** — projects, PIs, institutions, amounts,
  activity codes, linked publications, patents and clinical studies.
- No scraping, no ToS negotiation, no PII, no MNPI. Clears fund compliance trivially
  (see `REMOTE_FIRST.md`).

It is the only candidate in either set that scores maximum on both phases:

| | Fast lane | Ceiling |
|---|---|---|
| **Buyer** | Life-science vendor reps and small distributors | Life-science tools funds |
| **Price** | $99–299/month, card, self-serve | ~$80k/year |
| **Cycle** | Days | 3–9 months |
| **Proof needed** | "Did it find me a live account?" | Provable PIT history + backtest |
| **Same corpus?** | **Yes — identical source, different derivation** | |

And the industry has already quantified the fast lane's value proposition for you:
**trigger-based prospecting on grant events lifts response rates from 1–2% to 15–25%.**
You are not asking a rep to believe a new idea; you are selling them a documented
10x on a number they already track.

---

## The complication — and why it is actually the wedge

Federal science funding is in severe contraction, and I am not going to pretend
otherwise:

- **2,291 active NIH grants terminated in early 2025**, withdrawing **$2.45 billion**.
- **5,844 NIH and 1,996 NSF grants** cancelled or suspended in total.
- Only **35%** of affected researchers had funding fully restored by end of 2025.
- NIH published **14 NOFOs in CY2026 to mid-March, versus 756 in all of 2024**.

The naive fast product — "alert me when a lab near you wins a grant" — depends on award
volume, and award volume has collapsed. Built that way, this dies in month three.

**So invert it.** In a contraction the urgent question for a vendor rep is not *"who just
got funded?"* It is:

> **"Which of my accounts just lost their funding, and which ones still have money?"**

Every rep's territory is being quietly destroyed and they are finding out when the PO
never comes. Every incumbent in academic-market intelligence was built for the
expansion era and answers the growth question. **Nobody is selling the contraction
answer**, and it is a harder, more urgent, more valuable question — a rep who stops
working three dead accounts and doubles down on a solvent one gets paid more this
quarter.

That reframe is the whole fast product.

---

## Phase 1 — worked backwards from the card swipe

**Target: first revenue inside 6 weeks, no calls, no travel.**

### T-0 · The purchase
A regional sales rep or a small lab-supply distributor enters a card on a pricing page.
**$149/month.** No demo, no procurement, no signature. Cancel anytime. They expense it,
or their manager does, and neither needs approval at that number — this price point is
chosen precisely because it sits under the threshold where anyone has to ask permission.

### T-1 · What makes them enter the card
They ran the free version against their own territory and it showed them something they
did not know — three accounts whose funding ended, one that just renewed large. The
free tier does one territory, delayed. **Paid does unlimited territories, daily, with
alerts.** The conversion moment is recognition, not persuasion.

### T-2 · How they land on the page
A **free public funding tracker**: which institutions and departments gained and lost
NIH funding, by state, updated weekly, with no signup. This is the lead magnet, the SEO
asset, and the credibility artifact all at once. It ranks for the searches that
worried researchers, administrators and reps are already making, and journalists cite
it — which is free distribution, exactly as in `FMCSA_GTM.md`.

### T-3 · How anyone hears about it
LinkedIn, where life-science reps genuinely live, plus the tracker itself ranking. Post
the findings, not the product: *"the 20 institutions that lost the most NIH funding this
quarter."* Every rep who sells into those institutions shares it, and every one of them
has a territory problem.

### T-4 · What you must be able to say
A per-institution, per-department funding delta — up, down, ended, renewed — that is
correct and current. Nothing more sophisticated than that is required to charge $149.

### T-5 · The build
RePORTER API pull → per-institution and per-PI funding time series → change detection
(new award, renewal, early end, non-continuation) → territory filter (state, zip,
institution list the user types in) → email digest. **No integration with the customer's
systems**, which is why this ships in weeks rather than quarters.

### T-6 · The data
`api.reporter.nih.gov`. Free, keyless, commercial use permitted, back to 1985.

**Week-1 verification, and it is load-bearing:** confirm how terminations and
non-continuations actually surface in RePORTER. Terminated awards may not carry an
explicit flag — you may have to *infer* them from an award ending before its projected
end date with no continuation. If that inference is reliable, it is a genuine moat
because it is real work. If it is not reliable, the contraction framing weakens and you
fall back to renewals and new awards. **Verify before building anything else.**

---

## Phase 2 — the bridge, which requires no separate decision

You do not stop and pivot. You keep running Phase 1, and three things accrue on their
own:

1. **Point-in-time history.** Every daily run is a timestamped snapshot. After twelve
   months you hold something no competitor can buy or backfill: a verified record of
   what was knowable on each date. This is the gate that kills most alt-data vendors,
   and you clear it as a by-product of serving paying customers.
2. **Signal validation from real behaviour.** Which alerts do customers act on? Which do
   they ignore? That is a labeled dataset about which funding events actually predict
   purchasing — and predicting purchasing is precisely what a tools fund wants to buy.
3. **Credibility.** "Used by N life-science vendors" is a materially better opening line
   to a fund than "I built a dataset."

Note the shape: this is the same flywheel as `ledger.py` in this repo. Public data is
the cold start; **observed outcomes are the moat.**

---

## Phase 3 — worked backwards from the $80,000 signature

**Target: first fund contract months 9–15.**

### T-0 · The signature
A Head of Data Sourcing at a life-science-focused or generalist fund signs an annual
subscription, ~$50–100k, over email and Zoom.

### T-1 · The trial
They load a sample and try to reproduce a result. You win on **time to first backtest**:
ship institution-level funding flows already mapped to tickers for the listed tools
companies (Thermo, Danaher, Illumina, Revvity, Bruker, Agilent), numeric features not
raw JSON, an explicit `as_of` on every row, and a written PIT completeness statement.
Expect **fewer than 1 in 5 trials to convert** and plan for volume.

### T-2 · How they find you
List on **Neudata** and **Eagle Alpha** — funds subscribe to these specifically to
discover datasets. Optionally list the derived panel on Snowflake or Databricks (90%
revenue share to you).

### T-3 · The thing that makes the listing work
A **public backtest**: does institutional NIH funding lead reported academic-segment
revenue at a listed tools company, and by how many quarters? One chart, one table, one
methodology note, false-positive rate stated.

### T-4 · Why yours is credible and a competitor's is not
Because you can prove the history. And because Phase 1 gave you the answer to the
question a fund actually asks — *does this funding signal translate into purchasing?* —
measured on real vendor behaviour rather than asserted.

### T-5 · The build
The Phase 1 corpus, re-derived: institution → parent → ticker mapping, sector and
instrument-category rollups, lead-lag features, PIT-stamped panel export.

### T-6 · The data
The same API. You never changed source.

---

## The switch trigger, stated explicitly

Do not move on a date. Move when **all three** are true:

1. **≥ 9 months** of continuously collected PIT snapshots.
2. **≥ 25 paying operator customers** — enough that "used by N vendors" is true and
   enough that Phase 1 covers your costs.
3. The public backtest **separates from the base rate**. If institutional funding does
   not lead tools revenue, you have a fine lead-gen business and no alt-data product.
   Say so and keep the good business.

Until all three hold, Phase 1 *is* the work.

---

## Timeline and kill gates

| When | Work | Gate |
|---|---|---|
| **Week 1** | Pull the API. Verify termination/non-continuation detectability. Build institution time series. | If change events cannot be reliably derived, the wedge is gone — reconsider before writing a line of product. |
| **Week 2–3** | Build and publish the free public funding tracker. | If it produces no rankings anyone finds surprising, the lead magnet fails. |
| **Week 4** | Territory filter + email digest + Stripe. Ship the $149 tier. | — |
| **Week 5–6** | Publish findings on LinkedIn and pitch the tracker to trade and science press. | **Hard gate: 3 paying customers by week 8.** If nobody pays $149 for this, the premise that funding events drive purchasing is weak — which also undermines Phase 3. |
| **Month 3–9** | Grow to 25+ customers. Snapshot daily without fail. | Missing snapshots break PIT integrity permanently. Treat the cron as production. |
| **Month 9–12** | Run the backtest. Write the tearsheet. List on Neudata and Eagle Alpha. | Backtest must separate from base rate. |
| **Month 12–15** | Fund trials. | <20% conversion is normal, not failure. |

**Total capital required: a domain, an email sender, and Stripe.** No data spend.

---

## Honest risks

- **The contraction cuts both ways.** Fewer awards means less signal volume, and if
  federal science funding recovers sharply the contraction framing dates quickly. The
  product should be built around *funding change*, not *funding loss*, so it works in
  either regime.
- **Non-stationary history.** The 2025–26 disruption is a structural break in the middle
  of your backtest window. A quant will find it. Document it before they do — an
  undisclosed regime break is what gets a vendor dropped.
- **Phase 1 might succeed too well.** If the lead-gen product grows fast, Phase 3 becomes
  a distraction rather than a graduation. That is a good problem, but decide
  deliberately rather than drifting.
- **The two-buyer bet could fail on one side.** The fast lane could work while the
  backtest shows nothing. That outcome still leaves you a profitable, remote,
  zero-capital business — which is why this sequencing is low-regret.

## Week one, concretely

1. Hit `api.reporter.nih.gov`, pull five years for one large state.
2. Determine whether early terminations and non-continuations are reliably derivable.
3. Build the per-institution funding delta series.
4. Rank the top 20 institutions by funding gained and lost this year.
5. Look at that list. **If it surprises you, it will surprise a rep — and that reaction
   is the entire product.**

---

## Who you sell to in Phase 1, and how

Answered to the same standard as `FMCSA_GTM.md`: named segments, named companies,
the title that owns the card, and the actual mechanism.

### The honest headline

**In Phase 1 you do not sell. You publish, and the product sells itself.** There is no
demo, no call, no proposal. At $149/month nobody needs approval, so the entire job is
getting the right person to look at their own territory once. Everything below is in
service of that single event.

The exception is one segment small enough to email by hand — and it is the sharpest one.

### The buyers, ranked by "will enter a card without asking anyone"

#### 1. Surplus and used lab-equipment dealers — the contraction-native buyer

This is the segment I underweighted, and it may be the beachhead.

When a lab loses funding it liquidates equipment. These firms run **lab closures as an
explicit service line** — which means *a lab shutting down is literally their lead*.
Your product tells them which labs are about to shut down, weeks before the phone rings.

Named, and the list is nearly complete: **EquipNet**, **Copia Scientific** (the merged
BioSurplus / BioDirect / Boston Microscopes), **American Laboratory Trading**,
**Surplus Solutions**, **The Lab World Group**, **Lab Liquidators**, **LabX**.

- **Who signs:** founder, VP of Acquisitions, or head of asset recovery. Small firms —
  often the owner.
- **Why they're first:** perhaps 15–25 firms nationally, all findable in an afternoon,
  all reachable by email, and each one values a single liquidation lead at tens of
  thousands of dollars. **These are the only people you should cold-email**, and you
  should charge them far more than $149 — a dealer tier at $999/month is defensible
  against one recovered liquidation.
- **The email is not a pitch.** It is: *"Six labs in your region had NIH funding end in
  the last 90 days. Here they are."* Delivery, not persuasion — the same discipline as
  the freight plan.

#### 2. Small and mid-size life-science vendors — the volume

Thousands of reagent, antibody, instrument, consumable and lab-software companies sell
into academic labs. The market is **fragmented enough that the largest antibody supplier
holds under 5% share**, which tells you the long tail is enormous.

- **Who signs:** founder/CEO at the small end, VP Sales or Head of Commercial at the mid.
  They own the card and answer to nobody at $149.
- **Why they buy:** they have *no* market intelligence. The incumbents — Clarivate,
  IQVIA, Salesmotion and friends — are enterprise, custom-priced, and quoted on request.
  **Nothing exists at $149 self-serve.** That gap is your entire pricing position.
- **Honest gap:** I could not find a clean published count of US life-science vendors.
  Building that list is a week-one task and it is entirely public — trade-media
  advertiser lists (GenomeWeb, LabX Media Group), SAMPS membership, and supplier
  directories. **The prospect list is itself a public-data exercise**, which is on-brand.

#### 3. Independent manufacturer's reps and regional distributors

A real and often-forgotten category in lab equipment: commission-driven, territory-based,
and with no data budget whatsoever. **Laboratory Equipment Company**, **Life Scientific
Inc.** and dozens of regional firms operate this way. A rep whose income is a percentage
of a territory has the most direct possible incentive to know which accounts still have
money.

#### 4. Life-science recruiters

Funding changes drive both hiring and layoffs. Recruiters buy tools on a card, move fast,
and a lab that just won a large award is about to post three positions.

**Adjacent, revisit later:** university research-development offices (institutional, so
slower), biotech BD and competitive intelligence, CRO and CDMO business development.

### The mechanism, in three layers

**Layer 1 — the tracker does the selling.** The free public funding tracker ranks for
searches that worried researchers, administrators, reps and journalists are already
making right now. Press citation is free distribution. This is the top of the funnel and
it costs nothing to run.

**Layer 2 — the places your buyer already gathers, all reachable without travel:**

- **SAMPS** — *Serving Sales & Marketing Professionals in Life Science*. Describes itself
  as the first and only organization dedicated to sales and marketing professionals in
  life sciences and applied research, and it runs **newsletters and online events**. This
  is your buyer, pre-assembled, with a remote-accessible stage. Speak at an online event.
- **GenomeWeb** — reaches exactly the supplier-and-executive audience; its advertisers
  *are* your customers.
- **LabX Media Group** and **Life Science Connect** — newsletters, webinars and
  communities aimed at lab professionals and the vendors selling to them.
- **LinkedIn** — where life-science commercial people genuinely live. Post the findings,
  never the product.

**Layer 3 — direct email, to segment 1 only.** Fifteen to twenty-five surplus dealers is
a list you work by hand in a day. Everyone else arrives inbound.

### The funnel, as an actual Tuesday

1. A rep sees a LinkedIn post: *"the 20 institutions that lost the most NIH funding this
   quarter."*
2. They click through to the free tracker and search their own state — because of course
   they do.
3. They recognise three accounts they are still actively working.
4. They hit the limit: one territory, weekly. Unlimited territories and daily alerts is
   the paid tier.
5. Card entered. Elapsed time: under four minutes, no human involved.

### Pricing ladder

| Tier | Price | Who |
|---|---|---|
| Free | $0 | One territory, weekly, no signup — the lead magnet and SEO asset |
| Rep | **$149/mo** | Individual reps, recruiters, independent manufacturer's reps |
| Team | **$499/mo** | Small vendors, 5 seats, shared territory lists |
| Dealer | **$999/mo** | Surplus and liquidation firms — closure alerts, priority feed |

The ladder matters because the dealer tier can carry the business while the volume
segments compound, and it is the tier you can sell by hand starting in week five.

### What you never do in Phase 1

No demos. No discovery calls. No enterprise pilots. No conference booths. The moment
you agree to a call for a $149 product, the economics break and the remote constraint
starts costing you money instead of protecting it.

## Additional sources

- Surplus and liquidation dealers — [EquipNet](https://www.equipnet.com/), [Copia Scientific](https://www.excedr.com/blog/lab-equipment-from-copia-scientific-formerly-biosurplus), [American Laboratory Trading](https://americanlaboratorytrading.com/), [Surplus Solutions](https://ssllc.com/), [The Lab World Group](https://www.thelabworldgroup.com/), [Lab Liquidators](https://labliquidators.com/)
- Where the buyer gathers — [SAMPS](https://www.samps.org/), [GenomeWeb](https://www.linkedin.com/company/genomeweb-llc), [LabX Media Group](https://www.labxmediagroup.com/), [Life Science Connect communities](https://lifescienceconnect.com/communities/)
- Incumbent positioning — [Clarivate life-sciences commercialization](https://clarivate.com/life-sciences-healthcare/commercialization/), [life-sciences sales intelligence tools roundup](https://salesmotion.io/blog/life-sciences-sales-intelligence-tools-biotech)
- Market fragmentation — [Mordor: life science reagents market](https://www.mordorintelligence.com/industry-reports/life-science-reagents-market)

## Sources

- **NIH RePORTER** — [API v2](https://api.reporter.nih.gov/), [repoRter.nih R interface](https://cran.r-project.org/web/packages/repoRter.nih/vignettes/repoRter_nih.pdf)
- **Trigger-based prospecting lift** — [Landbase on biotech B2B lead generation](https://www.landbase.com/blog/b2b-lead-generation-biotech-companies), [Callbox life-science marketing guide](https://www.callboxinc.com/lead-generation/life-science-marketing-guide/)
- **Funding contraction** — [AAU on the NIH grantmaking slowdown](https://www.aau.edu/newsroom/leading-research-universities-report/data-show-dramatic-slowdown-nih-grantmaking), [STAT national researcher survey](https://www.statnews.com/2026/03/19/nih-funding-national-researcher-survey-finds-cutbacks-disruptions/), [PMC on 2025 grant terminations](https://pmc.ncbi.nlm.nih.gov/articles/PMC13037894/), [Nature: US science after a year](https://www.nature.com/immersive/d41586-026-00088-9/index.html)
- **Alt-data buyer mechanics** — [Neudata market report](https://www.neudata.co/blog/state-of-the-alternative-data-market-2026), [Eagle Alpha on point-in-time](https://www.eaglealpha.com/2024/05/06/point-in-time-alternative-data/)
