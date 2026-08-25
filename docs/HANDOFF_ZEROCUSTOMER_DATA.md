# Handoff — the zero-customer data strategy: what it is, what we hold, what's unexploited

Written 2026-08-25. Picks up a side-thread, not a build task. Read this before
proposing new uses of the corpus; two obvious-looking ideas are already ruled out
below, with the measurement that rules them out.

---

## 1. What we call it

The repo already has a name and it should be standardized rather than replaced:
**zero-customer bootstrapping** (`docs/AUDIT_ZEROCUSTOMER.md`), interchangeable with
**cold-start bootstrapping**, which is the term the wider literature uses for the
chicken-and-egg problem it solves.

The precise description, if a sentence is needed:

> **Cold-start bootstrapping from public, self-labeling settlement exhaust.**

Three properties, and it is worth keeping them separate because they fail for
different reasons:

1. **Public.** x402 settles in USDC on Base. The history exists whether or not
   anyone is our customer, so there is no data/demand chicken-and-egg to break.
2. **Self-labeling.** This is the part that actually matters and the part most
   people miss. A transfer that landed *is* a successful settlement — the label
   comes free with the observation. No annotation, no customer feedback loop, no
   waiting. In ML terms this is *distant supervision*: the environment emits the
   ground truth as a side effect of operating. `rwa_backfill.py` states it plainly:
   "on-chain history is already LABELED."
3. **Exhaust.** It is a byproduct of other people's operations, not something we
   asked anyone to produce.

### The honest part

**The raw data is not a moat.** Anyone can read Base. Blockscout is keyless and free.
If someone decides to compete on "count USDC transfers to x402 payees," they will
have the same rows we have inside a week.

What is defensible is narrower and worth being precise about:

- **The join.** Advertised price (crawled from sellers' own 402 challenges and the
  Bazaar) × settled price (chain) × whether the endpoint still resolves and can be
  parsed (our own probe). Each leg is individually cheap; holding all three joined
  and address-normalized is not, and nothing we have found does it.
- **Accumulated labels over time.** The chain gives settlement. It does not give
  *dispute*, *outcome*, or *verdict-then-what-happened*. Those accrue only by running
  (`ledger.py`, `rwa_outcomes.py`), and they are the flywheel's real output.
- **Calibration discipline.** The reversibility locks (`SYBIL_RING_GATES`,
  `ISSUER_TRUST_GATES`, `REVERT_AXIS_GATES`) and the measured false-flag rates are
  judgment encoded in the repo, not data anyone can scrape.

Say "the join and the labels are the moat," not "the data is the moat." The second
is false and an informed reader will know it.

---

## 2. What we actually hold (measured 2026-08-25)

| Asset | Size | Source |
|---|---|---|
| `data/reputation_seed.db.gz` — settlements | **37,943 rows** | Blockscout backfill |
| ... distinct payees | **281** | |
| ... distinct payers | **2,028** | |
| ... timestamp range | 2024-04-13 → 2026-08-18 | |
| `data/directory.json` — ecosystem map | **198 payees** (advertised min/max price, resources, networks, payer counts, sanctions flag) | `discovery_crawl` + `ecosystem_scan` |
| `data/liveness.json` — endpoint probe | **195 hosts** classified | `directory_liveness` |
| `data/category_index.json` | 5 category price baselines | `category_pricing` |
| `data/divergence_index.json` | 18 advertised-vs-settled ratios | `price_integrity` |
| RWA corpus (`rwa_ledger`, `rwa_backfill`) | 674 acquisitions, 4 permissioned issuers | Blockscout token transfers |

Liveness breakdown, which is its own finding: of 195 hosts, **71 body_accepts,
2 hdr_accepts, 23 wellknown, 86 opaque_402, 12 other, 1 dead**. So 86 endpoints
advertise a price nobody — including us — can read.

---

## 3. The constraint that governs everything below

**The settlement corpus is a recent WINDOW per payee, not complete history.**

`chain_backfill` paginates newest-first with a page cap (`--max-pages 4`), so a
high-volume payee contributes only its most recent transactions. This was already
documented as the reason `burst_sybil` must stay advisory. It was re-confirmed here
by a cleaner test — median visible span (first→last settlement) **by settlement count**:

| settlements | payees | median visible span |
|---|---|---|
| 1–9 | 6 | 28 days |
| 10–49 | 41 | 29 days |
| 50–199 | 168 | 24 days |
| 200+ | 66 | **18 days** |

