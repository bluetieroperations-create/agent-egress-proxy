# Cold-start data monetization — 10 public datasets nobody has turned into a product

**Date:** 2026-08-25 · **Status:** strategy memo, no code · **Author:** research pass

## Why this memo exists

This repo already proved a specific play twice:

- `chain_backfill.py` — seed counterparty reputation from **public Base USDC history**
  with **zero customers**, by paginating a known payee's inbound transfers.
- `rwa_backfill.py` — same trick for tokenized RWAs: *"on-chain history is already
  LABELED: a token transfer that landed IS a successful settlement."* It moved Ondo
  from `insufficient` to a graded issuer off 74 real acquisitions / 33 distinct buyers.
- `ecosystem_scan.py` — one crawl, four products: instant verdicts, market stats,
  a trust directory, and a BD funnel.

The generalizable asset is not crypto. It is a **method**: find public data that is
*already outcome-labeled*, resolve entities across it, and ship a counterparty score
before you have a single customer. Everything downstream of that in this repo —
`reputation_store.merge_records`, `payer_graph`, `payer_reputation`, `confidence`,
`seller_audit`, the HOLD/STOP gate discipline, the reversibility locks — is
**domain-agnostic machinery**. It is currently pointed at one market.

This memo points it at ten others.

---

## The rubric — what makes a dataset cold-start bootstrappable

A dataset qualifies only if it passes all five. Most "open data" fails #2 and #4.

| # | Test | Why it decides everything |
|---|------|---------------------------|
| **1** | **Free + bulk-reachable** | If it needs a key, a contract, or a per-record fee, your unit economics are someone else's business model. Monthly bulk file > paginated API > HTML scrape > PDF. |
| **2** | **Already outcome-labeled** | The data must contain the *failures*, not just the roster. A registry of carriers is a phone book; a registry of carriers **plus out-of-service orders** is a training set. This is the `rwa_backfill` insight verbatim. |
| **3** | **Entity resolution is hard** | The moat is not the download. It is joining `ACME LOGISTICS LLC` at 400 Main St to `ACME TRANSPORT INC` at 400 Main Ste B. Anyone can `curl`. Almost nobody does the join well, and the join is what compounds. |
| **4** | **A buyer already loses money to the gap** | Someone must currently be eating fraud, defaults, recalls, or wasted BD spend. "Interesting" is not a market. |
| **5** | **Survivorship bias is closable** | `revert_scan.py` exists in this repo because backfill *only sees successes* and `settle_rate` was always 1.0. Every one of these datasets has the same trap. If you cannot find the denominator (the failed attempts), the score is decorative. |

**Plus the thing that makes it yours:** as in `ledger.py`, the public backfill only
gets you to parity with anyone else who scrapes. The durable asset is **your own
accumulated outcome ledger** — every score you issued and what actually happened
afterward. Public data is the cold start. Observed outcomes are the moat.

---

## The ten

Ranked by (fit to machinery already in this repo) × (how unowned the niche is) ×
(clarity of the buyer).

---

### 1. Chapter 11 creditor matrices → the open B2B trade-credit graph

**The data.** Every Chapter 11 debtor must file Official Form 204 (20 largest
unsecured creditors) *and* a full creditor matrix — every creditor's name and
address — at petition. Big cases publish these free on claims-agent portals (Kroll
Restructuring Administration, Epiq, Stretto, Omni, BMC, Donlin Recano) with no
PACER fee. Proofs of claim and attachments are public. Schedules and SOFAs follow
within weeks and carry **amounts**.

