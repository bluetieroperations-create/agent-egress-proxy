# Blackwall — roadmap

Deferred work, captured so it isn't lost. Nothing here is built yet; each item
notes *why it's deferred* and the *caveat that matters*. Ordered loosely by
leverage, not commitment.

## Shipped (for context)
Verdict engine (GO/HOLD/STOP) · behavioral counterparty reputation ·
wash-trade-resistant price-anomaly · OFAC sanctions screening · self-owned
endpoint-readiness · value-aligned pricing · x402 billing (EIP-3009 / facilitator
seam) · MCP stdio server · service-discovery descriptor · **deployed live on Base
mainnet** · **real mainnet USDC settlement driven end-to-end** (paid x402 path:
402 → EIP-3009 sign → facilitator verify+settle → verdict; caught a 100× price
gouge on live ingested reputation) · listed on awesome-x402 · adversarially
audited (315 tests).

---

## Signal depth

### Peer-group price cross-check  — **engine shipped; self-populating index deferred**
Compare a counterparty's median not just to its *own* history (per-class, done) but
to a **peer-group median** — what comparable services charge for the same resource
class. Catches a counterparty that's an outlier vs peers even if its own history
looks self-consistent. **The engine is built** (`peer_group_median`,
`build_peer_class_index`, `peer_anomaly_ratio`, verdict integration — HOLD-only,
Sybil-bounded; opt-in via a `peer_index` + `resource_class`). See `docs/MARKETPLACE.md`.
- **Deferred half:** auto-building/refreshing the index from Blackwall's own ledger
  — needs `resource_class` recorded on verdicts, a cross-counterparty
  class-observation export, and a periodic rebuild (the rolling-aggregate infra
  below). Until then, inject a `peer_index` from external market data.
- **Caveat:** peer grouping is the hard part (what counts as "comparable"?); a bad
  grouping is worse than none. `resource_class` must be a shared taxonomy.

### Self-owned readiness calibration
`LocalReadinessSource` detects a 402 via **GET**, so a POST-only x402 endpoint
with no manifest can score a false `needs_work`.
- **Why deferred:** needs calibration against a corpus of real live endpoints.
- **Caveat:** conservative-only (it can only *add* caution), so it's a quality
  issue, not a safety bug.

### ERC-8004 interop  *(consume, don't compete)*
Read on-chain agent identity + reputation from the **ERC-8004** registry standard
as an *input* signal (the shared rail others build on).
- **Why deferred:** standard is young; wait for adoption before wiring it in.

---

## Trust & verifiability

### On-chain verdict attestations (EAS)  ⚠️ proofs only — not the data
Publish **verdict receipts / settlement confirmations** as on-chain attestations
(Ethereum Attestation Service on Base, the pattern Ontario uses) so verdicts are
*publicly verifiable and composable* — "Blackwall attested this settlement."
- **Why deferred:** real build — on-chain writes (gas, an EAS schema, an
  attestation signing key).
- **CAVEAT (load-bearing):** attest the **PROOF** (a verdict was issued, a
  settlement confirmed), **never the reputation corpus.** The moat is that the
  accumulated counterparty history is *private*; publishing the dataset to public
  EAS hands it to competitors for free. Proofs add credibility; data dumps destroy
  the moat. Do not conflate the two.

---

## Distribution

### Framework middleware plugins
Drop-in wrappers for **LangChain / CrewAI / Vercel AI SDK** — "call Blackwall
before you sign." Captures developers at build time.
- **Why deferred:** lower leverage until there's pull; one thin plugin first, not
  all three.

### Listing follow-ups
- ~~Update the awesome-x402 entry to "Live on Base".~~ **Done.**
- Submit to additional registries (Smithery / Glama MCP) once the MCP-over-HTTP
  transport exists (below) — they want a reachable MCP endpoint, not stdio.

---

## Infra & scale  *(post-traffic — don't pre-build)*

### Mainnet persistence + rolling aggregate
Move the store/ledger onto a **persistent disk** ($7/mo tier) so the data flywheel
survives restarts; add a rolling reputation aggregate + bounded history/nonce
eviction so memory/state stays flat under load.
- **Why deferred:** there's no traffic yet. Pay for persistence when there's data
  worth keeping (watch Render logs for `/v1/forecast-payment` + the payTo wallet).

### MCP-over-HTTP
The MCP server is **stdio/local-only**; an HTTP transport lets *remote* agents and
MCP registries use it.
- **Why deferred:** post-traffic; the HTTP verdict API + discovery cover discovery
  today.
