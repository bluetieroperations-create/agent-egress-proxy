# Ten more — set B, filtered through Test 6 from the start

**Date:** 2026-08-25 · **Companion to:** `docs/COLD_START_DATA.md` (set A),
`docs/REMOTE_FIRST.md` (the constraint)

Set A was picked before the remote-only constraint existed, which is why three of its
ten fail Test 6. **This set is picked under it.** Every entry below has a buyer who
already transacts from a desk — an investment fund with a data-sourcing team, a vendor
who buys leads on a card, or a marketplace listing with no human in the loop.

Same five original tests still apply (free/bulk, already-labeled, hard entity
resolution, a buyer losing money, closable survivorship bias), plus:

> **Test 6 — it closes without a meeting.**

Verified reachable this session: every source named has a public download, API, or
search endpoint, and I checked each for existing owners.

---

## 1. EMMA obligated-person financials → the private financials that are secretly public

**The data.** Under SEC Rule 15c2-12, any entity that issues municipal debt must file
annual audited financials to EMMA — free, public, permanent. That includes **nonprofit
hospitals and health systems, universities, senior-living operators, airports, toll
roads, and utilities**. EMMA is the official source for the annual financials of any
hospital financed by public debt.

**The signal.** These are audited financial statements for enormous institutions that
are otherwise completely opaque. Nonprofit hospital systems with billions in revenue
file full financials that no equity screen has ever touched. Plus the distress label
from set A: a *missed* filing is itself a signal.

**Why it is unowned.** Everyone treats EMMA as a bond-market utility. Health-care
journalists have been formally trained on it as a hospital-financials source, which
tells you it is (a) genuinely valuable and (b) still being accessed by hand, one PDF at
a time. Nobody has parsed the corpus into a structured panel.

**The product.** A quarterly/annual financial panel for ~2,000 nonprofit health systems
and universities: revenue, margin, days cash on hand, payer mix, volume statistics.

**Who pays, remotely.** Muni credit funds (obvious). Far more interesting: **anyone who
sells into hospitals** — medtech, staffing, revenue-cycle, EHR, GPO — who currently
cannot tell which of their prospects is flush and which is failing. That is a
sales-intelligence product bought on a card by a VP of Sales, not a procurement cycle.
Also healthcare equity funds sizing the nonprofit competitive set.

**Desk spike.** Pull 200 hospital annual filings. Test whether the financial statements
are parseable at scale (they are PDFs — this is the real work, and the moat).

**Risk.** PDF extraction quality is the whole game. Filing formats vary by obligor.
Timeliness is poor — annual, often 6+ months lagged, so it is a fundamental panel, not
a trading signal.

---

## 2. NADAC weekly + state Medicaid formularies → generic drug pricing and formulary wins

**The data.** CMS publishes **NADAC** (National Average Drug Acquisition Cost) **weekly**,
with an explicit week-to-week comparison file naming every drug whose price changed.
Monthly files too. State Medicaid preferred drug lists and drug-pricing datasets are
also public and exportable by NDC.

**The label.** An actual, surveyed acquisition cost per NDC, changing weekly. This is
the ground truth of generic drug price deflation — the single most important variable
in generic pharma economics — published free, every Wednesday, in a comparison file
that literally hands you the deltas.

**Why it is unowned.** The pharma pricing analytics market sells to manufacturers at
enterprise prices. Nobody has turned the weekly delta file into a clean time series
product for investors, and the state PDL layer (which drug won formulary position in
which state, and when) is scattered across 50 state sites.

**The product.** A generic-price deflation index by molecule and by manufacturer;
formulary win/loss tracking by state; shortage-to-price-spike detection.

**Who pays, remotely.** Healthcare and pharma funds (Teva, Viatris, Amneal, Hikma,
Perrigo all live and die on generic deflation). Generic manufacturers themselves for
competitive pricing. Specialty pharmacies and PBM watchdogs.

**Desk spike.** Download 3 years of weekly NADAC comparison files, build a deflation
index by therapeutic class, and check whether it leads reported gross margin at the
listed generics.

**Risk.** NADAC methodology changed in Dec 2024 (three-month moving average for
generics) — your history has a seam and you must document it, or a quant will find it
and drop you.

---

## 3. FDA Paragraph IV + Orange Book + drug shortages → the patent-cliff clock

