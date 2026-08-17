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

### RamsReadinessSource — ERC-8226 authorization axis  *(BUILT — dormant-but-ready)*
**SHIPPED** as `rams_readiness.py`: reads `canExecute` from a mandate registry and folds
via the shared `apply_rwa_readiness`, wired into `CombinedRwaReadinessSource` (idle) when
the EVM RPC is set. It stays a NO-OP until a request advertises `acquires.mandate_registry`
(or `BLACKWALL_RAMS_REGISTRY` is set), then activates with zero code change — so the day an
asset ships a RAMS hook it just starts firing. Remaining once mainnet RAMS exists: map the
finalized `ExecutionReason` enum to names (reported generically as "reason code N" today),
and thread agent/principal identity from an ERC-8004 source instead of `payer` +
`acquires.principal`. Original design note below.

### RamsReadinessSource — ERC-8226 authorization axis  *(consume, don't compete)*
A thin, opt-in, fail-open readiness source that adds the **agent-AUTHORIZATION**
revert-predicate to the tokenized-RWA gate (`rwa_readiness.py`, shipped). ERC-8226
"RAMS" (Regulated Agent Mandate Standard, Brickken) is an on-chain **agent-mandate
registry**: a signed, time-bounded, amount-capped, revocable mandate keyed by
`(agent, principal)` that a regulated token validates at transfer via
`canExecute(agent, principal, asset, action, amount) → (bool, ExecutionReason)`.
Read that view **the same way** `rwa_readiness` already reads
`detectTransferRestriction`/`preTransferCheck` — a **second** pre-transfer revert
surface on agent-delegated transfers (`OVER_TX_CAP` / `AGENT_FROZEN` / `REVOKED` /
expired / `NOT_ACTIVE`). Fold as a `blocked`/`unknown` grade through the existing
`apply_rwa_readiness` (HOLD-only, never STOP). Fire **only** when the target asset
advertises an `IAgentMandate` registry.
- **Why it's a COMPLEMENT, not a competitor:** RAMS is the *authorization* axis
  ("did the principal permit this agent, within caps?") and **explicitly does NOT
  check KYC/whitelist/transfer restrictions** — it leaves receiver eligibility to
  the token's own ERC-7943/ERC-3643 hook, running in parallel. It sits strictly
  *above* our eligibility axis on a different revert cause. So it can only *add* a
  revert-predicate we'd otherwise miss; it can't replace or block ours.
- **Why deferred:** Draft ERC (filed 2026-04-12; reference impl merged 2026-06-29),
  only a **Sepolia** deployment, one announced-not-shipped partner (Taiko),
  single-vendor champion (Brickken, who also authors the ERC-7943 substrate RAMS
  rides). Real assets exposing a queryable RAMS registry are ~zero today.
- **Build trigger (load-bearing):** a **mainnet** tokenized asset ships a RAMS hook
  / advertises an `IAgentMandate` registry. Until then the read has nothing to hit.
  Track the `ethereum/ERCs` PR cadence + the diagnostic-enum finalization.
- **Caveat:** the mandate's `canExecute` needs `(agent, principal, action, amount)`
  — richer inputs than our receiver-only reads. Needs the request to carry the
  agent/principal identity (ties into ERC-8004 interop above) and the action
  selector; degrade fail-open (`unknown`) when they're absent. See
  `docs/TOKENIZED_RWA.md` (Adjacent: RAMS) and `COMPETITIVE.md`.

### Tokenized-stock data path  — **discovery + Solana readers shipped; enrichment fold deferred**
The "Blockscout-for-Base but for tokenized stocks" plumbing. **Shipped:**
`tokenized_stock_registry.py` (recognize a token contract -> issuer/underlying/ISIN;
keyless ingest from the Backed/xStocks `/public/assets` feed + an operator seed for
gated issuers) and `solana_rwa.py` (the SPL **Token-2022** restriction leg -- mint
extensions + account frozen state -> the same readiness signal, folded via the shared
`apply_rwa_readiness`; wired through `CombinedRwaReadinessSource`); the **registry
enrichment fold** (`apply_asset_registry` -> `signals.rwa_asset` + trading-halt HOLD);
**pagination** (full 617-ticker / 6k-deployment ingest); **peg/NAV divergence**
(`pyth_price.py` -- keyless Pyth underlying price vs per-unit paid, overpay HOLD); the
**accumulation corpus** (`rwa_ledger.py` -- every RWA buy + context logged, keyed by
receipt_id); and the **outcome-capture loop** (`rwa_outcomes.py` -- T+N mark-to-market via
Pyth labels each buy underwater/in-profit, closing the flywheel; `issuer_trust` grades the
rollup). **Deferred halves:**
- **Graduate `issuer_trust` to a GATE** — it's descriptive today; once labeled outcomes
  accrue and the grade is calibration-locked (like the sybil_ring graduation), fold it as
  a conservative verdict input (earned floor, like `seller_audit`; HOLD-only, never STOP).
  - **TRIGGER (self-signaling, checkable):** `rwa_report.py`'s `issuer_directory` IS the
    readiness signal. Run it on the live corpus; when several issuers have graduated OUT
    of `trust: "insufficient"` (i.e. `>= ISSUER_TRUST_MIN_OUTCOMES` LABELED outcomes each,
    from `capture_outcomes` cron runs), there's enough data to calibrate. Until then the
    directory shows "insufficient" across the board — that's the honest "not yet" light.
  - **Calibration when triggered:** mirror `calibration_lock.py` / the sybil_ring
    graduation — pin the grade→floor mapping against the accrued corpus, add a
    reversibility lock (`ISSUER_TRUST_GATES=False` default), fold HOLD-only via
    `decide_payment` (like `verified_floor`), and prove the false-flag rate on known-good
    issuers (Backed/Ondo) is ~0 before flipping the lock on.