Under complete history, span would **grow** with volume. It shrinks. That is the
pagination window, and it silently contaminates any time-based derivation.

**Concretely, this kills two attractive ideas** — do not re-propose them without a
complete-history source:

- **Endpoint mortality / survival curves.** Naively the corpus says 98.6% of payees
  settled within the last 30 days and 99.6% within 90 — which reads as "the x402
  ecosystem is almost entirely alive." It is an artifact of *when we crawled*, not
  evidence of liveness. Median "lifespan" of 23 days is the window width.
- **Cohort / retention analysis.** Same reason: first-seen dates are crawl-bounded,
  so cohorts are fictional.

What would fix it: full per-payee pagination (expensive against a keyless
rate-limited endpoint), or an archival source, or simply accumulating our own
observations forward from now with a fixed anchor date.

---

## 4. Candidate uses, graded honestly

Novelty caveat, stated because it bit us this month: competitor claims made from
README blurbs were wrong twice (AgentRank is unreachable; Warden is a different
category). **"Nobody else does this" below means "we found no one doing it," not a
verified market survey.** Verify before putting any of it in marketing.

### Strong — we hold the data, the work is small, and we found no equivalent

**A. Payer-side reputation as a product.** We have **2,028 distinct payers**. The
entire market scores *sellers*. `payer_reputation.py` already exposes
`payer_profile()` / `.screen()` and a `screen_payer` MCP tool. The buyer is a
facilitator, wallet, or seller who wants to know *who is paying me* before settling —
and unlike seller risk, nobody is selling this. Cold-start is neutral, never a block,
so it is safe to expose. **This is the most under-exploited asset we hold.**

**B. Advertised-vs-settled divergence, generalized.** Built and shipped as a HOLD
gate (`price_integrity.py`), but it is currently *only* a gate. The same join answers
a question sellers and buyers both want: "is this endpoint's list price real?" A
public per-endpoint honesty score is a distribution asset, not just a signal.

**C. A category price index for agent services.** `category_index.json` is a CPI for
x402 by category, computed from settled reality rather than advertised prices. Nobody
publishes one. It is cheap to publish, it is citable, and it makes Blackwall the
reference for what things cost. Currently 5 categories — thin, but real.

### Medium — real, but blocked or needs work

**D. The failure denominator.** `revert_scan.py` reads *failed* transfer attempts,
which fixes the survivorship bias in a success-only corpus. Genuinely novel. Currently
dormant: the axis activated on permissioned RWA issuers, then tried to downgrade
BlackRock for correctly enforcing its allowlist, so `REVERT_AXIS_GATES` is off and
**must stay off**. Re-home it as an *asset*-level friction signal, not an issuer-trust
signal.

**E. "Can this endpoint actually be paid?"** The liveness survey is operational
reality no chain indexer has: 86 of 195 endpoints serve a 402 whose requirements are
unreadable. That is a real interoperability finding and a plausible public artifact
("the state of x402 payability"). It is also a BD list.

### Weak / ruled out

**F. Mortality, survival, cohorts.** Ruled out above. Window artifact.

**G. "We have proprietary settlement data."** We do not. See §1.

---

## 5. Where to pick this up

Nothing here is committed to as a build. The order I would argue for:

1. **A (payer reputation)** — the code largely exists; the missing piece is packaging
   and someone to sell it to. Start by asking a facilitator whether they would use it.
2. **C (price index)** — cheapest publishable artifact, feeds credibility and SEO.
3. **E (payability report)** — already measured; needs writing up, not computing.

Open questions for the next session:

- Is payer-side screening a product or a feature? It may be worth more as the thing
  that gets a facilitator integration than as a standalone.
- Can we get complete per-payee history cheaply enough to unlock §4F? Worth one spike
  against an archival provider before abandoning the temporal axis entirely.
- The 86 `opaque_402` endpoints: is there a third carrier we have not implemented, or
  are they genuinely unpayable? Nobody has looked at more than a sample by hand.

## Related reading

`docs/AUDIT_ZEROCUSTOMER.md` (the pipeline audit), `docs/DATA_COMPLETENESS.md` (the
completeness convergence eval and why `burst_sybil` stays advisory),
`docs/DIRECTORY_LIVENESS.md`, `docs/PAYER_GRAPH.md`, `docs/CATEGORY.md`.
