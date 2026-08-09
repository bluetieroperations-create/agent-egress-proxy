# Data-source spike — can Blackwall get counterparty history fast & cheap enough?

**Question (the load-bearing one, HANDOFF §4 / spec step 2):** the entire
Blackwall value thesis rests on a *stateful, compounding* counterparty-reputation
signal a stateless facilitator can't replicate. Before building on it: can that
history actually be fetched **fast and cheap enough for a synchronous,
pre-signature, per-call check**?

**Verdict: partly — and the gaps decide the architecture.** The data is
reachable with no API key, but a free public indexer is **too slow and too
variable** for the hot path, and the *moat* signal (disputes/underdelivery) is
**not on-chain at all**. Both findings point the same way: Blackwall needs its
**own indexed store + observed-outcome ledger**, not a passthrough to someone
else's indexer.

## What was tested

- Source: **Blockscout for Base** (`https://base.blockscout.com`), no key.
- Client: **stdlib `urllib`** only (matches the repo), through the session's
  egress proxy. (403 from the proxy was an origin-side User-Agent filter, not a
  policy block — fixed with a real UA; curl confirmed the host is allowed.)
- Code: `reputation_onchain.py` (the adapter, drop-in for `MockReputationSource`).
  The live latency/feasibility harness was a one-off probe and has since been
  removed; its measured findings are recorded below.

## Evidence (measured this session)

| Call | Purpose | Latency | Notes |
|------|---------|---------|-------|
| `/addresses/{a}/counters` | tx + token-transfer volume | **~1.0–1.4 s** | reliable, cheap, gives volume signal |
| `/addresses/{a}/token-transfers?type=ERC-20&filter=to` | recent inbound transfers | **~8–14 s** for active addresses | large payloads; **highly variable** |
| `…/token-transfers?token={USDC}&filter=to` | USDC-only history (the one we actually want) | **timeout > 20 s** | unusable on the free indexer |
| warm cache hit | repeat lookup | **< 1 ms** | the only thing that makes a hot path viable |

End-to-end the real record **drops into `blackwall.forecast()` unchanged** (same
`lookup()` seam) — so the integration shape is proven even though the source is
not yet hot-path-grade.

## Findings

1. **Reachable & parseable: YES, no key.** Volume, typical-amount, and
   counterparty are real, derivable on-chain signals.
2. **Latency: NO, not from a free indexer.** 1 s best case, 8–14 s typical for
   active addresses, **>20 s timeout** for the token-filtered query we most
   want. A synchronous per-signature check cannot depend on this. Mitigations
   that *are* viable: a warm cache (sub-ms, implemented) + **x402 session reuse**
   (pay once, many checks) exactly as the spec anticipated — but cold-miss
   latency still forces a **self-indexed store** for production (ingest Base
   transfers into Blackwall's own DB, or a paid indexer with proper indexes:
   Goldsky / Dune / Alchemy).
3. **The moat signal is NOT on-chain.** Settlement *volume* is visible;
   **dispute / underdelivery rate is not** — "did the counterparty actually
   deliver what was paid for" is an off-chain fact. The adapter reports
   `dispute_rate = None` (UNAVAILABLE), never a fabricated 0. **Implication:**
   on-chain-only reputation degrades to "volume + age," which a facilitator
   could approximate — so it is *not* a durable moat by itself. The defensible
   asset is **Blackwall's own accumulated outcome ledger** (every GO it issued,
   and whether that payment was later disputed/refunded/underdelivered). That
   compounds with usage and nobody else has it.

## What the code now reflects

- `OnchainReputationSource`: `/counters` required (fast); transfers fetched
  **best-effort** with a short timeout — on slow/timeout it **degrades to a
  volume-only, HOLD-leaning record** rather than blocking. TTL cache in front.
- `_meta.data_completeness` marks every signal `real` / `UNAVAILABLE-on-chain` /
  `needs-extra-query` so a verdict never silently treats missing data as good.
- `derive_record` is pure and unit-tested offline (`test_reputation_onchain.py`).

## Recommended decision

**Do not** build the hot path on a passthrough to a public indexer. Two
concrete next gates, in order:

1. **Own outcome ledger (the moat).** Record every verdict + the eventual
   settlement outcome. This is the dispute signal nobody else has, and it's a
   write-path Blackwall fully controls — start it now, it only gets valuable
   with age.
2. **Indexed read store for volume/age/typical-amount.** Either ingest Base
   USDC `Transfer` events into Blackwall's own DB, or budget a paid indexer.
   Target < 100 ms p95 for the cached/indexed lookup; treat the public indexer
   as a backfill/bootstrap source only.

Until both exist, the honest product is the **single-hop safety + price-anomaly
check with a self-built reputation ledger warming up** — not a claim of
on-chain-derived reputation depth.

## Correction (found later, during the settlement-watcher audit)

The original run reported `usdc_inbound = 0` for the sample addresses and
attributed it to unrepresentative wallets. That was **partly a bug**: Blockscout
v2 carries the token contract under `token.address_hash`, but the extractor read
`token.address` (always null) and dropped every USDC transfer. Fixed (read
`address_hash`, identify USDC by contract not the spoofable `symbol`). Re-checked
against a real USDC-receiving address: **26 inbound USDC settlements with real
price history**. The latency findings above stand; the signal-completeness one is
*better* than first reported — USDC volume/price IS extractable once the field is
right. (Lesson logged in CLAUDE.md: verify against the live path, not just green
unit tests that may encode the wrong API shape.)

## Reproduce

```sh
python -m unittest test_reputation_onchain.py -v   # offline derivation tests
```