- ~~**On-chain settlement-held reader**~~ — **BUILT** (`rwa_balance.py`).
- ~~**Definitive `settled` label**~~ — **BUILT**: `forecast` snapshots the payer's pre-buy
  balance (opt-in, via `balance_reader.balance_of`, one `balanceOf` on the RWA hot path);
  the outcome loop reads the post-buy balance and records `settled = post > pre` (the
  security actually ARRIVED), preferred over the `holds_balance` heuristic in every rollup.
  - **Residual caveat (true `settled` still imperfect):** the delta can false-NEGATIVE if
    the agent received then moved the token before T+N, and is ambiguous if other buys of
    the same token landed in the window. The tx-hash of the settlement (or a tighter
    before/after around the exact tx) is the only fully-sound attribution -- deferred.
- ~~**Backing / proof-of-reserves + authoritative Pyth feed map**~~ — **BUILT**
  (`backed_oracle.py`): keyless Backed `/public/proof-of-reserves` -> `backing_ratio`
  (shares held vs tokens circulating); under-collateralized -> HOLD. `/public/oracles` ->
  the issuer-declared Pyth feed map that hardens the peg gate. Folded via `backing_index`.
- ~~**True token-market peg**~~ — **BUILT** (`dex_price.py`): reads the token's live
  Uniswap-v3 pool price (`slot0().sqrtPriceX96`, verified live vs the USDC/WETH pool),
  compares to the underlying (Pyth); >10% off NAV -> HOLD. Discovers the pool via the v3
  factory across fee tiers (or `acquires.dex_pool`). Opt-in `BLACKWALL_DEX`, HOLD-only,
  fail-open. RESIDUAL: no liquidity-depth check -> a dust pool can false-flag (bounded --
  HOLD-only, and it can only ADD caution). Next refinement: a pool USDC-balance floor to
  skip dust, and pick the deepest fee tier rather than first-found.
- **Gated-issuer seed** — PARTIALLY DONE: 9 **Ondo Ethereum** tokenized stocks
  (AAPLon/TSLAon/NVDAon/MSFTon/GOOGLon/AMZNon/METAon/SPYon/QQQon) seeded into `STATIC_SEED`,
  each doubly-verified 2026-08-17 (Ondo's `docs.ondo.finance/addresses.md` + an independent
  on-chain `symbol()`/`name()` read). STILL EXCLUDED (couldn't meet the two-source bar):
  Ondo BNB/Solana (issuer CSV only, explorers bot-blocked), Robinhood Chain (official
  Blockscout + QuickNode but issuer registry not renderable; note: heavy on-chain
  ticker-spoofing there -- verify hard), and Dinari dShares (no static per-ticker issuer
  source; production addresses live only behind their `DShareFactory`/authenticated API).
  Next: enumerate Ondo's other chains + Dinari's production `DShareFactory.getDShares()`
  and confirm each on an explorer before adding.
- **Solana ATA auto-derivation** — the per-wallet frozen read currently needs
  `acquires.receiver_token_account`; auto-deriving the associated-token-account PDA
  (off-curve check) would make the Solana leg fully receiver-specific like the EVM one.
  Reuse the Ed25519 primitives in `cdp_auth.py` for the on-curve test.
- **Solana on-chain reputation** — clone the `blockscout.py` enrichment pattern onto
  Solana (public RPC / Solscan free tier) for holder/transfer history.
- **Caveat:** no FREE canonical cross-issuer registry exists (rwa.xyz is paid/Enterprise);
  discovery is self-assembled from issuer feeds. NEVER hard-code an unverified address —
  a wrong one mis-identifies a token. See `docs/TOKENIZED_RWA.md`.

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
