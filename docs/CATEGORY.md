# Service categories — shared classifier, Traceipt handoff, and a Blackwall idea

`categories.py` is a tiny, stdlib-only classifier: given an x402 resource URL, return
a coarse **service category** (`finance`, `ai-agents`, `onchain`, `commerce`,
`dev-tools`, `search-data`, `content-media`, `email-comms`, `storage-files`,
`identity-security`, else `other`). It lives in its own module — no `ecosystem_scan`
or verdict imports — so any consumer can reuse it cheaply.

**It is DESCRIPTIVE ONLY.** The category is derived from the *seller-controlled*
resource URL, so it is advisory metadata for analytics/reporting. It must never gate a
verdict, key a billing decision, or be treated as an assertion of fact. Classification
of arbitrary API paths is fuzzy (~⅓ of a live crawl lands in `other`) — expected, not a
defect.

Already wired into Blackwall's `ecosystem_scan`: every endpoint profile carries a
`category`, `rank_directory` passes it into the trust directory, and `ecosystem_stats`
reports a `category_distribution` (the State-of-x402 report now shows *what the x402
economy sells*).

---

## 1. Handoff to Traceipt — tag receipts with category

**For the `claude/x402-product-ideas-6adgah` (Traceipt) session.** This is a shared-seam
item (a known open item): the classifier lives on the Blackwall branch but the
receipt schema lives in `traceipt/`, so this is a proposal, not a unilateral change.

**Proposal:** when Traceipt issues a receipt for an x402 settlement, tag it with
`categories.classify_resource(<resource_url>)` as an **advisory** field
(e.g. `receipt.meta.category`). Benefits:

- **Spend-by-category** — a pile of receipts becomes "this agent spent 60% on
  finance-data APIs, 30% on AI" — useful for the payer's own accounting and for anyone
  auditing an agent's activity.
- **Sector analytics / proof-of-mix** — Traceipt can publish "X% of anchored
  settlements this week were finance APIs" from receipts alone, no extra on-chain data.
- **Richer anchoring context** — when Blackwall auto-anchors a verdict
  (`verdict_anchor.py`), including the counterparty's category in the anchored
  *tokenless* view makes the proof self-describing ("GO for a commerce endpoint at T").

**Constraints (must hold):**
- **Advisory only.** Never a gate, a billing key, or a trust signal. Same posture
  Blackwall gives it.
- **Derived, not asserted.** The seller controls the URL; a mislabeled/adversarial URL
  yields a wrong category. Fine for analytics, not for anything trust-critical.
- **Schema coordination.** Adding a receipt field is a schema change both projects
  touch. Import `categories.py` (pure, stdlib, tested) rather than re-implementing, so
  the two sides can't drift. Keep the field OPTIONAL so old receipts stay valid.

**How to consume:** `from categories import classify_resource` →
`classify_resource(resource_url) -> slug`. That's the whole surface.

---

## 2. Blackwall idea (PROPOSED, not built) — per-category price baselines

Your "finance/markets history is overlooked data" instinct checks out. Blackwall's
price-anomaly gate today is **per-payee** (an amount vs. *this* counterparty's own
median) or per-resource-class. It has no view of what a whole *category* charges — yet
that data is sitting right there in the crawl.

**The overlooked data (live, from the Bazaar crawl):** categories have tight,
distinct median prices *and* wild in-category outliers:

| category | n | median | max | outlier |
|---|---|---|---|---|
| finance | 184 | $0.0050 | $5.00 | **1000×** |
| onchain | 110 | $0.0045 | $10.00 | **2200×** |
| ai-agents | 154 | $0.011 | $0.75 | 68× |
| commerce | 32 | $0.0020 | $1000 | huge |

**The gap it closes:** a **cold-start** payee (no per-payee history → generic HOLD
today) that quotes $5 for a finance API is a 1000× outlier *against its category
cohort* — but Blackwall can't see that today. A category-level baseline flags it.

**The design (small — it extends an existing mechanism):** `decide_payment` already
accepts a `peer_median` / `peer_index` (a peer-group price baseline). Compute a
**per-category price median** and feed it keyed by the counterparty's category. No new
gate — it flows through the existing peer-price signal (HOLD-only, fail-open).

