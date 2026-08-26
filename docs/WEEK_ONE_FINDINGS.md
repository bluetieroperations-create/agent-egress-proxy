# Week one: can we tell a dying lab from a healthy one?

**Date:** 2026-08-25 · **Gate:** the hard gate in `docs/PHASE_PLAN.md`
**Verdict: PASS, conditionally — and the naive version would have failed in the field.**

Michigan, FY2019–2026: **17,900 awards, 3,857 principal investigators**, pulled in
25 seconds from a free keyless API. Everything below is measured on that corpus, not
estimated.

## What passed

**1. The API is as good as advertised.** No key, no auth, commercial use permitted,
coverage to FY1985. 17,900 records in 25 seconds through ordinary stdlib `urllib`.

**2. Entity resolution is solved for free.** RePORTER assigns every investigator a
stable `profile_id`. Measured coverage: **22,033 of 22,034 PI entries**. The fuzzy
name-matching I budgeted as the hard part of the build does not need to exist.

**3. Point-in-time history is RECONSTRUCTABLE, not just collectable.** Every record
carries `date_added` — **100% coverage**, freshest record three days old. Filtering
`date_added <= X` rebuilds what RePORTER knew on any past date.

> This is the most commercially significant finding of the week. The Phase 3 plan
> assumed 9–12 months of daily collection before a fund-grade backtest was possible.
> A substantial part of that history can be reconstructed **now**.
>
> Caveat, and it matters: `date_added` records when a row first appeared, not whether
> it was later edited. That is the difference between *reconstructable* and *provably
> point-in-time*. Forward collection still has to start immediately — but the backtest
> does not have to wait a year.

**4. The quiet period is now measured, not guessed.** Lag from budget start to
appearing in RePORTER:

| percentile | lag |
|---|---|
| p50 | **5 days** |
| p75 | 18 days |
| p90 | 57 days |
| p95 | **90 days** |
| p99 | 224 days |

82% post within 30 days, 95% within 90. **90 days is the right wait** — it is where
posting lag stops masquerading as lab closure, and it sets the product's latency.

## What failed, and what it cost

The naive version — flag any PI in the state whose NIH support ended — was tested by
reconstructing the corpus as of **2025-08-25** and checking flagged labs against what
actually happened since.

| quiet period | flagged | later re-funded | false positive |
|---|---|---|---|
| 0 days | 745 | 114 | 15.3% |
| 90 days | 591 | 45 | 7.6% |
| 365 days | 455 | 27 | 5.9% |

7.6% looked like a pass. **It was an artifact of a too-narrow question.** Restricting
to labs that had gone dark *recently* (90–365 days, the only ones that are leads
rather than history) and re-checking each PI **nationally** rather than within
Michigan:

> **Raw false-positive rate on the fresh cohort: 36.0%.**

More than one in three flagged labs was not dying. A dealer visiting three sites and
finding one thriving churns immediately, and in a segment of twenty-five firms that is
an account lost.

Decomposing the 36%:

| source | share | fixable? |
|---|---|---|
| **PI moved institution** — my query was state-scoped, so a move read as death | **24.3 pts** | Yes — verify candidates nationally |
| **Genuine signal error** — dark nationally, later re-funded anyway | **16.5 pts** | Irreducible |

So with a national verification pass: **~83.5% precision.**

## What the plan never anticipated

**Dollar size is a bad proxy for equipment.** The single largest lab on the first
generated list was a **$35.9M principal investigator running a longitudinal household
panel survey**. Enormous funding. Zero freezers, zero centrifuges, zero microscopes.
It was the worst possible lead, ranked first.

Department types across the flagged set: `BIOSTATISTICS & OTHER MATH SCI`,
`PUBLIC HEALTH & PREV MEDICINE` and `SOCIAL SCIENCES` sit next to `PHYSIOLOGY`,
`PATHOLOGY` and `PHARMACOLOGY`. Only the second group owns instruments.

A desk-science filter now runs before ranking. It is deliberately conservative —
`dept_type` is missing on ~45% of awards, and an unknown department is never treated
as desk-bound. Co-PIs on one grant are also collapsed to one site, so a dealer never
receives the same address twice.

## The caveat that cannot be measured

**No NIH funding is not the same as no funding.** RePORTER sees NIH and HHS. It does
not see NSF, DoD, DoE, foundations, industry contracts, institutional start-up
packages, or philanthropy. A PI dark in RePORTER may be perfectly well funded
elsewhere.

**83.5% is therefore an upper bound on precision for "this lab is winding down."**
The true rate is lower by an unknown margin, and no amount of NIH data will close it.
Cross-checking NSF's public awards API is the obvious next reduction, and it is the
first thing to build in week two.

## The architecture this produced

You do **not** need a national corpus. A state corpus plus a national verification
pass over the small candidate set is equivalent and costs ~150 extra API calls:

```
state corpus (17,900 awards, 3,857 PIs)
  → dark 90–365 days, bench science, ≥$500k          130
  → national verification (drop the movers)          106   (−24)
  → collapse co-PIs to one site                      105   (−1)
```

**105 Michigan labs, $268M in ended NIH support, median lab $2.0M.** That is one
state. It is a real list, and it is emailable tomorrow.

## Verdict

The gate passes. The signal is real, separable, and cheap to compute.

But the version described in the plan would have shipped at roughly **64% precision**
and churned its first dealers — and the three corrections that took it to ~83.5% were
all found by *looking at the output*, not by reading the API docs.

**Week two:** cross-check NSF to attack the blind spot, then hand-verify twenty of
these 105 against department pages and news before anything is sent to anyone.

## Reproducing

```sh
cd labclosure
python3 fetch_reporter.py MI
python3 run.py MI
python3 -m unittest test_lab_signal -v    # 14 tests, each names the mutation it kills
```
