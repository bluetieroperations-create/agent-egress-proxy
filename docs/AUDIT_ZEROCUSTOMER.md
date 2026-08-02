# Audit — "feed Blackwall with zero customers" pipeline

_Adversarial audit → eval → live verify of the cold-start data pipeline:_
`discovery_crawl.py` → `chain_backfill.py` → `ecosystem_scan.py`, plus the shared
seams (`settlement_watch.extract_usdc_transfers`, `reputation_store.ingest_transfers`
/`lookup`, `addresses`). Run 2026-08-02. Method: an independent adversarial pass on
the seams + targeted probes + a live re-scan against the CDP Bazaar and on-chain USDC.

## What held (verified)
- **Address join** is consistent lowercase end-to-end (crawl `payTo` → backfill →
  `extract_usdc_transfers` `to`/`from` → `ingest` `counterparty` → `lookup`). A
  crawled payee joins to its own on-chain history even across checksummed/lowercase.
- **Idempotent re-ingest** (`INSERT OR IGNORE` on `UNIQUE(tx_hash,counterparty,amount)`):
  re-running backfill ingests 0 the second time.
- **USDC-by-contract** identification drops symbol-spoof "USDC" lookalikes.
- **Non-advancing pager** is bounded by `max_pages` (no infinite loop).

## Findings

| # | Sev | Finding | Direction | Status |
|---|-----|---------|-----------|--------|
| H1 | High | `resource_count = len(accepts)` counted each payment **option** as a distinct **resource**; inflated "resources", "multi-resource %", and price weight | over-counts | **Fixed** — `resource_count` = distinct URLs, `option_count` = accepts; stats add `payment_options` |
| H2 | High | Price layer divided **every** asset by 10⁶ (USDC-6dp), so an 18-dp token read ~10¹²× too large; negatives/zero passed through | garbage/injectable numbers | **Fixed** — USD only for known 6-dp USDC assets (`USDC_6DP`); non-USDC counted separately; non-positive excluded |
| M2 | Med | `COUNT(DISTINCT payer)` counted a blank sender as a phantom distinct payer → could clear the thin/Sybil gate | **unsafe** (unearned trust) | **Fixed** — `COUNT(DISTINCT NULLIF(payer,''))` |
| M4 | Med | `backfill` had no try/except around the live pager; one 429/timeout aborted the whole scan | availability | **Fixed** — per-payee fail-soft, `errors` count in summary |
| M5 | Med | Percentile index `int(q*n)` returned the **max** at p90 for small n → p50/p90 overstated | over-states | **Fixed** — nearest-rank on positions `0..n-1` |
| M6 | Med | BD `candidates.csv` wrote untrusted `resource` URLs raw → CSV/formula injection in Excel/Sheets | exploit | **Fixed** — `_csv_safe` prefixes `= + - @` / control leads |
| M3 | Med | `fetched` double-counted on an overlapping/non-advancing pager (store stayed correct via dedup key) | misleading metric | **Fixed** — `payee_transfers` dedupes byte-identical rows, keeps distinct-sender same-tx transfers |
| M8 | Med | CLI `--payee` / `--payees-file` not deduped/case-normalized → same address fetched & counted twice | double-count | **Fixed** — `backfill` dedupes by lowercased address |
| M7 | Med | `_load_sanctioned` fails open silently → `sanctioned: 0` reads as "clean" when screening was skipped | misleading | **Fixed** — returns `(list, ok)`; `main` warns on stderr when unavailable |
| M9 | Med | `crawl_bazaar` broke silently on a page error → partial snapshot presented as complete | silent truncation | **Fixed** — stderr warning naming offset/pages/error |
| L1 | Low | No-history endpoints got a positive `trust_score` from attacker-controllable advertised breadth | rank inflation | **Fixed** — `distinct_payers is None` → score 0 |
| **M1** | Med | `UNIQUE(tx_hash,counterparty,amount)` collapses two real USDC Transfer events in **one** tx (disperse/multicall) from different senders → under-counts settlements and can drop a distinct payer | **conservative** (only under-counts → safe direction) | **Deferred** — fix needs `log_index` in the natural key, a **shared-schema** change touching `settlement_watch`/`reputation_store` used by the Traceipt project. Documented; safe because it can only make the gate *more* cautious. |
| L2 | Low | Transfer with missing `tx_hash` dropped (dedup key needs it) | under-count | Accept (by design) |
| L3 | Low | Zero-price/free resources excluded from the price distribution | under-count | Accept (by design) |
| L4 | Low | Discovery docs nested deeper than `_MAX_DEPTH=6` truncated | under-count | Accept (DoS guard) |
| L5 | Low | No per-page cap on `accepts`/`items` array size | memory | Accept (upstream page_limit bounds the default source) |

## The headline correction (H2)
The first published "State of x402" brief claimed two endpoints advertised
**$10T / $10B** prices — flagged as a live "price anomaly." That was **our own
decimals bug**, not a real anomaly: those prices are denominated in **18-decimal
tokens** (a Polygon token; USDT/USDC on BNB Chain are 18-dp there), and dividing an
18-dp amount by 10⁶ inflates it ~10¹²×. Corrected reality:

- Real USDC price range: **$0.0001 – $1,000** (p50 $0.01, p90 $0.10). The $1,000 top
  is Bitrefill's gift-card tier — a real merchant.
- The genuine hazard is sharper and true: **108 of 940 priced options (~12%)** are in
  a non-USDC / different-decimal asset. An agent assuming "6-dp USDC" misreads them by
  up to 12 orders of magnitude. `ecosystem_scan` now renders USD only for confirmed
  6-dp USDC and flags the rest.

## Live verification (all fixes in place)
```
crawl_all(max_pages=6) -> 148 endpoints, 602 distinct resources, 940 options, 18 chains
backfill top-25 (2 pages each) -> errors=0, sanctions screened=True
price_usdc: min $0.0001  p50 $0.01  p90 $0.10  max $1000  (635 distinct 6-dp-USDC prices)
non_usdc_priced_options: 108
directory top: bitrefill (85 distinct payers) > loyalspark (44) > aidress.ai (42) > anchor-x402 (41)
```

## Follow-up hardening (live path robustness, not bugs)
The M4 fix made `backfill` fail-soft — but "soft" meant a transient 429/timeout
_skipped_ a payee, silently leaving its reputation missing. Added `http_util.get_json`:
- **Retry with exponential backoff** on transient 429/5xx/timeout/connection-reset
  (honors `Retry-After`; permanent 4xx never retried) — a rate-limited public node
  no longer drops a payee's history; the pager recovers instead of skipping.
- **Read-size cap** — `urlopen().read()` was unbounded; an oversize/hostile response
  is now a hard `ResponseTooLarge`, not a memory balloon.

Both live fetchers (`chain_backfill.BlockscoutPager`, `discovery_crawl`) route
through it. Transport + clock are injectable, so the retry ladder is unit-tested
with no network and no real sleeping (12 tests). Live re-verified: Bazaar crawl +
Blockscout backfill succeed through the helper, 0 errors.

## Regression tests added (25)
- `test_ecosystem_scan`: resource≠option counting; non-USDC/negative/zero price excluded;
  USDC survives amid non-USDC; percentile ≠ max; no-history score 0; CSV-safe helper.
- `test_chain_backfill`: fetched deduped on non-advancing pager; same-tx different-sender
  transfers kept; fail-soft on transport error; repeated-payee dedupe.
- `test_reputation_store`: blank payer not counted distinct.

Full suite: **682 passing** (was 669).
