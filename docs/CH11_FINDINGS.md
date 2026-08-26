# Chapter 11 creditor matrices, week one: the first one that didn't die

**Date:** 2026-08-25 · ~40 minutes, zero cost.
**Verdict: PARTIAL PASS.** Real obstacles found. None fatal yet. One decisive
question left open, and it is named below.

## 1. Incumbency — the check that killed the last three

Paid incumbents exist for bankruptcy *documents and case data*:

- **BankruptcyData** — filings, petitions, schedules, docket activity, sold to law
  firms, advisors, investors and lenders.
- **Epiq AACER** — 500,000+ bankrupt companies, daily updates across 93 courts, back
  to 2007, with creditor information.

So "bankruptcy data" is a served market. But the specific thing set A proposed — the
**cross-case creditor graph**, i.e. *this supplier has appeared as an unsecured creditor
in six filings in three years* — did not turn up in any product description.

**Honest limit: absence of evidence is not proof.** I could not find it; I cannot prove
nobody sells it. That is weaker than the BRIMR/46brooklyn findings, where the incumbent
was named and free within minutes.

## 2. Access is real, but only for about half the market

| source | automated access |
|---|---|
| Epiq | **200 — works**, exposes direct document URLs |
| Stretto | 200, but JS-rendered; no static case links |
| CourtListener / RECAP | **200 — free, permanent, no account for documents** |
| Kroll | **403 at CloudFront** — blocked, including robots.txt |
| Omni | **403** — blocked |
| Verita | TLS failure |

Set A assumed "free on claims-agent portals." True for a human with a browser. **About
half of them block automated collection at the CDN.** Kroll in particular handles many
of the largest cases.

That is a real cost, not a blocker: a headless browser gets past it, and CourtListener
covers high-profile cases free. But it means the pipeline is browser-driven, not
`curl`-driven, for a meaningful share of filings.

## 3. Documents download and text extracts

Pulled a real 26-page filing from Epiq: `application/pdf`, 1.4 MB, clean download.
Text extraction produced 13,307 characters over four pages. **The plumbing works.**

## 4. The actual problem: structure, not access

Here is the raw extraction of a secured-creditor row from a 2014 filing:

```
X $8,141,112.83 UNDETERMINED BANK OF AMERICA, N.A.
I FLEET WAY
SCRANTON, PA 18507
```

The creditor's **name appears after the amount**. Column headers came out as
`1 CONTINGENT / 4) C '— ' CI / H til t:J / DISPUTED`. And OCR rendered `$0.00` as
**`$0,00`** — a silent corruption that would produce a wrong number rather than an
obvious error.

That document is a scanned filing, which is the worst case. But it demonstrates the
real work: **every case has its own layout, and older filings are images, not text.**

This is exactly the "annoying data is the moat" hypothesis meeting reality. The
question is whether it is *tractably* annoying or *intractably* annoying.

## 5. The decisive open question

**Do modern filings parse cleanly?**

Two reasons to expect they might:
- Filings from roughly 2020 onward are usually **native PDFs**, not scans.
- **Official Form 204** (the 20 largest unsecured creditors) is a *standardised form* —
  same layout in every case, by rule.

If a recent Form 204 yields clean name/amount rows, the whole approach is viable and the
2014 example is just legacy backfill. If it does not, this needs OCR plus layout
inference and becomes a much bigger build.

I could not resolve this today — the accessible agents either had only older cases
(Epiq) or were JS-rendered (Stretto), and the recent large cases sit behind Kroll's
block. **Resolving it is the entire next step**, and the route is a headless browser
against Kroll, or CourtListener's free API with a token.

## Why this one is different

Four datasets before this died on contact:

| dataset | killed by |
|---|---|
| NIH labs | Signal meant the wrong thing |
| NIH institutions | Free incumbent (BRIMR) |
| MSHA | Data didn't exist |
| NADAC | Free incumbent (46brooklyn) |

Chapter 11 has produced no such kill. What it produced instead is a **cost estimate**:
browser automation for half the sources, PDF layout parsing, and entity resolution on
creditor names.

That is the difference between "someone already did this" and "this is expensive." Only
the second one is a business.

## Next test, in order

1. Get one recent **Form 204** and parse it. Clean rows or not — this decides everything.
2. If clean: parse 50 filings, count how often the same creditor recurs. If recurrence
   is rare, there is no graph and no product.
3. Only then think about buyers.

Do not skip to step 3. That was the mistake in weeks one and two of the NIH work.
