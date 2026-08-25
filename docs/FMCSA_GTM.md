# Pick one: FMCSA carrier lineage — and the path backwards from the cheque

**Date:** 2026-08-25 · **Companion to:** `docs/COLD_START_DATA.md` · **Status:** GTM plan

## The answer, stated plainly

**Opportunity #02 — FMCSA carrier identity/lineage scoring.** It is the easiest of the
ten by a wide margin, and it is the most profitable *only if you refuse to sell it the
obvious way*. Those two claims are the whole memo.

**Why it is easiest.**

- The data is four **free bulk files** on the DOT open-data portal. No scraping, no
  ToS grey zone, no per-record fee, no county-by-county grind. Contrast #07
  (construction liens), which needs an acquisition team before it needs an engineer.
- It is **already outcome-labeled**: out-of-service orders, authority revocations,
  crashes, insurance lapses. The training set ships with the download.
- **The algorithm is already written in this repo.** Chameleon detection is an
  anchor-and-ring identity graph — `payer_reputation.py` and `payer_graph.py`
  structurally, `sybil_ring` semantically. This is a port, not an invention.
- The validation target is **public and pre-published**: FMCSA's own analysis puts
  reincarnated carriers near **3× the serious-crash rate** of legitimate new entrants.
  You are not guessing whether the signal exists. You are reproducing a known number
  and turning it into a per-entity score.

**Why "most profitable" has a condition attached.**

The obvious move is to sell carrier vetting to freight brokers. Do not. That market is
already commoditized: Carrier411's broker plan runs **$149/month**, CarrierOK starts at
**$50/month per user** with an API tier at **$249/month**. Highway, Carrier Assure and
Descartes MyCarrierPortal own the broker desk. Competing there means fighting entrenched
incumbents for $150 seats — high effort, low margin, no leverage.

The money is one step sideways, with buyers who carry the loss rather than route the
freight:

| Buyer | What they lose today | What a lineage score is worth |
|---|---|---|
| **Freight factoring companies** | They advance cash against invoices from carriers they onboarded. A chameleon carrier is a direct write-off — often the full advance. | One prevented fraud loss typically exceeds a year of subscription. |
| **Cargo & contingent-cargo insurers / MGAs** | Cargo theft losses hit **$304.6M in Q2 2026 alone**, up from $135.7M a year earlier; ~$725M across 2025, +60% YoY. ATRI puts the average annual loss at **~$520k per carrier**, with 75% of stolen cargo never recovered. | A pricing and eligibility variable underwriters do not currently have. |
| **Platforms** (TMS, load boards, factoring software, the vetting incumbents themselves) | They have the workflow but not the lineage graph. | A data layer they licence rather than build. |

Industry-wide freight fraud is put at roughly **$800M/year** by the 2026 FreightWaves
Freight Fraud Symposium, with TIA quoting figures up to **$35B** for organized theft
overall. Treat the $800M as the defensible number and the $35B as directional — it is a
trade-association estimate over a much broader definition. Even the conservative figure
is enormous relative to what anyone currently spends on preventing it.

**Runner-up and why it lost.** #01 (Chapter 11 creditor graph) has the higher ceiling —
a twenty-year B2B credit graph is a structural monopoly. But it needs portal-by-portal
document acquisition, PDF parsing, and creditor-name resolution before it produces its
first number, and the buyer (trade credit insurance) has a long enterprise sales cycle.
It is the better *second* build, on the entity-resolution spine that #02 forces you to
write anyway.

---

## Working backwards from the cheque

Seven links. Read them in this order to understand the logic; execute them bottom-up.

### T-0 · The moment money changes hands

**Who signs:** VP of Risk or Chief Credit Officer at a freight factoring company.
Secondary: Head of Underwriting at a cargo MGA.

**Which budget it comes from:** the **fraud loss reserve**, not the software budget.
This distinction decides your price. Software budgets are defended per-seat and
benchmarked against $149/month tools. Loss reserves are measured against the losses
they exist to absorb, and a buyer will spend 5% of a reserve to cut it by 30%.

**What is on the page:** a per-account lineage score with evidence, delivered at
onboarding and re-run monthly on the existing book.

**Price:** anchor to loss avoided, not to seats. A pilot at $2–5k/month per factor is
defensible against a single six-figure write-off. Three design partners is $6–15k MRR.

### T-1 · The one thing that closes it: a retro on their own book

Nothing else closes this sale. Not a demo, not a deck, not a landing page.

You take 12–24 months of their onboarded-carrier list, run lineage detection
**retroactively as of each onboarding date**, and produce one sentence:

> "Of the 41 accounts you charged off last year, 17 carried a lineage flag on the day
> you onboarded them. Here are all 17, with the predecessor entity and the shared
> identifiers."

That is the entire product pitch. It is falsifiable, it is computed on their data, and
it converts a subjective "interesting" into a number their CFO already tracks.

