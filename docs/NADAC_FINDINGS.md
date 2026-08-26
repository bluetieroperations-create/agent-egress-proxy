# NADAC week one: FAIL (softly) — and the pattern is now the real finding

**Date:** 2026-08-25 · ~30 minutes, zero cost.

## The data is genuinely excellent

- Free, no key. Weekly. The file I pulled was dated **tomorrow** — it ships ahead.
- **The deltas are pre-computed.** The NADAC Comparison file has old price, new price,
  percent change and reason, 3.4M rows. No derivation needed.
- The deflation is real and clean:

| quarter | repricing events | median change | share falling |
|---|---|---|---|
| 2025Q3 | 80,420 | −0.91% | 64% |
| 2025Q4 | 80,749 | −0.41% | 56% |
| 2026Q1 | 81,205 | −0.97% | 66% |
| 2026Q2 | 80,985 | −0.35% | 57% |

## Why it still fails

**1. 46brooklyn Research already publishes it, free, for investors, since 2020.**
Interactive dashboard, generic deflation tracked drug by drug, married to Elsevier's
database. Third free incumbent in a row.

**2. The per-company wedge doesn't differentiate.** NDC's first five digits are the
labeler, so company-level rollup works — 335 labelers, and the codes map to real firms
(00093 Teva, 00378 Mylan/Viatris, 65862 Aurobindo). But every one of them lands in the
same narrow band:

```
60687  −0.78%     31722  −0.95%     00378  −0.79%
00904  −0.61%     50268  −0.72%     62135  −0.63%
65862  −0.77%     00093  −0.78%     62332  −1.17%
```

Half a point of spread. Deflation is a market-wide condition, not a company
differentiator. Splitting it by manufacturer adds precision, not insight.

Two further problems: the highest-volume labelers are mostly **repackagers**, not
manufacturers, so the obvious rollup is polluted; and NADAC is what **pharmacies pay**,
which sits downstream of manufacturer net price. The link to a generic maker's earnings
is real but indirect.

## Four for four — and that is the finding

| dataset | why it failed |
|---|---|
| NIH RePORTER, lab level | Signal didn't mean "closing" — 8/8 flagged labs thriving |
| NIH RePORTER, institutions | Already free (BRIMR); timeliness weak; premise reversed |
| MSHA | Claimed data doesn't exist; proxy has the wrong sign |
| NADAC | Already free (46brooklyn); company split doesn't differentiate |

Three of the four were killed by a **free incumbent**. That is not bad luck.

> **Free, clean, well-documented public data attracts free public analysis.**
> The easier a dataset is to work with, the more likely someone already gave the
> analysis away.

Set A and set B were both ranked with a "data effort: low" column treated as a virtue.
It was the opposite. **Low effort selected for spaces that were already served.**

## What that implies for the search

The unowned niches are the ones that are *hard*, not the ones that are *easy*:

- Data locked in **PDFs** (EMMA institutional financials — flagged "effort: high").
- Data scattered across **thousands of jurisdictions** (liquor and health permits —
  flagged "very high").
- Data requiring a **join nobody has built** (Chapter 11 creditor matrices — the one
  entry in either set with no incumbent named).

Every one of those was ranked *down* for being hard. On the evidence, the difficulty
was the moat, and I ranked it as a cost.

## Recommendation

Stop testing easy datasets. The next candidate should be chosen because it is
**annoying**, not because it is clean — and the first check stays the same: who already
publishes it free?

On the current evidence, the strongest remaining candidate in either set is the
**Chapter 11 creditor graph**: PDF-and-portal acquisition, hard entity resolution, and
the only entry where the incumbency check turned up nobody.
