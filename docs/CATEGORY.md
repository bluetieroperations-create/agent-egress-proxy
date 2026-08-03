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
item (see `docs/HANDOFF.md` §4): the classifier lives on the Blackwall branch but the
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

**Do it right, or not at all:**
- **Use ON-CHAIN settled amounts, not advertised prices.** The table above is
  *advertised* prices (seller-controlled, gameable). The robust baseline is what payees
  in a category *actually collected* on-chain (we have this via `chain_backfill`), which
  a seller can't inflate by editing a Bazaar listing.
- **Advisory / HOLD-only, never STOP.** Category is fuzzy (~⅓ `other`) and derived; a
  wrong category → wrong baseline. It must only ever *escalate to review*, never block.
- **Fail-open + big margins.** Flag order-of-magnitude outliers (≥10×/≤0.1× the
  category median), not small deviations — categories are broad and noisy.

**Status:** proposed. It's a genuine new signal (validated above), but wiring a new
input into the verdict needs its own audit → eval → verify pass before shipping.