**The data.** FDA publishes the **Paragraph IV certification list** — every drug for
which a generic sponsor has filed an ANDA challenging the brand's patents, and the date
of first filing. It exists specifically so firms can tell who filed first. Join to the
**Orange Book** (patents and exclusivities per drug) and the **drug shortage database**.

**The label.** A Paragraph IV filing is a dated, public declaration that a generic
company believes it can break a patent. First-filer status carries 180-day exclusivity
— enormous money. Shortages are dated supply failures.

**Why it is unowned.** Law firms track this qualitatively, drug-by-drug, for clients.
There is academic work analysing filings retrospectively. There is no structured
**patent-cliff timing product** joining filing dates, Orange Book expiries, litigation
dockets and shortage events into one forecastable calendar.

**The product.** A per-molecule erosion clock: expected generic entry date, number of
filers, litigation status, and the revenue at risk. Plus shortage-driven price-spike
alerts.

**Who pays, remotely.** Pharma funds (patent cliffs are the dominant driver of large-cap
pharma valuation), generic manufacturers, specialty pharmacy, and drug-pricing
researchers.

**Desk spike.** Build the join for the top 100 branded drugs by US revenue. If the
resulting erosion calendar disagrees with sell-side consensus anywhere, that gap is the
sales pitch.

**Risk.** Litigation outcomes are the uncertain part and they are what actually decides
entry timing. Be honest that you are forecasting a distribution, not a date.

---

## 4. EPA e-Manifest → a per-facility industrial heartbeat

**The data.** Since 2018 every hazardous waste shipment in the US is tracked
electronically cradle-to-grave. **Manifest data becomes public 90 days after receipt at
the designated facility**, searchable through RCRAInfo: which sites ship waste, what
types, how much, and where it goes.

**The label.** Waste out is a direct, physical proxy for **production volume at a named
facility**. A factory that ships less solvent is running less. This is one of very few
free, facility-level activity measures in existence — the kind of thing satellite
companies charge enormous sums to approximate.

**Why it is unowned.** The dataset is young (2018), lives in a compliance portal nobody
thinks of as market data, and the 90-day lag makes it useless for high-frequency
trading — which is exactly why it is still available to you. It is a *fundamental*
signal, not a tick.

**The product.** A facility-level production index rolled up to parent company and
sector: semiconductors, chemicals, pharma manufacturing, auto plants, refineries.

**Who pays, remotely.** Industrials and chemicals funds, ESG/transition analysts,
supply-chain risk teams, and competitors doing capacity intelligence.

**Desk spike.** Pull manifests for 20 known semiconductor fabs. Do shipment volumes
track published utilization? If yes, you have a factory activity index nobody else has.

**Risk.** The 90-day lag caps the use cases — say so up front. Coverage skews to
industries that generate regulated waste; a data centre generates almost none.

---

## 5. MSHA quarterly production and employment → mine-by-mine, since 2000

**The data.** MSHA publishes **quarterly employment and production per mine**, by mine
ID and subunit, back to 1 January 2000, plus a matching contractor dataset. Free bulk
downloads on MSHA and data.gov. Also inspections, violations, accidents, air sampling.

**The label.** Actual reported production and headcount, per physical site, quarterly,
for 25 years. That is a *complete point-in-time panel* handed to you — the exact asset
that alt-data buyers say almost no vendor can prove.

**Why it is unowned.** MSHA is thought of as a safety regulator. Coal analysts use it;
almost nobody uses it for **aggregates, cement, sand, lithium, and industrial
minerals**, which is where the money now is — data-centre and infrastructure
construction runs on aggregates.

**The product.** A site-level production panel for the aggregates and industrial
minerals complex, rolled up to operator and to public parent. Plus M&A detection —
operator changes at a mine ID show up before announcements.

**Who pays, remotely.** Materials funds (Vulcan, Martin Marietta, Summit, Eagle
Materials, Knife River), commodity desks, lithium and critical-minerals investors, and
the operators themselves for competitive intelligence.

**Desk spike.** Build the operator→ticker map and backtest quarterly production against
reported volumes for the listed aggregates names. If it tracks, you have a
pre-announcement volume estimate on a 25-year history.

**Risk.** Reporting is by mine operator and self-reported; entity resolution from mine
ID to public parent is the work (and the moat). Publication lags one to two months
after quarter end — still ahead of earnings.

---

## 6. State DOT bid lettings → the forward order book for construction materials