**The label (test #2).** This is the cleanest labeled counterparty-failure dataset in
American commerce: a named business that extended credit, the amount, and the fact
that it did not get paid. Free. Weekly. Forever.

**Why nobody has it.** The restructuring industry treats these as case documents, not
as a dataset. Everyone reads *one* matrix when their customer files. Nobody has
stitched twenty years of them into a graph. Bloomberg/Debtwire sell *case* coverage;
D&B/Experian sell *entity* credit files sourced from tradelines they must buy.
**Nobody sells the edges.**

**The product.** A bipartite supplier↔customer exposure graph — structurally the same
object as `payer_graph.py`. From it: "which of your customers appear as unsecured
creditors of recent filers" (contagion), "this prospect has been burned in 6 filings
in 3 years" (concentration risk), industry-level distress propagation, and the
`captive_sybil`/`sybil_ring` analogue — related-party creditor rings.

**Who pays.** Trade credit insurers (Allianz Trade, Coface, Atradius), invoice
factoring and AR finance, corporate credit departments, distressed debt funds,
claims traders (a creditor list *is* a claims-acquisition target list — that alone
funds the build).

**Cold-start spike (1 week).** Pick the 200 largest Chapter 11 filings of the last 3
years, pull Form 204 from claims-agent portals, normalize creditor names, count
repeat appearances. If the top repeat creditors are recognizable and the overlap is
non-trivial, the graph is real.

**Risks.** Claims-agent ToS varies — read each; several are explicitly public-access.
Entity resolution on creditor names is genuinely hard (that is the moat, see test
#3). Survivorship: you see companies that *filed*, not those that quietly wound down.

---

### 2. FMCSA identity graph → chameleon-carrier and freight-fraud scoring

**The data.** FMCSA publishes, free and in bulk on the DOT open-data portal: the
motor carrier census (active/pending/inactive), inspections, violations, crashes,
operating authority, insurance filings (L&I), and new-entrant out-of-service orders.
Monthly snapshots.

**The label.** Out-of-service orders, authority revocations, crash records, insurance
lapses. Unambiguous, dated, attributable.

**Why nobody has it *properly*.** There are plenty of lookup tools (Carrier Assure,
Highway, MyCarrierPortal, FreightVet). They answer *"is this DOT number bad?"*. The
expensive question is *"is this new, clean DOT number the same humans as that dead,
filthy one?"* — the chameleon carrier. FMCSA's own prototype (ARCHI, 2012) and the
newer MOTUS platform exist precisely because the agency cannot do it at scale.
Reincarnated carriers are reported at roughly **3× the serious-crash rate** of
legitimate new entrants. The detection method is an identity graph over shared
addresses, phones, officers, and VINs — exactly the `payer_reputation` anchor-and-ring
construction in this repo.

**The product.** A carrier identity/fraud score with a *lineage* field: "clean 4-month
DOT number, but shares registered address + officer surname + 3 tractor VINs with
MC-XXXXXX, revoked for OOS in March." Plus a broker-side double-brokering detector.

**Who pays.** Freight brokers and 3PLs (cargo theft and double-brokering losses run
into the hundreds of millions annually), cargo insurers, **freight factoring
companies** (fraud there is acute and they underwrite on thin data), shippers.

**Spike (3–5 days).** Download census + OOS files. Build address/phone/officer keys.
Count clusters where a revoked DOT number shares ≥2 keys with a DOT number registered
within 90 days of the revocation. If that count is large and hand-checks look real,
you have the product.

**Risks.** Post-FAST Act, some property-carrier BASICs (Crash Indicator, HazMat) are
hidden from public display — build on what remains public. Falsely branding a
legitimate carrier a chameleon is a defamation exposure: ship it as *evidence with
citations*, HOLD-only, never an unexplained STOP — the exact discipline in
`blockscout.py` and `settlement_sim.py`.

---

### 3. FDA import refusals + inspections + recalls → supplier/CMO quality score

**The data.** FDA's Data Dashboard exposes APIs for `compliance_actions` and
`inspections_classifications`; import refusals are published monthly (updated weekly)
with **manufacturer name, country, product code, refusal date, and charge code**.
Import Alerts (DWPE lists) name firms under detention without physical examination.
openFDA adds recalls, adverse events (MAUDE), and establishment registrations as bulk
JSON. Warning letters are published as HTML.

**The label.** A refusal is a named foreign manufacturer failing a specific
requirement on a specific date. An inspection classification (NAI/VAI/OAI) is a
graded outcome. A Class I recall is a severe one.

**Why nobody has it.** Consultancies sell *services* around this; regulatory-affairs
software manages *your* filings. There is no widely used **counterparty score** for
"should I source from this contract manufacturer." Buyers currently do it with a
paralegal and a spreadsheet, per supplier, once.

**The product.** A supplier quality score per manufacturer/FEI, with sister-facility
rollup (same corporate parent, different FEI — entity resolution again), trend
detection (an OAI followed by a refusal cluster is a pre-recall signal), and a
"what changed this month in your supplier list" monitor.

**Who pays.** Pharma/nutraceutical/device sourcing and quality teams, private-label
and Amazon-brand aggregators, food importers, customs brokers, supply-chain
insurers, and short-sellers.

**Spike.** Pull 24 months of import refusals, group by manufacturer, cross-join to
inspection classifications and recalls. Ask: does an OAI classification predict a
refusal or recall within 12 months? If yes, that single number is the pitch.

**Risks.** Firm-name normalization across FDA systems is notoriously bad (test #3 —
also the moat). Refusals skew to certain product categories; do not present a
category artifact as a supplier signal.

---

### 4. Secretary-of-State registry churn → SMB mortality and shell-ring detection

**The data.** 50 state business registries. Entity status, formation date, annual
report compliance, **administrative dissolution**, registered-agent resignation,
reinstatement, officer/address changes. Many states publish bulk files; the rest are
searchable.

**The label.** Administrative dissolution is a dated death certificate. Registered
agent resignation is a leading indicator (the agent stopped getting paid). Serial
formations at one address is a fraud-ring fingerprint.

**Why the niche is open.** Middesk, Enigma, Baselayer and friends sell **KYB
verification** — *does this business exist, right now*. That is a point-in-time
lookup. Almost nobody sells the **time series**: velocity of status change, agent
churn, address co-tenancy, officer reuse across entities. It is the difference
between `is_scam` (a tag) and `payer_graph` (a structure).

**The product.** (a) A liveness/mortality score with a 3–12 month horizon; (b) a
shell-ring detector over shared agents/addresses/officers — the direct SMB analogue
of `sybil_ring`; (c) a "your customer just went non-compliant" webhook, which is
almost free to run and immediately sellable.

**Who pays.** SMB lenders and MCA providers, fintech onboarding/KYB, commercial
insurers, B2B marketplaces, franchisors, payment processors sizing merchant risk.

**Spike.** Take 3 states with bulk downloads. Compute the base rate: what fraction of
entities that hit "active/non-compliant" are dissolved within 12 months? That
conversion rate is the entire product thesis, and it is measurable in a day.

**Risks.** 50 different schemas and ToS regimes — start with the 5 states covering
most commerce. Administrative dissolution is often clerical, not distress; the model
must separate *abandoned* from *sloppy*, or it will false-flag exactly like
`burst_sybil` did on high-volume payees.

---

### 5. Interconnection queues + air permits + PUC dockets → developer reliability & pre-announcement datacenter detection

**The data.** ISO/utility interconnection queues (GridTracker's interconnection.fyi
standardizes 50+ queues, free, daily). State air permit applications for backup
generators — a datacenter's diesel fleet requires an air permit **before** any press
release. PUC/PSC dockets: rate cases, IRPs, load forecasts, large-load tariffs.

**The label.** Queue withdrawal. Roughly **76.8% of CAISO queue requests have
withdrawn**; MISO publishes 56.2%. That is a rich, dated, per-project failure label —
and per-project rolls up to **per-developer**.

**Why this specific angle is open.** Enverus, Halcyon, RegulatorIndex and GridTracker
all sell *project* and *docket* visibility. **Nobody scores the developer.** Yet
"which developers actually energize what they queue, and how fast" is precisely the
question a lender, an EPC, an equipment vendor, or a utility planner needs — and it
is fully derivable from queue history that is already public. It is `seller_audit.py`
applied to interconnection: an EARNED grade from settled outcomes.

**The product.** (a) Developer reliability grades (queued GW → withdrawn / energized,
median time-in-queue, cost-allocation survival rate); (b) datacenter early-warning
from air-permit + large-load-tariff + queue triangulation, weeks-to-months ahead of
announcement; (c) grid-constraint heatmaps.

**Who pays.** EPCs and equipment vendors (transformers, switchgear, gensets — brutal
lead times, huge BD value in early signal), project finance lenders, hyperscaler and
industrial site selection, land brokers, utilities, and energy traders.

**Spike.** Pull CAISO + MISO queue history, group by interconnection customer name,
compute withdrawal rate per developer. Normalize the names (again, the moat). If the
spread between best and worst developer is wide, the grade means something.

**Risks.** Developer names are SPVs — "Solar Project 47 LLC" — so entity resolution to
the sponsor requires the SoS registry from #4. That is a feature: **#4 and #5 share
infrastructure.** ISO-NE publishes no status at all, so coverage is uneven; say so
rather than imputing, exactly as `dispute_rate = None` is reported as UNAVAILABLE
in `reputation_onchain.py` rather than fabricated as 0.

---

### 6. CMS ownership + enrollment + survey data → the healthcare ownership graph

**The data.** Since 2023 CMS publishes full ownership for **~6,000 hospices and
~11,000 home health agencies**, plus SNF ownership and "additional disclosable
parties," updated quarterly on data.cms.gov, *including* change-of-ownership history
back to 2016. Join to NPPES, PECOS, Care Compare survey deficiencies, Open Payments,
Part D prescriber files, and the OIG LEIE exclusion list.

**The label.** Survey deficiencies, immediate-jeopardy citations, terminations,
exclusions, ownership changes followed by quality collapse.

**Why it is underused *and* defensible.** GAO and Health Affairs both documented that
the CMS files are incomplete — one analysis found only **~1/3 of PE investments and
under 1/5 of REIT investments** identifiable from the public data. Most people read
that as "the data is bad." Read it correctly: **the gap is the product.** The value
is in the resolution layer that closes it by joining CMS ownership to SoS registries
(#4), UCC filings, and property records. Academics publish papers about the gap;
nobody sells the fixed graph.

**The product.** A chain/owner graph: which operator actually controls this facility,
what else they control, what happened to quality after each acquisition, and which
owners have exclusion or deficiency history.

**Who pays.** Payers and ACOs doing network integrity, PE and lender diligence,
health systems evaluating post-acute partners, medical device and pharmacy sales
teams (chain-level targeting is worth real money), staffing firms, plaintiff firms,
and journalists (that last one is free distribution, not revenue — but it is
distribution).

**Spike.** Rebuild the hospice ownership graph, then test: does an ownership change
predict a deficiency-rate change in the following 4 quarters? A yes is a headline
*and* a product.

**Risks.** Ownership data quality is the known weakness; every claim needs a
provenance citation. Stay clear of anything that touches individual consumers'
eligibility decisions — see the FCRA note below.

---

### 7. Construction payment risk from liens, permits, licensing and bond claims

**The data.** County/state mechanic's lien and preliminary notice filings, building
permits, contractor license status and **board disciplinary actions**, surety bond
claims, OSHA inspections, and (from #1) bankruptcy appearances.

**The label.** A recorded lien is a dated, attributable "this general contractor did
not pay this subcontractor." A license suspension is a regulator's verdict.

**Why the niche is open.** Levelset (now Procore) owns lien *workflow* and has
payment-speed data — but it is captive to its platform, sub-side, and sold as a
feature of compliance software. A **public-records-only GC payment-risk index**,
sellable to anyone including Levelset's non-customers, is unclaimed. Construction is
one of the largest sectors in the economy with among the worst counterparty
information.

**The product.** A GC/developer payment-risk grade: lien velocity per $ of permitted
work, days-to-release, repeat-sub concentration, license/OSHA/bond overlay. Plus
project-level early warning (liens on a project are the visible edge of a stalling
job).

**Who pays.** Subcontractors and material suppliers deciding whether to extend
credit, surety underwriters, specialty insurers, construction lenders, equipment
rental companies.

**Spike.** One large metro county with digital lien records + permits. Rank GCs by
liens per permitted dollar. If the tail is ugly and recognizable, it sells.

**Risks.** County-by-county acquisition is grinding, non-uniform, and sometimes
paywalled — the labor *is* the barrier to entry, which cuts both ways. Liens are
often filed as leverage in ordinary disputes; a raw count defames. Weight by
release-time and repeat-claimant breadth (a lien from one angry sub ≠ liens from
nine unrelated subs — literally the `established_payers` vs `captive_ratio`
distinction in `payer_graph.py`).

---

### 8. ClinicalTrials.gov version diffs → trial, site and sponsor execution reliability

**The data.** ClinicalTrials.gov holds >500,000 studies. The v2 API (rebuilt 2024) is
good — and **exposes only the current version**. Historical versions exist and are
retrievable per record (the `cthist` R package does exactly this, described as
labor-intensive at cohort scale). Nobody has assembled the full diff corpus.

**The label.** Terminated/withdrawn status **with the sponsor's stated reason**;
enrollment target revised downward; primary completion date slipped repeatedly;
endpoints changed after enrollment started; sites added or dropped.

**Why it is genuinely unowned.** The API gap *is* the moat (test #3, in its purest
form). Citeline/Informa sell trial intelligence off the current snapshot. The
**longitudinal diff** — the record of every time a sponsor quietly moved the goalposts
— is not a commercial product. And endpoint changes after enrollment start are a
well-known integrity concern that nobody monitors systematically at scale.

**The product.** (a) Site and investigator execution scores (does this site enroll on
time, across sponsors?) — the single most valuable unknown in trial operations;
(b) sponsor/CRO reliability grades; (c) a "silent trouble" feed for investors —
enrollment cut by 60% and completion pushed 18 months, six weeks before the 8-K;
(d) an endpoint-change integrity monitor for journals, regulators and journalists.

**Who pays.** CROs and site networks (site selection is the biggest cost driver in
trials), biotech hedge funds and long-onlys, pharma vendor management, clinical trial
insurers, IRBs and academic integrity groups.

**Spike.** Take 5,000 oncology Phase 2/3 trials from 2015–2023. Fetch all versions.
Test: does an early downward enrollment revision predict termination? If it does, you
have both a product and a paper.

**Risks.** Fetching version histories at scale is slow and must be rate-polite —
`http_util.py`'s backoff and read-cap logic is the right shape and is already
written. Site-level attribution to named investigators is sensitive; ship it
aggregate-first.

---

### 9. Federal contracting failure data → GovCon counterparty risk

**The data.** FPDS/USASpending contract actions including **terminations for default
and for cause**, SAM.gov exclusions/debarments, GAO and Court of Federal Claims
protest decisions, DCAA-adjacent public findings, IG reports, and SAM registration
lapses.

**The label.** Termination for default. Debarment. A sustained protest. Rare, severe,
dated, and public.

**Why it is open.** HigherGov, GovWin, and friends sell **opportunity** data — who is
buying what. The mirror image, **counterparty risk on contractors and subcontractors**,
is barely productized, even though the buyer is obvious: a prime that flow-downs to a
sub who then defaults eats the schedule slip.

**The product.** A contractor reliability grade (on-time delivery proxies from
modification history, termination history, protest posture, size-standard and
set-aside integrity, ownership churn) plus expiring-contract + incumbent-weakness
targeting for BD.

**Who pays.** Primes vetting subs, GovCon-focused lenders and factors, PE firms doing
GovCon M&A (this is an active roll-up market), bid/no-bid decision teams.

**Spike.** Pull all terminations for default/cause over 10 years, join to entity
UEI/CAGE, and check whether terminated contractors show precursor signals
(modification churn, protest losses, ownership change) in the preceding 24 months.

**Risks.** CPARS performance ratings are not public — the strongest signal is behind
the wall, so this must be built from proxies and stated as such. Termination coding
in FPDS is inconsistent across agencies.

---

### 10. Payer Transparency-in-Coverage MRFs → rate integrity and ghost-rate detection

**The data.** Every health plan and issuer must publish machine-readable files of
in-network negotiated rates and out-of-network allowed amounts. Aggregate scale is in
the **petabytes** — some individual payer index files are hundreds of GB.

**The label.** "Ghost" or "zombie" rates — negotiated rates published for
provider/service combinations that are **clinically implausible** (an anesthesiologist
with a published rate for an MRI read). Regulators have proposed requiring a taxonomy
file specifically so these can be identified and excluded, which is a formal
acknowledgment that the files as published are substantially fictional.

**Why the angle is open.** Turquoise Health, Serif Health and Payerset already sell
**normalized rates**. That race is run. The unclaimed product is **file integrity**:
what fraction of a payer's published network is real, how it changed month over
month, and which "in-network" providers show no plausible rates at all. That is the
data backbone of the ghost-network problem — currently litigated and legislated with
almost no quantitative evidence base.

**The product.** A per-payer, per-market network-integrity index; a ghost-rate
detector (rate × provider taxonomy × plausibility); an employer-facing "what your TPA
actually negotiated versus the market" report.

**Who pays.** Self-funded employers and their advisors (this is a live fiduciary
obligation post-CAA), TPAs differentiating on honesty, provider contracting teams,
state regulators and AGs, plaintiff firms, digital-health companies pricing cash-pay.

**Spike.** Take 3 payers in 1 metro. Join published rates to NPPES taxonomy. Measure
the implausible-combination share. If it is a large number — and every indication is
that it is — that single statistic is a press release, a regulator meeting, and a
sales deck.

**Risks.** The scale is real: this is the only one of the ten with a meaningful
infrastructure bill, and it is the reason the niche is still open. Scope to a few
payers and metros; do not attempt national coverage before someone has paid.

---

## Ranked shortlist

| # | Opportunity | Data effort | Entity-resolution moat | Buyer urgency | Incumbency | Reuses this repo |
|---|-------------|-------------|------------------------|---------------|------------|------------------|
| 1 | Ch.11 creditor graph | Medium | **High** | High | **None on the graph** | `payer_graph`, `merge_records` |
| 2 | FMCSA chameleon graph | **Low** | **High** | **Very high** | Lookup tools only | `payer_reputation`, `sybil_ring` |
| 3 | FDA supplier quality | **Low** | Medium | High | Consultants only | `reputation_store`, `confidence` |
| 4 | SoS registry churn | High (50 states) | **High** | High | KYB ≠ time series | `sybil_ring`, `settlement_velocity` |
| 5 | Grid developer grades | Medium | Medium | High | Project data ≠ dev scores | `seller_audit`, `issuer_trust` |
| 6 | CMS ownership graph | Medium | **High** | Medium | Academic only | `payer_graph` |
| 7 | Construction payment risk | **Very high** | Medium | High | Levelset (captive) | `ledger`, `going_bad` |
| 8 | Trial version diffs | Medium | **High** | Medium | **None** | `http_util`, `confidence` |
| 9 | GovCon counterparty risk | Low | Medium | Medium | Opportunity data only | `seller_audit` |
| 10 | TiC network integrity | **Very high** | Low | High | Rates race is run | — |

**If forced to pick two:** **#2 (FMCSA)** and **#1 (Chapter 11)**.

- #2 is the fastest possible proof: free bulk files, no scraping, a fraud label that
  is unambiguous, a buyer (freight factoring, cargo insurance) actively hemorrhaging
  money, and an algorithm this codebase has already written twice. A working
  chameleon-detection prototype is a **weekend**, not a quarter.
- #1 is the biggest asset. A twenty-year B2B trade-credit graph is a structural
  monopoly on a graph nobody else has bothered to build, and it feeds #4, #7 and #9
  as a shared entity spine.

Note that #1, #4, #5, #6, #7 and #9 **all** need the same underlying capability:
US business entity resolution across registries, addresses, officers and DBAs. Build
that once and it is the substrate under six of the ten.

---

## Monetization patterns that fit

1. **Score-as-a-gate (the Blackwall shape).** A synchronous API returning
   GO/REVIEW/BLOCK with cited evidence, priced per call, embedded at the moment of
   decision (booking a carrier, extending terms, approving a PO). Highest value,
   because it sits where the money moves. Everything in this repo about fail-open,
   HOLD-only, and never-fabricate-a-zero applies directly.
2. **Monitoring subscription.** Cheap to run, sticky: "tell me when anything changes
   about these 400 counterparties." Usually the easiest first sale.
3. **Bulk data licensing.** Insurers and funds will buy the normalized corpus outright.
4. **BD funnel / lead gen.** `ecosystem_scan.audit_candidates()` in this repo already
   proves the pattern: the same crawl that scores also *ranks who to sell to*. In #3,
   #5 and #6 the lead list is arguably worth more than the score.
5. **Agent-native distribution.** Every one of these is a natural MCP tool and an
   x402-priced endpoint. `mcp_server.py` and `x402.py` are already written; a new
   domain is a new `lookup()` seam, not a new product surface.

---

## Legal and ethical guardrails — read before building any of these

- **FCRA is the bright line.** Any score used to make decisions about an
  *individual's* credit, employment, insurance or housing eligibility makes you a
  consumer reporting agency, with dispute-resolution, accuracy and permissible-purpose
  obligations. **Keep every one of these B2B and entity-level.** #4 (sole proprietors),
  #6 (named clinicians) and #7 (individual licensees) are the ones that can drift
  across the line without anyone noticing.
- **Defamation.** A published "this company is a fraud" score is actionable if wrong.
  The repo's existing discipline is the correct answer: HOLD-only where evidence is
  thin, every claim carries its citation and provenance, no unexplained STOPs, and a
  documented dispute path. `confidence.py`'s `backed_by[]` / `missing[]` shape is the
  right output contract.
- **Terms of service and robots.txt.** Bulk government files are generally clean.
  Claims-agent portals, county recorders and state registries vary — check each,
  keep per-source provenance, rate-limit politely (`http_util.py` already does).
- **Survivorship bias (test #5) is the technical failure mode that will embarrass
  you.** This repo learned it the hard way: backfill saw only successes, `settle_rate`
  was always 1.0, and `revert_scan.py` had to be written to find the missing
  denominator. Every dataset here has the same trap. Find the failures before you
  publish a rate.
- **Graduate gates the way this repo does.** Ship new signals descriptive-only behind
  a reversibility lock (`SYBIL_RING_GATES`, `ISSUER_TRUST_GATES`, `REVERT_AXIS_GATES`),
  measure the false-flag rate on known-good entities, and promote to gating only when
  it stabilizes near zero. `REVERT_AXIS_GATES` stayed off because its first act was to
  downgrade BlackRock for working as designed. That instinct is the product.

---

## Recommended next step

A two-week spike on **#2 (FMCSA)**, because it is the only one where the full loop —
bulk download → entity graph → labeled outcome → measurable detection rate — is
reachable without any scraping infrastructure at all:

1. Download the census, OOS, inspection and crash files.
2. Build identity keys (normalized address, phone, officer name, VIN).
3. Find clusters: revoked DOT number ↔ new DOT number sharing ≥2 keys within 90 days.
4. Measure: what share of those new entities subsequently receive OOS orders or
   crashes, versus the base rate for genuinely new entrants? Published FMCSA analysis
   puts reincarnated carriers near **3×**. Reproduce that number independently.
5. If it reproduces, that single measured multiple is the entire go-to-market — take
   it to three freight factoring companies and ask what a 3× fraud-rate discriminator
   is worth per lookup.

Nothing in step 1–4 requires a customer. That is the whole point.

---

## Sources

- Interconnection queue attrition and public queue data — [interconnection.fyi (GridTracker)](https://www.interconnection.fyi/), [CAISO queue statistics](https://ustechautomations.com/resources/blog/caiso-interconnection-queue-report), [grid interconnection statistics](https://axis-intelligence.com/grid-interconnection-queue-statistics/)
- Chameleon carriers and FMCSA detection — [Trucksafe on FMCSA data strategy](https://trucksafe.com/post/chameleon-carriers-fraud-detection-and-fmcsa-s-evolving-data-strategy), [CNS on MOTUS enforcement](https://www.cnsprotects.com/news/fmcsa-anti-chemeleon-carrier-strategy/), [FMCSA Open Data Program](https://www.fmcsa.dot.gov/registration/fmcsa-data-dissemination-program)
- Municipal continuing disclosure — [MSRB Continuing Disclosures](https://www.msrb.org/Continuing-Disclosures)
- ClinicalTrials.gov history access — [API v2 announcement](https://www.nlm.nih.gov/pubs/techbull/ma24/ma24_clinicaltrials_api.html), [cthist registry-history package](https://pmc.ncbi.nlm.nih.gov/articles/PMC9249399/)
- SERFF rate filings — [SERFF Filing Access](https://serff.com/serff_filing_access.htm)
- Data center air permitting — [Williams Mullen on generator air permits](https://www.williamsmullen.com/insights/news/legal-news/frequently-asked-air-questions-faaqs-understanding-data-center-emergency), [BuildCentral on spotting builds early](https://www.buildcentral.com/data-center-permits-in-2026-how-to-spot-new-builds-expansions-and-power-upgrades-early/)
- Pre-RFP procurement incumbents — [Civic IQ](https://civiciq.com/blog/pre-rfp-signal-monitoring-guide), [GovTech on procurement](https://www.govtech.com/budget-finance/the-new-rules-of-procurement-what-it-means-to-buy-tech-in-2026)
- PUC docket intelligence incumbents — [RegulatorIndex](https://regulatorindex.com/), [Halcyon Rate Case Tracker](https://halcyon.io/rate-case-tracker)
- Chapter 11 creditor lists — [Official Form 204](https://www.uscourts.gov/forms-rules/forms/chapter-11-cases-list-creditors-who-have-20-largest-unsecured-claims-against-you-who-are-not), [Kroll Restructuring Administration](https://www.kroll.com/en/services/restructuring-administration)
- FDA import refusals and dashboards — [FDA Data Dashboard API](https://datadashboard.fda.gov/oii/api/index.htm), [openFDA downloads](https://open.fda.gov/apis/downloads/)
- CMS ownership data and its gaps — [CMS hospice/HHA ownership release](https://www.mcguirewoods.com/client-resources/alerts/2023/5/cms-publicly-releases-ownership-data-medicare-certified-hospice-home-health-agencies/), [GAO-23-106163 on PE identification limits](https://www.gao.gov/products/gao-23-106163), [Health Affairs on ownership data gaps](https://www.healthaffairs.org/doi/10.1377/hlthaff.2023.01110)
- Construction lien data incumbency — [Levelset](https://www.levelset.com/)
- Administrative dissolution mechanics — [Wolters Kluwer](https://www.wolterskluwer.com/en/expert-insights/the-administrative-dissolution-and-reinstatement-of-business-entities)
- Transparency in Coverage ghost rates — [Groom Law Group on proposed TiC amendments](https://www.groom.com/resources/the-ghost-in-the-machine-readable-files-proposed-transparency-in-coverage-amendments-attempt-to-shed-additional-light-on-health-plan-data/)
- 340B public reporting — [340B OPAIS reports](https://340bopais.hrsa.gov/reports)
