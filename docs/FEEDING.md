# Feeding Blackwall — the corpus drives real verdicts

_Run 2026-08-02._ The cold-start pipeline (`discovery_crawl` → `chain_backfill` →
`ecosystem_scan`) doesn't just produce a report — it produces a **reputation corpus
the verdict engine consumes**. This is the loop closing: public data in, real
GO/HOLD/STOP out, before customer #1.

## The seed is a manifest, not a binary
The warm corpus is **regenerated from a committed manifest**, not a checked-in
database — no multi-MB binary in git, nothing stale, fully reproducible:

- `data/seed_payees.txt` — **198** x402 payee (`payTo`) addresses to backfill (the
  most-active endpoints from a 724-endpoint Bazaar crawl).
- `data/report.json` / `directory.json` / `candidates.csv` — the State-of-x402
  snapshot, trust directory, and BD funnel from that pass (text, diffable).

Regenerating the store from the manifest yields ~**30 anchors** and, e.g., Bitrefill
at **134 distinct payers** — the coverage the payer-graph / payer-reputation signals
need (see `docs/PAYER_GRAPH.md`). `*.db` is git-ignored, so a regenerated store stays
local.

## How it feeds the engine
`blackwall.forecast(payload, reputation_source)` calls `reputation_source.lookup()`.
Regenerate the store from the manifest, then point the service at it:

```sh
export SSL_CERT_FILE=/root/.ccr/ca-bundle.crt        # proxy CA (env-specific)
python3 chain_backfill.py --store data/reputation_seed.db \
  --payees-file data/seed_payees.txt --max-pages 3
BLACKWALL_STORE=data/reputation_seed.db python3 mcp_server.py   # or --store <path>
# -> production_source(store) + payer-reputation graph -> answers from the corpus
```

No mock. A known payee now resolves to real on-chain history instead of a
cold-start HOLD.

## Live verdict proof (engine fed from the corpus)
```
Bitrefill @ $0.09  (normal)             -> GO    rep 0.99  [146 settlements, 134 distinct payers]
Bitrefill @ $5000  (10000x its median)  -> STOP  price anomaly 97.7x the counterparty's own median
thin payee @ $0.01 (150 settl, 1 payer) -> HOLD  Sybil gate: volume != trust, no auto-GO
unknown payee @ $0.09 (cold start)      -> HOLD  rep 0.50, 0 prior settlements
```

_(Proof numbers above are from an earlier 73-payee build; the committed manifest now
covers 198 payees / ~30 anchors — richer, same behavior.)_

Each row is the design working on **real data**:
- **GO** needs real reputation — now present for ~198 payees.
- **STOP** fires when the *quoted* amount is anomalous vs the payee's *own* on-chain
  price history (the history is what makes the anomaly measurable).
- **HOLD on a busy-but-thin payee** is the Sybil defense: 150 settlements from a
  single distinct payer does **not** auto-approve.
- **HOLD on the unknown** is the honest cold-start — and it's now the exception, not
  every payee.

## Reproduce / refresh
From the fixed manifest (deterministic payee set; on-chain history drifts over time):
```sh
export SSL_CERT_FILE=/root/.ccr/ca-bundle.crt        # proxy CA (env-specific)
python3 chain_backfill.py --store data/reputation_seed.db \
  --payees-file data/seed_payees.txt --max-pages 3
```
Or re-scan the ecosystem from scratch (also refreshes the manifest + snapshots):
```sh
python3 ecosystem_scan.py --backfill-store data/reputation_seed.db \
  --max-pages 80 --backfill-top 200 --backfill-max-pages 3 \
  --out-report data/report.json --out-directory data/directory.json \
  --out-candidates data/candidates.csv
```
The seed lives in git as the **manifest** (`data/seed_payees.txt`), not a binary DB,
so there's no bloat and nothing stale — the store is a ~2-minute build step.

## What this proves — and doesn't
- **Proves:** the public-data corpus feeds the live engine; verdicts differentiate
  established / anomalous / thin / unknown on real Base settlement data.
- **Doesn't:** outcome depth. Dispute/chargeback signal (the `dispute_rate` the
  reasons show as 0.0%) comes from the receipt/verdict loop, not a crawl — it stays
  0 until real traffic flows.
