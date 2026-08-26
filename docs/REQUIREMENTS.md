# Requirements ledger — what you've actually asked for, and what it implies

**Date:** 2026-08-25 · **Governs:** `COLD_START_DATA.md`, `FMCSA_GTM.md`,
`REMOTE_FIRST.md`, `COLD_START_DATA_II.md`

Every need below is traced to where you stated it. Inferred items are marked as
inferred and are not treated as decided.

---

## Part 1 — The raw list, in the order you said it

| # | Need | Where |
|---|------|-------|
| 1 | Data must be **public** | opening request |
| 2 | Data **nobody is using or doing anything with** | opening request |
| 3 | Data that is **highly valuable** | opening request |
| 4 | Must support **cold-start bootstrapping** — usable before you have customers | opening request |
| 5 | **Deep search**, not off-the-cuff | opening request |
| 6 | Must be **profitable and monetizable** | opening request |
| 7 | **Ten examples**, and things "we could do" — actionable, not theoretical | opening request |
| 8 | Identify the **most profitable** one | "which one is most profitable" |
| 9 | Identify the **easiest to do** one | "and easy to do" |
| 10 | **Work backwards from the moment the customer buys** | "from the point of the customer buying" |
| 11 | Explain the **full workflow process**, not just the endpoint | "explain the workflow process" |
| 12 | Explain **how we get there** — the path, not the destination | "how we'll get there" |
| 13 | **Validate the buyer before building anything** | "Before we do anything, who buys this?" |
| 14 | **Named buyers**, not buyer categories | "Who am I selling to" |
| 15 | The **selling mechanism** — how outreach actually happens | "how am I doing that?" |
| 16 | **100% remote execution** — everything from behind the computer | "behind my computer" |
| 17 | **In-person is gated behind realized revenue** from the thing being sold | "already had to have made revenue" |
| 18 | Keep the **working-backwards method** as the standing frame | "keep the working backwards process in mind" |
| 19 | **Fresh research** when the constraint changes | "do another search for fields" |
| 20 | **Ten more, genuinely different** — no recycling | "10 more that are different" |

### Inferred, not stated — flagged so you can correct me

| # | Inferred need | Why I inferred it | Status |
|---|---|---|---|
| i1 | **Solo or very small operator** | No team ever referenced; you ask what *you* will be doing | Assumed |
| i2 | **Low or no capital outlay** | "Easy," free data emphasis, no budget mentioned | Assumed |
| i3 | **Speed to first dollar matters** | Revenue gates travel, so revenue is the near-term goal | Assumed — **and this one is load-bearing** |
| i4 | **Reuse the existing codebase** | The repo's machinery keeps being the reason things are "easy" | Assumed |
| i5 | **Avoid legal blowups** | I raised FCRA, defamation, MNPI; you didn't ask | Assumed |

---

## Part 2 — Recategorized

The list above is chronological, which is useless for deciding. Here it is re-cut by
**what each need actually does to a candidate idea.**

### A. Hard constraints — binary filters. Fail one, the idea is dead.

| | Constraint | From |
|---|---|---|
| **A1** | The data is public | 1 |
| **A2** | Nothing in-person before revenue exists | 16, 17 |
| **A3** | Buildable and sellable by one person at a desk | 16, i1 |
| **A4** | No meaningful capital or paid data required | i2 |
| **A5** | Works with zero existing customers — cold start | 4 |

These are not preferences to trade off. An idea that requires a conference booth, a
data licence, or a five-person team is out regardless of how attractive it looks.

### B. Objectives — the things you're maximizing, which *do* trade off

| | Objective | From | Currently |
|---|---|---|---|
| **B1** | Profit / revenue ceiling | 6, 8 | Alt-data wins: ~$80k per client-year |
| **B2** | Low build difficulty | 9 | MSHA, NADAC, FMCSA all near-trivial |
| **B3** | Speed to first dollar | i3 | **Unresolved — see the conflict** |
| **B4** | Unowned / low competition | 2 | Checked per idea, incumbents named |
| **B5** | Intrinsic data value | 3 | Proxied by "maps to a ticker" |

### C. Method — how you want the work conducted

| | Requirement | From |
|---|---|---|
| **C1** | Research must be deep and sourced, not asserted | 5, 19 |
| **C2** | Reason backwards from the purchase, never forwards from the tech | 10, 18 |
| **C3** | Buyer identified and validated before any build | 13 |
| **C4** | Present breadth (tens), then narrow to a single pick | 7, 8, 20 |
| **C5** | Incumbency checked honestly — say when a space is taken | implicit in 2 |

### D. Deliverable form — what an answer has to contain to count