**Two disciplines that make it credible:** score strictly point-in-time — no using
facts that postdate the onboarding decision, or you have built a time machine, not a
model. And report the false positives in the same breath: "we also flagged 9 accounts
that performed fine." A vendor who volunteers their false-positive rate is the only
kind worth believing, and it is the same instinct as this repo's reversibility locks.

### T-2 · The meeting that gets you their book

A risk officer does not hand a stranger their charge-off list. They will hand it to
someone who has already shown them something they cannot get anywhere else.

So the ask is not "can we see your data." It is: **"we found N active clusters of
reincarnated carriers operating right now. Three of them are in your state. Want us to
check whether any are on your book?"** That is a specific, free, immediately valuable
offer, and the answer is yes.

### T-3 · The outreach that gets the meeting

Not cold email about a product. **Publish the finding.**

Freight is an unusually press-responsive industry with an active fraud beat —
FreightWaves, Overdrive, CCJ, Trucking Dive, Land Line — plus a live conference circuit
(the 2026 Freight Fraud Symposium exists precisely because this is the industry's top
concern). A credible, independently-computed count of live chameleon clusters is a
story any of them will run.

Publish it as a **report with methodology, not a press release**. Anonymize the named
entities in the public version — that is both the legally safe posture and the reason
people call you to ask which ones they are. Inbound from that report is your pipeline;
you should not need to cold-call factors at all.

### T-4 · The number that makes it publishable

Two claims, both computed, both defensible:

1. **Reproduce the multiple.** Do flagged reincarnated entities show materially worse
   subsequent OOS/crash outcomes than genuinely new entrants? FMCSA's published
   analysis says ~3×. If you independently reproduce it, you have validated your
   detector against an authoritative prior.
2. **Count the live clusters.** How many revoked→new-entity pairs are active *today*?
   This number has, as far as I can find, never been published. It is the headline.

If #1 does not reproduce, the detector is wrong and you stop. That is the kill gate,
and it fires in week 2, not month 9.

### T-5 · The detection build

- **Identity keys:** normalized street address, phone, company officer name, email
  domain, and — if available in the inspection files — tractor VIN. The census
  (data.transportation.gov, Socrata resource `az4n-8mr2`) carries officer names from
  the MCS-150. **Verify VIN availability in the inspection extract on day one**; the
  cluster rule's strength depends on how many independent keys you actually have.
- **Cluster rule:** a revoked/OOS DOT number and a newly-registered DOT number sharing
  **≥2 independent keys** within 90 days. Two keys, not one — a shared registered-agent
  address alone is a legitimate commercial-registration artifact and will bury you in
  false positives. This is exactly the lesson `burst_sybil` taught in this repo, where a
  single-axis signal flagged the *most* reputable entities.
- **Score, don't verdict:** emit tiers with evidence and confidence, in the
  `confidence.py` shape — `backed_by[]` and `missing[]`. Never emit an unexplained
  block.

### T-6 · The data

Download four free bulk files: census, out-of-service, inspections, crashes. This is a
day, and it is the only dependency the entire plan has.

---

## Execution order, with kill gates

| Week | Work | Gate |
|---|---|---|
| **1** | Download all four files. Confirm which identity fields actually exist — especially officer name coverage and VIN. Build normalization. | If officer + address + phone coverage is too sparse to form 2-key clusters, the whole plan dies here, cheaply. |
| **2** | Build the cluster rule. Run it. Reproduce the ~3× outcome multiple on historical data. | **Hard gate.** No reproduction, no business. Stop and go to #01. |
| **3** | Count live clusters. Hand-verify the top 50 by eye — actually read the records. Write the methodology. | If hand-verification shows the clusters are junk, the rule needs work, not the market. |
| **4–5** | Publish the report, anonymized. Pitch the freight trade press. Present the number, not a product. | If no press interest and no inbound, the number isn't surprising enough — sharpen or reconsider. |
| **6–9** | Convert inbound into 3 retro analyses on real books. Free. This is not a pilot, it is the sales motion. | If retros do not find flagged charge-offs, the signal does not predict *their* loss type. Learn which loss type it does predict. |
| **10–13** | Convert retros to paid monitoring: score at onboarding + monthly re-score of the book. | Target: 3 design partners, $6–15k MRR. |
| **Q2** | Cargo insurers / MGAs as a data feed. Then platform licensing. | |

**Total pre-revenue cost: one engineer, roughly six weeks, no data spend.** That is
what "easy" means here, and no other entry in the ten-list comes close to it.

---

## Sequencing the revenue

1. **Design-partner monitoring** ($2–5k/mo × 3). Fastest cash, and it generates the
   outcome ledger that becomes the actual moat.
2. **Insurer / MGA data feed** (annual licence, five figures and up). Underwriters
   already pull FMCSA BASIC percentiles from third-party aggregators, so the
   procurement path is worn — you are adding a variable to an existing feed, not
   introducing a new category.
