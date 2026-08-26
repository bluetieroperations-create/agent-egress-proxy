# MSHA week one: FAIL

**Date:** 2026-08-25 · Tested in ~20 minutes, zero cost.

## What I claimed in set B

"MSHA publishes quarterly production per mine, back to 2000 — aggregates, cement,
lithium, sand." **That was wrong.** I took it from a search snippet that said
"employment and production" without checking whether production covered non-coal.

## Reason 1: there is no production data for aggregates

The field is literally named `COAL_PRODUCTION`.

| mine type | rows | non-zero production |
|---|---|---|
| Coal | 344,451 | populated |
| **Metal / nonmetal** (aggregates, cement, lithium, sand) | **2,416,612** | **52 rows — 0.0%** |

Fifty-two rows out of 2.4 million. MSHA reports tonnage for coal only.

## Reason 2: hours worked does not proxy production

Hours *is* published, 100% coverage, per mine, per quarter, since 2000. USGS itself
uses MSHA hours to estimate production for non-responding producers, so this was the
obvious fallback.

It does not work:

| | Q1 2026 vs Q1 2025 |
|---|---|
| MSHA metal/nonmetal hours | **−0.8%** |
| USGS aggregate production | **+6%** |

Seven points apart, **opposite directions**. Mechanically sensible — productivity rises,
so hours per ton falls. Hours measures labor, not output.

## One thing that was better than expected

MSHA data is **fresher than USGS**: the file already holds 2026 Q2, while the latest
public USGS quarterly is Q1 2026. A quarter ahead — but ahead with the wrong signal is
worth nothing.

## Also: USGS already publishes the aggregate free

Quarterly crushed stone and sand & gravel reports, PDF and XLSX, plus state-level time
series back to 1971. Same shape as the BRIMR problem in `AGGREGATE_FINDINGS.md`.

## What survives

Per-mine **employment** history, complete since 2000. That supports mine
opening/closure and headcount trends — not production, and not a signal that beats
USGS. Small market; not a fast-dollar product.

## Score so far

Three datasets tested against the fast-dollar requirement. Three failures:

| dataset | why it failed |
|---|---|
| RePORTER, lab level | Signal doesn't mean "closing" — 8/8 flagged labs thriving |
| RePORTER, institution level | Already free (BRIMR); timeliness edge weak; contraction reversed |
| **MSHA** | **The claimed data does not exist; the fallback proxy has the wrong sign** |

The pattern is consistent and worth naming: **the check that kills these is always
"does the number mean what I need," and it is always cheap.** Each of the three died in
under two weeks for zero dollars.

**Next: NADAC** — and the first thing to verify is not the API, it is whether anyone
already sells the weekly delta.