**How it's built:**
- **ON-CHAIN settled amounts, not advertised prices.** The table above is *advertised*
  prices (seller-controlled, gameable). The baseline is what payees in a category
  *actually collected* on-chain (`reputation_store.price_history`, seeded by
  `chain_backfill`), which a seller can't inflate by editing a Bazaar listing.
- **Median-of-medians across ≥5 DISTINCT payees.** One high-volume/wash payee can't
  drag the rate, and a category with too few payees is omitted (`MIN_CATEGORY_PAYEES`).
  `other` is never indexed (not a coherent cohort).
- **Advisory / HOLD-only, never STOP; fail-open.** Category is fuzzy and derived from
  the seller-controlled resource URL; a wrong category → wrong baseline, so it can only
  ever *escalate to review*, never block, and a missing baseline is a no-op.

**Calibration (from the audit → eval).** The gate trips at **50×** the category's
on-chain median. An eval on real on-chain data found legit payees range up to ~20× their
category median (premium tiers within a wide cohort), while genuine gouges are 1000×+.
At the initial 10× the gate flagged 16% of *legit* payees (premium AI/finance APIs) —
false positives. 50× clears every observed legit payee (0% false positives) while still
catching an order-of-magnitude+ gouge. It targets **over**-pricing only (under-pricing
isn't a buyer risk).

**Shipped** (`category_pricing.py` + `blackwall.decide_payment`'s `category_median`
gate + `forecast`'s `category_index`), behind `BLACKWALL_CATEGORY_INDEX` (a precomputed
`{category: median}` JSON — build with `python category_pricing.py --store … --out …`).
Passed audit → eval → verify.

### Known limits (audit)
- **Evadable by resource spoofing.** The category comes from the request's `resource`
  URL; a gouger can omit it or pick a URL that classifies to `other` (no baseline) to
  dodge the gate. This is fail-open by design — evading it just forgoes the *extra*
  scrutiny; every base gate (reputation/Sybil/own-price/sanctions) still applies. The
  category signal is additive, never load-bearing.
- **Wrong-category → wrong baseline.** A misclassified resource gets the wrong cohort's
  median. Because it's HOLD-only, a false hit is a conservative error (human review),
  and the 50× margin makes it rare.
- **Baseline coverage.** Only categories with ≥5 distinct on-chain-active payees get a
  baseline; thin categories simply have no signal (fail-open).

---

## 3. Advertised-vs-settled divergence (SHIPPED) — bait-and-switch

`price_integrity.py`. A seller can list a cheap price on the CDP Bazaar to rank well in
discovery, then actually COLLECT far more on-chain. This payee-level trust signal
compares a payee's on-chain **settled median** to its most-**expensive advertised**
price: `ratio = settled_median / max_advertised`.

- **Metric uses MAX advertised** (not min) so a payee with a legit expensive *listed*
  endpoint that people pay is NOT flagged — only one collecting more than *anything* it
  advertises.
- **Eval (live corpus, 161 payees with both):** 95% settle at ≤ 2.5× their max-advertised
  price. The tail (≥10×) is sellers listing ~$0.001 and collecting $0.05–$0.09. Gate
  (**HOLD**) at **10×** — the 5–6× band is ambiguous with the temporal confound below,
  10×+ is unambiguous. At 10× only ~5/292 payees gate.
- **HOLD-only, never STOP, fail-open.** Folded via `blackwall.decide_payment`'s
  `divergence_ratio` (`DIVERGENCE_HOLD_RATIO=10`) + `forecast`'s `divergence_index`
  (a `{payee: ratio}` watch-list precomputed by `price_integrity.build_divergence_index`,
  loaded from `BLACKWALL_DIVERGENCE_INDEX`, baked into the free image).

### Known limits (audit)
- **Temporal confound.** Advertised price is a CURRENT snapshot; settled amounts are a
  HISTORICAL window — a seller that legitimately RAISED its price after listing looks the
  same. The 10× bar + HOLD-only (reviewable) keep it a conservative escalation.
- **Coarse (payee-level).** The store's `price_history` carries no resource tag, so a
  settled amount can't be matched to a specific advertised endpoint; the signal compares
  the payee's settled median to its overall advertised max.
