# Feeding Blackwall — the corpus drives real verdicts

_Run 2026-08-02._ The cold-start pipeline (`discovery_crawl` → `chain_backfill` →
`ecosystem_scan`) doesn't just produce a report — it produces a **reputation corpus
the verdict engine consumes**. This is the loop closing: public data in, real
GO/HOLD/STOP out, before customer #1.

## The corpus
`ecosystem_scan.py --backfill-store data/reputation_seed.db --max-pages 30
--backfill-top 75 --backfill-max-pages 3` built:

- **9,463** real on-chain USDC settlement rows
- across **73 payees** (the most-active endpoints in a 458-endpoint Bazaar crawl)
- e.g. Bitrefill: **146 settlements, 134 distinct payers**

## How it feeds the engine
`blackwall.forecast(payload, reputation_source)` calls `reputation_source.lookup()`.
The MCP/HTTP service takes a store path and builds that source:

```sh
BLACKWALL_STORE=data/reputation_seed.db python3 mcp_server.py     # or --store <path>
# -> production_source(store) -> the engine answers from the corpus
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

Each row is the design working on **real data**:
- **GO** needs real reputation — now present for 73 payees.
- **STOP** fires when the *quoted* amount is anomalous vs the payee's *own* on-chain
  price history (the history is what makes the anomaly measurable).
- **HOLD on a busy-but-thin payee** is the Sybil defense: 150 settlements from a
  single distinct payer does **not** auto-approve.
- **HOLD on the unknown** is the honest cold-start — and it's now the exception, not
  every payee.

## Reproduce
```sh
export SSL_CERT_FILE=/root/.ccr/ca-bundle.crt        # proxy CA (env-specific)
python3 ecosystem_scan.py --backfill-store data/reputation_seed.db \
  --max-pages 30 --backfill-top 75 --backfill-max-pages 3 \
  --out-report data/report.json --out-directory data/directory.json \
  --out-candidates data/candidates.csv
```
`data/reputation_seed.db` is a committed **seed** so the engine boots warm out of the
box; it is regenerable by the command above and will drift as on-chain history grows.

## What this proves — and doesn't
- **Proves:** the public-data corpus feeds the live engine; verdicts differentiate
  established / anomalous / thin / unknown on real Base settlement data.
- **Doesn't:** outcome depth. Dispute/chargeback signal (the `dispute_rate` the
  reasons show as 0.0%) comes from the receipt/verdict loop, not a crawl — it stays
  0 until real traffic flows.