| | Requirement | From |
|---|---|---|
| **D1** | Named companies, not buyer categories | 14 |
| **D2** | Named channels and named people | 15 |
| **D3** | A concrete step sequence with a path | 11, 12 |
| **D4** | A decision, not a survey | 8, 9 |
| **D5** | Non-overlapping options when more are requested | 20 |

### E. Open — not yet decided, and now the bottleneck

Every one of these changes the answer, and none has been settled:

| | Open question | Why it matters |
|---|---|---|
| **E1** | **Fast first dollar, or highest ceiling?** | Directly determines the pick. See below. |
| **E2** | How many hours a week can you put in? | Decides whether a PDF-parsing corpus is viable at all |
| **E3** | Any capital at all, or strictly zero? | Decides whether you can buy a $2k domain/data seed or must be free-only |
| **E4** | Selling to **investors** or **operators**? | Investors pay more and buy remotely; operators buy faster and smaller |
| **E5** | Building a business, or an asset to sell? | Changes whether you optimize for the moat or for cashflow |
| **E6** | Appetite for legal exposure? | Scoring companies invites disputes; index data doesn't |

---

## Part 3 — The conflict in your current constraint set

Stated plainly, because it is the most important thing in this document:

> **A2 (no in-person before revenue) pushed the answer toward alt-data. Alt-data has
> the slowest first dollar of anything we looked at — 3 to 9 month cycles, fewer than
> 1 in 5 trials converting. So the constraint intended to protect you until revenue
> arrives has selected the path where revenue arrives last.**

That is not an argument against the constraint. It is an argument that **B3 (speed to
first dollar) has to be promoted from an inference to a stated priority**, because as
soon as it is, the answer changes shape.

Three coherent resolutions:

**Resolution 1 — accept the slow path.** ClinicalTrials.gov PIT corpus, MSHA, or
NADAC sold to funds. Highest ceiling (~$1.6M/yr for one well-placed dataset), fully
remote, compliance-clean. First revenue realistically 4–9 months out. Correct if you
have runway and are optimizing for the ceiling.

**Resolution 2 — add a fast lane deliberately.** Run one desk-only, low-ticket,
self-serve product alongside the alt-data build: a monitoring feed or a niche
directory sold at $50–500/month on a card, no calls, no procurement. Slower ceiling,
but it can pay in weeks. Correct if runway is the binding constraint.

**Resolution 3 — sell to operators, not investors.** Same datasets, but pointed at
vendors' sales teams instead of funds — the NIH RePORTER play has this built in
(tools funds *and* the tools vendors' own reps), as does EMMA (muni funds *and* anyone
selling into hospitals). Operators buy smaller, faster, and still entirely remotely.
Correct if you want money in month two without abandoning the ceiling.

**Resolution 3 is the one I'd pick**, because it satisfies A2 and B3 simultaneously
without splitting your attention across two products.

---

## Part 4 — The filter, as one page

Apply to any candidate idea, in this order. Stop at the first failure.

1. **Is the data public and free?** — no → discard (A1, A4)
2. **Does it already contain its own failures/outcomes?** — no → discard (A5)
3. **Can one person get it without scraping fifty jurisdictions?** — no → park (A3)
4. **Can you prove point-in-time history on day one?** — no → 18-month delay, park
5. **Does the buyer transact without a meeting?** — no → discard (A2)
6. **Can you name five real buyers and reach them from a desk?** — no → not ready (D1, D2)
7. **Is the space genuinely unowned, or just unfamiliar to you?** — check (B4, C5)
8. **What's the first dollar timeline?** — weeks / months / quarters (B3)
9. **What could a wrong score cost someone?** — high → scoring risk, prefer index data (E6)

An idea that clears 1–7 is worth a spike. The spike must be designed to fail cheaply,
in week 2, not month 9.

---

## Part 5 — Which document answers which need

| Document | Serves |
|---|---|
| `COLD_START_DATA.md` | 1–7 — set A, the original ten, with the five-test rubric |
| `FMCSA_GTM.md` | 8–15 — the single pick, worked backwards from the cheque, plus named buyers and channels |
| `REMOTE_FIRST.md` | 16–19 — Test 6 added, the honest demotion, and the re-ranked pick |
| `COLD_START_DATA_II.md` | 19–20 — set B, ten new fields chosen under the constraint |
| **this file** | The spec all four answer to |

## The one thing I need from you

**E1.** Fast first dollar, or highest ceiling? Everything downstream — which dataset,
which buyer, which product shape — resolves the moment you answer it, and until then
I'm optimizing against an assumption I made on your behalf.