**The data.** Every state DOT publishes its letting schedule, bid tabs, and awards.
TxDOT holds statewide lettings monthly with public notice, bid opening, apparent low
bidder and executed contracts; MnDOT publishes a 12-month tentative letting schedule.
Fifty states, all public, all free.

**The label.** An awarded contract is a signed, dated, dollar-valued commitment to build
something specific — with quantities. Bid tabs contain **unit prices and material
quantities**, which means you can read cement and aggregate demand directly off them.

**Why it is unowned.** Construction data vendors sell *project leads to contractors*.
Nobody has built the aggregate view: total lettings by state by quarter as a forward
indicator of materials volumes and pricing. The 12-month tentative schedules make it
genuinely *forward-looking*, which is rare.

**The product.** A national lettings index — dollars awarded, material quantities,
bid-to-estimate spreads (a direct read on contractor pricing power) — plus contractor
win-rate scoring as a second product.

**Who pays, remotely.** Materials and E&C funds, contractors doing competitive bid
analysis, equipment makers, and state-level economic analysts.

**Desk spike.** Five large states, 3 years of awards. Does aggregate letting volume lead
reported materials volumes? Also check bid-to-engineer's-estimate spread as an
inflation gauge — that number is quietly excellent and nobody publishes it.

**Risk.** Fifty formats, several of them awful. Start with the five largest states;
that is most of the dollar volume.

---

## 7. FCC equipment authorization → unannounced hardware, on a clock

**The data.** Every intentional radiator sold in the US must be certified before it can
ship, and the grant appears in the FCC's public **equipment authorization** database
with applicant, dates, and technical exhibits.

**The label.** A certification grant is a hard, dated commitment that a physical product
is real and near launch. Better: applicants routinely request **short-term
confidentiality** on photos and manuals — and those requests *expire on a schedule*.
The expiry date is itself a predictable disclosure event you can queue.

**Why it is only partly owned.** Free viewers exist (fccid.io, fcc.report) and tech
blogs watch them. What does not exist is the **structured historical corpus with
applicant→parent→ticker mapping and a confidentiality-expiry calendar**. The hobbyist
layer is served; the analytic layer is not.

**The product.** A hardware-launch pipeline by company: filings per quarter, device
class, time-from-grant-to-launch, and an alert calendar for confidentiality expiries.

**Who pays, remotely.** Consumer-tech and semiconductor funds, component suppliers
(a grant reveals the radio and often the chipset), and competitive-intelligence teams.

**Desk spike.** Map 3 years of grants for 20 known consumer-hardware makers and measure
the median grant→launch lag. That lag *is* the forecast.

**Risk.** Devices authorized under SDoC do not appear in the database at all, so
coverage is structurally incomplete — state it plainly. Applicants often file under
obscure ODM names; the entity mapping is the hard part and the moat.

---

## 8. NHTSA complaints + Early Warning Reporting → defects before the recall

**The data.** NHTSA's complaint database runs **from 1949 to present, updated daily**,
free on data.gov. Alongside it, the TREAD Act **Early Warning Reporting** system
collects manufacturer submissions on deaths, injuries, property damage and production,
with non-confidential portions publicly searchable.

**The label.** Recalls. Every recall is a dated, attributable failure, and the complaint
stream preceding it is the leading indicator — NHTSA opens investigations precisely
because complaint clusters form.

**Why it is unowned.** Everyone uses the recall list (lagging). Almost nobody models the
**complaint velocity that precedes it**, per make/model/year/component, at scale.

**The product.** A defect-emergence score per vehicle platform and per supplier
component, with expected recall probability and estimated cost exposure.

**Who pays, remotely.** Auto OEM and supplier funds (recall costs move supplier
earnings hard), warranty and extended-warranty underwriters, insurers, fleet buyers,
and plaintiff firms.

**Desk spike.** For 50 known recalls, measure complaint velocity in the 6 months prior
versus the platform base rate. If a threshold separates them, you have the product.

**Risk.** Complaint volume correlates with fleet size and media attention — you must
normalize per vehicle in operation or you will simply rediscover which cars are
popular. EWR public portions are partial; confidential fields are not available.

---

## 9. NIH RePORTER → the funding flow under the life-science economy

**The data.** Every NIH grant — institution, PI, amount, dates, topic, renewals,
terminations — free with an API. Plus the same for NSF and other federal science
agencies.