3. **Platform / OEM licensing.** Sell the lineage layer *to* the vetting incumbents and
   the TMS/load-board/factoring-software vendors rather than fighting them for the
   broker desk. Picks and shovels beats a price war you would lose.
4. **Agent-native endpoint.** `mcp_server.py` and `x402.py` already exist; a new domain
   is a new `lookup()` seam. Low effort, optional, and strategically consistent.

**Explicitly do not sell $149/month broker seats.** It is the largest-looking channel
and the worst one.

---

## What actually kills this

- **Field sparsity.** If officer names and VINs are too thin in the public extracts,
  the 2-key rule degrades to address-matching and drowns in false positives.
  *This is the single biggest technical risk and it resolves in week 1.*
- **FMCSA policy change.** Post-FAST Act, some property-carrier BASICs are already
  hidden from public display, and the MOTUS rollout is explicitly about identity
  verification — the agency could close this gap itself, or restrict the underlying
  data. Build on what is public today and keep your own snapshots; a historical archive
  nobody else kept becomes an asset if the tap narrows.
- **Defamation.** Publicly calling a real carrier a chameleon is actionable if wrong.
  Never publish named accusations. Deliver evidence with citations to the customer,
  scored not verdicted, with a documented dispute path — the same discipline as
  `blockscout.py`'s hard boundary and this repo's HOLD-only gates.
- **Channel size.** There are only ~247 invoice factoring businesses in the US, and the
  count is *shrinking* (down 5.4% in 2025, 16.1% the year before). Factoring alone is
  too small a TAM to be the whole company — it is the beachhead that proves the signal
  and funds the expansion into insurance and platform licensing.
- **Incumbent response.** Highway or Descartes could add lineage detection. Your defence
  is the accumulated outcome ledger (which clusters actually went bad, per customer,
  over time) and the historical snapshot archive — neither of which is downloadable
  after the fact. Same moat logic as `ledger.py`.

---

## Honest ceiling

This is a **$1–5M ARR niche business** on a realistic path, not a $100M one — unless the
lineage graph becomes the de facto identity layer for freight, which is possible but
should not be underwritten in the plan. What it *is*, unambiguously: the fastest route
from zero to a paying customer of anything in the ten-list, on free data, with an
algorithm this repo has already written twice, validated against a number the federal
government has already published.

And it forces you to build US business entity resolution — the substrate under six of
the other nine opportunities.

## Sources

- Cargo theft losses — [Verisk Q2 2026: $304.6M](https://www.verisk.com/company/newsroom/cargo-theft-losses-more-than-double-to-$304-million-in-q2-despite-a-drop-in-thefts-driven-by-high-value-metals-and-technology-heists/), [Insurance Journal](https://www.insurancejournal.com/news/national/2026/08/14/881495.htm), [HDT on 2025 totals](https://www.truckinginfo.com/news/cargo-theft-losses-more-than-double-in-q2-despite-fewer-incidents)
- Industry fraud totals — [Freight Fraud Symposium 2026 ($800M)](https://idispatchhub.com/freight-fraud-symposium-2026-convenes-at-the-rock-roll-hall-of-fame-ai-deepfakes-800-million-in-annual-industry-losses-identity-theft-schemes-and-the-new-security-standards-every-independent-ca/), [Inbound Logistics on the fraud surge](https://www.inboundlogistics.com/articles/risky-business-inside-the-freight-fraud-surge/)
- Chameleon carriers & FMCSA response — [Trucksafe on ARCHI/MOTUS](https://trucksafe.com/post/chameleon-carriers-fraud-detection-and-fmcsa-s-evolving-data-strategy), [CNS on MOTUS](https://www.cnsprotects.com/news/fmcsa-anti-chemeleon-carrier-strategy/)
- Data access — [FMCSA Open Data Program](https://www.fmcsa.dot.gov/registration/fmcsa-data-dissemination-program), [Motor Carrier Census Files](https://catalog.data.gov/dataset/motor-carrier-registrations-census-files), [MCS-150 form](https://www.fmcsa.dot.gov/sites/fmcsa.dot.gov/files/2026-02/MCS-150%20Form.pdf)
- Incumbent pricing — [Carrier411 alternatives roundup](https://carrierowl.com/blog/carrier411-alternatives), [Carrier Assure pricing](https://www.carrierassure.com/pricing)
- Factoring market size — [IBISWorld: invoice factoring business counts](https://www.ibisworld.com/united-states/number-of-businesses/invoice-factoring/5407/), [IFA on factoring fraud](https://magazine.factoring.org/magazine-articles/march-7-navigating-the-road-ahead-combating-factoring-fraud-in-the-freight-transportation-industry)
- Insurance underwriting data practice — [CarrierIQ on trucking underwriting data](https://carrieriq.io/blog/trucking-insurance-underwriting), [Foley on CSA scores and premiums](https://www.foleyservices.com/articles/csa-score-insurance-rates/)
