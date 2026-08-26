# Step 1: can a modern filing be parsed? YES

**Date:** 2026-08-25 · **Verdict: PASS.** 211 creditors from one filing.

## What happened

Pulled a certificate of service from a Chapter 11 case filed **eight days ago**
(Systematic Audio, W.D.N.C., case 26-50236) from Epiq. Native PDF v1.7 — not a scan.

Text extraction is **clean**. No OCR damage, correct reading order, no garbled columns.
The contrast with the 2014 filing tested in `CH11_FINDINGS.md` is total.

## Better than expected: it isn't just the top 20

I went looking for Official Form 204 — the 20 largest unsecured creditors. What I found
was the **full creditor matrix** inside the certificate of service: every party the
debtor served, alphabetically, with addresses.

**211 unique creditors from a single filing**, not 20. Certificates of service are filed
in every case, repeatedly.

Real names, immediately recognisable:

```
AIRGAS USA, LLC              C.H. ROBINSON WORLDWIDE, INC
CAPITAL ONE                  ECHO GLOBAL LOGISTICS INC
ESTES EXPRESS LINES          FEDEX FREIGHT
KEYBANK NATIONAL ASSOCIATION MEDALLION CAPITAL, INC
```

Composition: ~36% carry a company suffix, ~6% are tax authorities, the rest are
businesses without a formal suffix. Individuals (77 of them) are **privacy-redacted as
"ADDRESS ON FILE"** — which conveniently removes them, since a supplier graph wants
businesses anyway.

## The parse rule

Each row is `NAME STREET<2+ spaces>CITY ST ZIP`. Name and street are separated by a
**single** space; the wide gap sits before the city. So the city/state/zip tail is the
anchor, and the name ends where the address begins — a street number, a PO box, or
`C/O`.

Two real bugs found by reading the output, both now regression-tested:

- Splitting on the *first* digit run destroyed names that legitimately start with
  numbers — `24 HOURS CLOSING`, `401(K) ADMINISTRATOR`.
- Treating `ONE` as an address marker truncated **CAPITAL ONE** to `CAPITAL`.

Residual error is roughly 1–2% (a couple of truncated names per 200).

## Honest limits

- **Only native PDFs.** The 2014 scanned filing still fails — name after amount, OCR
  turning `$0.00` into `$0,00`. Historical backfill is a separate, harder project.
- **Names, not amounts.** The matrix lists who is owed, not how much. Amounts live in
  Schedule E/F and Form 204, which are separate documents with their own layouts.
- **Kroll and Omni still block automated access**, and a headless browser could not be
  made to work through this session's egress proxy. Epiq and CourtListener are open,
  so the pipeline works — it just doesn't cover every case yet.
- One filing is one filing. The layout is standard-ish, not standardised.

## What this buys

For the first time in five datasets, the data does the thing it was supposed to do.

`ch11/matrix_parse.py` is pure + stdlib (plus pypdf), 11 tests, each naming the mutation
it kills.

## Step 2 — the one that decides everything

Parse ~50 filings and count how often the same creditor appears in more than one.

- **If creditors recur:** there is a graph, and the product exists.
- **If every creditor appears exactly once:** there is no graph, and every buyer
  hypothesis dies at the same instant.

Do not think about buyers before running it.