**The label.** Awards, renewals, and **non-renewals or terminations**. Money in and
money cut, per institution, per field, dated.

**Why it is unowned.** RePORTER is a research-admin tool. Bibliometrics firms mine
publications; nobody sells the **downstream demand signal** — NIH funding leads
lab consumable and instrument purchasing by a predictable lag, which makes it a
forward indicator for life-science tools revenue.

**The product.** A funding-flow panel by institution, field, and instrument-relevant
category; an early-warning feed on funding cuts by institution; and a
research-front tracker that flags which biology is being funded before it becomes a
biotech thesis.

**Who pays, remotely.** Life-science tools funds (Thermo, Danaher, Illumina, Revvity,
Bruker all sell into NIH-funded labs), biotech VCs sourcing academic founders, and
**the tools vendors themselves** for territory targeting — a sales-intelligence product
sold to a VP of Sales, no procurement cycle.

**Desk spike.** Build the institution-level funding time series and test its lead-lag
against reported academic-segment revenue at one listed tools company.

**Risk.** Federal science funding is politically volatile right now, which cuts both
ways — it makes the signal more valuable and your history less stationary.

---

## 10. Liquor licenses + health permits + certificates of occupancy → stores before they open

**The data.** State ABC liquor license applications, county health-department permits
and inspections, and municipal certificates of occupancy and sign permits. All public,
all local, all free.

**The label.** A new liquor license or food permit at a new address is a store that is
about to open. A lapsed one is a store that closed. Both dated, both physical, both
months ahead of any corporate announcement.

**Why it is unowned.** Restaurant-industry data vendors sell *location databases*
scraped from store locators — which update only *after* a store opens. The permit layer
is the leading indicator and it is scattered across thousands of jurisdictions, which is
precisely why nobody has assembled it.

**The product.** A unit-count nowcast for restaurant and retail chains — openings and
closings by brand and market, weeks-to-months before the 10-Q. Plus a franchisee
churn signal (permits changing hands at the same address).

**Who pays, remotely.** Consumer and restaurant equity funds (unit growth is *the*
valuation driver for restaurant chains), CRE investors and site selectors, franchise
brokers, and suppliers who want to reach a new location before it opens — that last one
is a lead-gen product sold on a card.

**Desk spike.** One state's ABC application feed plus one large county's health permits.
Match to a chain with published unit counts. If your permit-derived count leads the
reported figure, that is the whole demo.

**Risk.** The most jurisdictionally fragmented item in this set — genuinely the hardest
acquisition problem here. Offset: start with the 10 metros that carry a
disproportionate share of national unit openings, and be explicit that coverage is
partial rather than implying a census.

---

## Ranked, with what each is actually worth

| # | Play | Data effort | Buyer transacts remotely | Point-in-time history available | Best buyer |
|---|---|---|---|---|---|
| 5 | **MSHA production panel** | **Low** | Yes | **25 years, complete** | Materials funds |
| 2 | **NADAC deflation index** | **Low** | Yes | Weekly, deep | Pharma funds |
| 3 | **Paragraph IV cliff clock** | Low | Yes | Deep | Pharma funds |
| 8 | **NHTSA defect emergence** | **Low** | Yes | **Since 1949, daily** | Auto/supplier funds, warranty |
| 9 | **NIH funding flow** | **Low** | Yes | Deep, API | Tools funds **+ tools vendors** |
| 6 | **DOT lettings index** | Medium | Yes | Medium, forward-looking | Materials funds, contractors |
| 4 | **e-Manifest activity index** | Medium | Yes | Since 2018, 90-day lag | Industrials funds |
| 7 | **FCC hardware pipeline** | Medium | Yes | Deep | Consumer-tech funds |
| 1 | **EMMA institutional financials** | High (PDF) | Yes | Deep, slow | Muni funds **+ hospital vendors** |
| 10 | **Permit-derived unit counts** | **Very high** | Yes | Build-forward | Restaurant/retail funds |

### The three I would shortlist against the ClinicalTrials.gov pick

**#5 MSHA** — because it is the only other entry that hands you a **complete
point-in-time panel on day one**. That was the entire reason ClinicalTrials.gov won the
remote-first re-rank: funds will not evaluate a dataset whose history cannot be proven
point-in-time, and both of these arrive with theirs intact. MSHA is also the lowest
effort item in either set — quarterly CSVs, no scraping.

