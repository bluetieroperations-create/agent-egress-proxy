# PFAS drinking water: the best lead so far

**Date:** 2026-08-25 · ~45 minutes · **Status: promising, NOT yet validated.**

## Why look here

The instinct was right and better than the one I had been running. I was searching for
**unowned data**, which is a lottery — anything easy is taken. The better search is
**data that did not exist two years ago**, because an incumbent cannot have twenty years
of something that started in 2023.

Three live candidates from 2023–2026 were checked. Two died on the usual test:

| candidate | verdict |
|---|---|
| SEC Form SHO (short positions) | **Delayed to 2028.** No data exists yet. |
| SEC 8-K cyber incidents | Only ~78 filings in 2.5 years, and **Debevoise publishes a free tracker**. Too small, already served. |
| EU DSA transparency database | 22 billion records — but the **Commission itself** ships free analytics and an open-source analysis package, and **Tremau** and **Checkstep** sell benchmarking. Three incumbents. |
| **EPA UCMR 5 (PFAS in drinking water)** | **No commercial product found.** |

## The data

- **Free bulk ZIP**, no key, no scraping: `ucmr5-occurrence-data.zip`. Downloaded in
  **3 seconds**.
- **1,928,117 sample results** across **10,299 public water systems**, plus a ZIP-code
  crosswalk. Release dated **January 2026** — monitoring ran 2023–2025 and data is
  still landing through 2026.
- Covers systems serving roughly **88% of the US population**.

## The finding

EPA set the first-ever national PFAS drinking water limits in April 2024: 4 ng/L for
PFOA and PFOS, 10 ng/L for PFHxS, PFNA and HFPO-DA.

> **1,717 water systems — 16.7% of everything monitored — exceed those limits.**

| | |
|---|---|
| Systems monitored | 10,299 |
| Any regulated PFAS detected | 1,975 (19.2%) |
| **Over the legal limit** | **1,717 (16.7%)** |
| Large systems / small systems | 1,009 / 708 |

Worst by concentration (limit is 4.0 ng/L):

```
NASHVILLE, TOWN OF            NC   PFOS      490.0   -- 122x the limit
POTRERO ELEMENTARY SCHOOL     CA   PFHxS     250.2
RUNNING SPRINGS WATER DIST    CA   PFOS      236.5
COLLEGEVILLE TRAPPE JOINT     PA   PFOA      235.0
LUBECK PSD                    WV   PFOA      179.5
```

Concentrated in CA (164), FL (157), NJ (156), PA (119), NC (115), MA (106), TX (103).

## Why the number is real

The first run returned **zero exceedances** — a silent failure, exactly the class of bug
that matters. UCMR 5 reports in **µg/L**; the regulation is written in **ng/L**. The
comparison was wrong by 1000x and produced a clean, confident, completely false answer.

Also: **97% of the file is non-detects**, carrying `<` and an empty value. Reading those
as zeros is harmless; reading them as measurements would put every system over the
limit. Both traps are now regression-tested.

## Why anyone would pay

Unlike the previous five datasets, this list is attached to **money that must be spent**:

- Exceeding systems are **legally required to install treatment**. Each is a capital
  project.
- The 3M and DuPont settlements total **over $14 billion**, paid to entities that
  **detected PFAS in their drinking water**. Municipalities began receiving Phase 1
  payments in summer 2025. Detection data is the eligibility key.
- Plausible buyers: water treatment vendors and engineering firms (a ranked, sized,
  geolocated target list), municipal advisors, plaintiff firms, and investors in
  listed water utilities that carry the liability.

## What is NOT yet established — read this before building

1. **The incumbency check was thin.** One search. EWG runs a well-known consumer PFAS
   map and EPA ships its own Data Finder, so *"look up my zip code"* is definitely
   taken. Whether a **commercial targeting product** exists was not properly tested.
   **That is the next task, and it is the one that killed three of the last five ideas.**
2. **No buyer has been contacted.** Treatment vendors and engineering firms live in this
   data professionally. They may already track it internally, in which case there is no
   sale.
3. **Compliance deadlines shifted.** EPA has revisited the rule and timelines; the
   urgency claim needs checking against the current schedule, not the 2024 announcement.

Finding something promising in 45 minutes, after five failures, should raise suspicion
rather than confidence. The correct next move is the boring one.

## Next

1. **Incumbency, properly.** Search vendor sites, trade press and conference exhibitor
   lists for anyone selling UCMR 5 targeting. Half a day.
2. **Ask three people.** Email a treatment vendor, an engineering firm and a municipal
   advisor: *"do you already have a ranked list of exceeding systems, and where does it
   come from?"* Their answer settles it faster than any analysis.
3. Only then build anything.

## Code

`pfas/pfas_exceedance.py` — pure + stdlib, 10 tests, each naming the mutation it kills,
including the µg/L-versus-ng/L conversion and the non-detect trap.
