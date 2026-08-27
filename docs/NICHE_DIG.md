# Digging into niche fields: FAA Service Difficulty Reports

**Date:** 2026-08-25 · Checked TTB alcohol labels, USDA organic, FAA defect reports.

## Quick kills

| field | verdict |
|---|---|
| **TTB COLA** (every alcohol label approved — 2.9M records, 2,500/week, a product-launch feed for the whole beverage industry) | **Taken.** COLA Cloud already processes and enriches it with a web app and API. |
| **USDA Organic INTEGRITY** (certified operations plus suspended/revoked lists, 327 enforcement cases) | Possible, unchecked. Guides exist, no data product found. |

## FAA SDR: the best raw data I have found in this whole search

Every mechanical defect reported on US civil aircraft. **Free CSV per year, direct
download, 1975–present.**

- 2025: **67,626 reports**. 2023–2026 pulled in under a minute.
- Fill rates are excellent, not the mess I expected:

| field | filled |
|---|---|
| Tail number | 99.5% |
| Aircraft make / model | 99.9% |
| **Airframe serial number** | **99.8%** |
| Aircraft total flight hours | 99.1% |
| Part name / condition | 100% |
| Part number | 35.8% |

Structured beyond the basics: corrosion level (8,211 records), crack length (1,163),
JASC system code, plus a free-text discrepancy narrative.

Part conditions in 2025: **CORRODED 14,679 · CRACKED 11,148 · INOPERATIVE 4,442 ·
FAILED 1,530**.

### What it uniquely enables

Serial number is 99.8% filled, so the data supports **defect history for an individual
airframe** — 9,019 distinct aircraft reported in 2025 alone:

```
153 reports   CNDAIR CL6002D24  s/n 15099
124 reports   CNDAIR CL6002C10  s/n 10072
113 reports   AIRBUS A330223    s/n 0778
105 reports   BOEING 7377H4     s/n 32535
```

That is the sort of thing a lessor, a used-aircraft buyer or an insurer would want before
signing.

## Why I am not calling this a win

**1. The count measures the reporter, not the aircraft.**

An airframe with 153 reports may have a meticulous operator, not a bad aircraft.
Reporting culture varies enormously between operators, and the diligent ones look worst.

This is the **third time** this exact trap has appeared:

| dataset | the count actually measured |
|---|---|
| NIH labs | Award size measured *survivability*, not equipment |
| NHTSA complaints | Complaint volume measures *how popular the car is* |
| **FAA SDR** | **Report volume measures the operator's reporting culture** |

> **In any voluntary or mandated reporting dataset, the count measures reporting
> behaviour first and reality second.** Normalising by exposure — fleet size, flight
> hours, vehicles in operation — is not a refinement. It is the whole job.

**2. Incumbency is ambiguous, and that has killed five ideas already.**

- **AviationDB** already offers free SDR search by tail number and serial. The lookup
  layer is taken, exactly as EWG took the PFAS lookup layer.
- **Cirium** tracks **450,000 tail numbers** including maintenance events and runs the
  standard valuation product. **IBA** does appraisals. Whether either folds SDR defect
  history in could not be established from outside.

## The pattern across everything checked

Ten-plus datasets now, and every single one has the same four layers:

1. A free official government portal — lookup only
2. A free third-party wrapper on top of it
3. Sometimes an expensive enterprise incumbent
4. An "analytical layer" in between that looks wide open

**Layer 4 always looks open.** The uncomfortable explanation is that the buyers who would
want it usually already hold better private data — airlines have their own reliability
systems, Cirium has 450k tails, D&B has tradelines. The public dataset is the *worst*
version of what the serious buyer already owns.

That is not a reason to stop. It is a reason to stop searching for unowned data and start
by finding a buyer whose private data is genuinely worse than the public data. Those exist
— but they are found by asking people, not by downloading files.

## If SDR continues

1. Ask two aircraft lessors or appraisers: *"when you evaluate a specific tail number, do
   you look at its SDR history, and where do you get it?"* Their answer decides it in a
   day.
2. Normalise by fleet hours before showing anyone a ranking. An un-normalised list of
   "worst aircraft" is wrong and would be embarrassing.