**#2 NADAC** — weekly cadence, a comparison file that literally publishes the deltas,
and a buyer universe (pharma funds) that is large, remote, and already spending.

**#9 NIH RePORTER** — the only entry with **two independent remote buyers**: funds *and*
the tools vendors' own sales teams. Two shots on goal from one build.

### Honourable mentions, cut for space

SERFF rate filings → P&C insurer pricing ahead of earnings (InsuranceFiling exists but
not as an investor product); FCC Broadband Data Collection → fiber buildout tracking;
H-1B/LCA disclosure data → headcount and salary bands at **private** pre-IPO companies;
USPTO trademark filings → brand launch detection; state gaming and sports-betting
handle → gaming equities; USDA RMA crop insurance indemnities → ag commodity stress.

---

## The pattern across both sets

Set B is more monetizable than set A under the remote constraint for one structural
reason: **almost every entry maps to a liquid, tradeable security**. That gives you a
buyer with a data-sourcing team, an $80k-ish annual price point, and a purchase that
completes over email — while set A's best ideas mostly pointed at enterprise buyers who
expect a relationship first.

The two tests that now decide everything:

1. **Does it map to a ticker?** If yes, your buyer is remote and well-funded.
2. **Can you prove point-in-time history on day one?** If yes, you clear the gate that
   kills most vendors. If the history has to be built forward from today, you are 18
   months from your first serious trial.

**#5 MSHA and #2 NADAC pass both, on free CSV downloads, with no scraping.** They are
the cheapest tests of this entire thesis available.

## Sources

- **EMMA / institutional financials** — [MSRB continuing disclosure](https://www.msrb.org/Continuing-Disclosures), [EMMA Dataport](https://dataport.emma.msrb.org/Home), [AHCJ on EMMA as a hospital-financials source](https://healthjournalism.org/event/emma-get-to-know-this-source-for-hospital-financial-reports/)
- **Drug pricing** — [CMS NADAC](https://www.medicaid.gov/medicaid/nadac), [Medicaid pharmacy pricing files](https://www.medicaid.gov/medicaid/prescription-drugs/pharmacy-pricing), [NADAC 2026 on data.gov](https://catalog.data.gov/dataset/nadac-national-average-drug-acquisition-cost-2026)
- **Patent cliff** — [FDA patent certifications and Paragraph IV list](https://www.fda.gov/drugs/abbreviated-new-drug-application-anda/patent-certifications-and-suitability-petitions), [Wilson Sonsini on the Paragraph IV list](https://www.wsgr.com/en/insights/fda-updates-paragraph-iv-certification-list-to-help-spur-investment-in-generic-drug-development.html), [GAO-23-105477 on FDA patent information](https://www.gao.gov/assets/gao-23-105477.pdf)
- **e-Manifest** — [EPA: learn about e-Manifest](https://www.epa.gov/e-manifest/learn-about-hazardous-waste-electronic-manifest-system-e-manifest), [RCRAInfo public search](https://rcrainfo.epa.gov/rcrainfoweb/action/main-menu/view)
- **MSHA** — [Employment/Production quarterly dataset](https://www.msha.gov/data-and-reports/data-sources-and-calculators/data-resources/mdsrg/employment-production-quarterly-data-set), [MSHA data and reports](https://www.msha.gov/data-and-reports), [data.gov mirror](https://catalog.data.gov/dataset/msha-operator-employment-and-production-data-set-quarterly)
- **DOT lettings** — [TxDOT contract letting](https://www.txdot.gov/business/road-bridge-maintenance/contract-letting.html), [MnDOT bid letting](https://www.dot.state.mn.us/bidlet/), [FHWA on bid analysis and award](https://www.fhwa.dot.gov/construction/cqit/award.cfm)
- **FCC equipment authorization** — [FCC ID search](https://www.fcc.gov/oet/ea/fccid), [OET equipment authorization search](https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm), [equipment authorization procedures](https://www.fcc.gov/general/equipment-authorization-procedures)
- **NHTSA** — [Early Warning Reporting](https://www.nhtsa.gov/vehicle-manufacturers/early-warning-reporting), [NHTSA datasets and APIs](https://www.nhtsa.gov/nhtsa-datasets-and-apis), [ODI EWR on data.gov](https://catalog.data.gov/dataset/nhtsas-office-of-defects-investigation-odi-early-warning-reporting-ewr)
