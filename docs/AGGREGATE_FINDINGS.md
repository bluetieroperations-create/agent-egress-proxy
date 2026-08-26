# Testing the aggregate product: it fails too, for different reasons

**Date:** 2026-08-25 · **Follows:** `docs/WEEK_TWO_FINDINGS.md`
**Verdict: FAIL as a fast-dollar product. The data is fine. The value is already free,
the timeliness edge is weak, and the urgency it was built on has evaporated.**

Week two's lesson was *verify meaning before computing*. Applied here, that meant
asking who already sells this **before** writing any analysis.

## 1. The aggregate is already free, from two authoritative sources

- **BRIMR** (Blue Ridge Institute for Medical Research) publishes free annual rankings
  of NIH funding by **institution, department, and individual investigator**, going back
  to **2006**. Universities issue press releases about their Blue Ridge rank.
- **NIH RePORT "Awards by Location and Organization"** — the data owner's own tool.
  Year-by-year funding by organization, state and congressional district, with
  downloadable aggregates and visualisations. Free.
- Free third-party explorers exist on top of the same API.

"Institution-level NIH funding levels" is not an unowned niche. It is a solved,
well-known, freely-published dataset with a twenty-year incumbent.

That leaves exactly one possible wedge: **timeliness**. BRIMR is annual and
fiscal-year-complete. Could a weekly feed beat it?

## 2. The timeliness edge is weak

Using `date_added` to reconstruct what was knowable at month *M* of each fiscal year,
then asking whether the partial year correctly calls the **direction** of the full-year
change, across the top Michigan institutions for FY2022–FY2025:

| month of fiscal year | direction calls | accuracy |
|---|---|---|
| 3 | 20 | **55.0%** |
| 4 | 26 | 80.8% |
| 5 | 27 | 66.7% |
| 6 | 29 | 65.5% |
| 9 | 29 | 79.3% |
| 12 | 30 | **90.0%** |

Month 3 is a coin flip. The signal does not stabilise until late in the year — by which
point BRIMR is nearly out anyway. Sample sizes are small (20–30 calls), so treat these
as directional, but nothing here supports a claim of meaningful lead time.

**BRIMR waits a year because waiting is the correct methodology.** Speed here buys noise.

## 3. The finding that actually matters: the naive comparison is catastrophically wrong

Comparing a partial FY2026 against a complete FY2025 — the obvious calculation, and the
one anyone building this would reach for first — produces a fake collapse.

Same-point comparison via `date_added` (FY2026 as known on 25 Aug 2026 vs FY2025 as
known on 25 Aug 2025):

| institution | FY25 @ Aug | FY26 @ Aug | **like-for-like** | naive |
|---|---|---|---|---|
| University of Michigan | $574.8M | $615.8M | **+7.1%** | −24.9% |
| Wayne State | $66.2M | $60.7M | −8.3% | −33.2% |
| Michigan State | $64.0M | $54.9M | −14.2% | −31.6% |
| Van Andel Research Institute | $21.9M | $20.6M | −5.9% | −23.8% |
| **Michigan total** | **$797.0M** | **$829.3M** | **+4.1%** | **−26.6%** |

**The naive read overstates the decline by 22.6 percentage points of statewide funding.**

## 4. And the contraction narrative is stale

Like-for-like same-point year-over-year, two independent states:

| state | awards | FY22 | FY23 | FY24 | **FY25** | **FY26** | naive FY26 |
|---|---|---|---|---|---|---|---|
| Massachusetts | 52,333 | −3.2% | +8.3% | −2.0% | **−11.6%** | **+6.1%** | −27.5% |
| Michigan | 17,900 | +9.1% | +4.7% | +6.6% | **−10.3%** | **+4.1%** | −26.6% |

**FY2025 was the contraction. FY2026 has recovered.** Massachusetts is the largest
biomedical-funding state in the country and Michigan is a solid mid-size one; they agree
closely on both the fall and the rebound.

The entire "which of my accounts just lost funding" wedge — the reframe that made the
whole plan work — was built on a real event that **has already reversed**. Building it
now would be building for a world that ended nine months ago.

## Verdict

The aggregate product fails as a fast-dollar play on three independent counts:

1. **The value is already free** from BRIMR and NIH itself, with twenty years of history.
2. **The timeliness wedge is weak** — a coin flip early, and only reliable when it is no
   longer early.
3. **The urgency evaporated.** The contraction that justified urgency is a year stale.

None of this is a data problem. The pipeline is correct. The product is not there.

## What survives, honestly

**One publishable asset.** The like-for-like correction is a real methodological finding.
Right now, anyone comparing partial-FY2026 to complete-FY2025 — journalists, university
communications offices, advocacy groups — will publish a −27% collapse that is off by
23 points. Correcting that publicly is a genuine credibility asset and a strong lead
magnet. **It is not a subscription business.** It is a piece of writing.

**The Phase 3 fund thesis, untouched.** BRIMR does not preserve point-in-time vintages,
does not map institutions to tickers, and does not sell to investors. The question *does
institutional NIH funding lead life-science tools revenue?* remains open, valuable, and
unowned — and week one showed its backtest can start now rather than in nine months.

## The conclusion I would draw

**NIH RePORTER has now failed twice at the fast-dollar layer** — once at lab level
(week two), once at institution level (here). That is not bad luck. It is the dataset
telling you what it is:

> **RePORTER is a ceiling dataset, not a fast-dollar dataset.**

Its buyers are patient, analytical, and institutional. Its fast, urgent, transactional
buyers either do not exist or are served free.

The requirements ledger (`docs/REQUIREMENTS.md`, E1) says fast dollar comes first. If
that still holds, the honest move is to **take the fast dollar from a different
dataset** — the set-B shortlist put **MSHA** (quarterly per-mine production, complete
25-year point-in-time panel, materials-fund buyers) and **NADAC** (weekly drug-price
deltas published as a comparison file) ahead of everything else on exactly the criteria
that matter here.

Keep NIH RePORTER for Phase 3. Do not keep trying to make it pay in month two.

## Reproducing

```sh
cd labclosure
python3 fetch_reporter.py MI && python3 fetch_reporter.py MA
python3 aggregate.py      # timeliness test
python3 likeforlike.py    # the +4.1% vs -26.6% correction
python3 multi.py          # cross-state like-for-like table
```
