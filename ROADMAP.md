# Blackwall — roadmap

Deferred work, captured so it isn't lost. Each item notes *why it's deferred* and
the *caveat that matters*. Ordered loosely by leverage, not commitment.

Some entries are now marked **SHIPPED** or **BUILT — dormant-but-ready** rather
than deleted, so the reasoning behind them survives.

## Shipped (for context)
Verdict engine (GO/HOLD/STOP) · behavioral counterparty reputation ·
wash-trade-resistant price-anomaly · OFAC sanctions screening · self-owned
endpoint-readiness · value-aligned pricing · x402 billing (EIP-3009 / facilitator
seam) · MCP stdio server **and Streamable-HTTP transport** ·
service-discovery descriptor · **deployed live on Base mainnet** · **real mainnet
USDC settlement driven end-to-end** (paid x402 path: 402 → EIP-3009 sign →
facilitator verify+settle → verdict; caught a 100× price gouge on live ingested
reputation) · listed on awesome-x402 · independently-verifiable Ed25519 receipts ·
**one x402 challenge parser** covering both carriers (JSON body and
`WWW-Authenticate: X402`) · **payer-side scoring** (`POST /v1/screen-payer`) ·
**public price index** (`GET /v1/price-index`) · CORS · adversarially audited
(1,609 tests; redteam 25 caught / 2 known gaps / 0 false positives).

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

### Self-owned readiness calibration  — **UNBLOCKED; the fix is known**
`LocalReadinessSource` detects a 402 via **GET**, so a POST-only x402 endpoint
with no manifest can score a false `needs_work`.
- **No longer deferred for lack of data.** Both blockers are gone:
  - The corpus exists — `data/liveness.json`, 195 real hosts classified.
  - The same GET-only bug was hit and fixed in `directory_liveness.probe_host`,
    which retries with POST because **a 405 is a POST-only endpoint, not a dead
    one**. That retry recovered **14** hosts a GET-only sweep had written off.
    Port the same retry here.
- **Calibration target:** of 195 hosts, 71 `body_accepts` / 2 `hdr_accepts` /
  23 `wellknown` are the ones readiness should score as fine; the 86 `opaque_402`
  are the genuinely ambiguous set. See `docs/PAYABILITY.md`.
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
- **Graduate `issuer_trust` to a GATE** — **DESCRIPTIVE FOLD BUILT** (`issuer_trust_gate.py`):
  the grade is now precomputed from the corpus at startup (`IssuerTrustSource.from_ledger`,
  O(1) hot-path lookup) and folded into every RWA verdict as `signals.issuer_trust`, wired
  into `forecast` + the server (built whenever `BLACKWALL_RWA_LEDGER` is on). Behind the
  `ISSUER_TRUST_GATES=False` reversibility lock it is DESCRIPTIVE ONLY today (recorded, never
  affects the verdict) exactly like the sybil_ring observation phase. **REMAINING to flip the
  lock:** once labeled outcomes accrue and the grade is calibrated, flip `ISSUER_TRUST_GATES`
  so a LOW grade becomes an advisory signal `rwa_aggregate` weighs collectively (HOLD-only,
  never STOP; the wiring is already in `SIGNAL_SPECS`). The stronger earned-FLOOR direction
  (a HIGH grade CLEARING a cold-start HOLD, like `seller_audit`) stays deferred — clearing
  needs more calibration than adding caution.
  - **TRIGGER (self-signaling, checkable):** `rwa_report.py`'s `issuer_directory` IS the
    readiness signal. Run it on the live corpus; when several issuers have graduated OUT
    of `trust: "insufficient"` (i.e. `>= ISSUER_TRUST_MIN_OUTCOMES` LABELED outcomes each,
    from `capture_outcomes` cron runs), there's enough data to calibrate. Until then the
    directory shows "insufficient" across the board — that's the honest "not yet" light.
  - **DATA ACCELERANT (BUILT):** `rwa_backfill.py` seeds the corpus from PUBLIC on-chain
    history with ZERO customers -- an on-chain transfer that landed is a settled outcome,
    so a token's transfer history backfills real labeled settlements + distinct buyers per
    issuer. Verified live: 74 real TSLAon acquisitions / 33 buyers -> Ondo `medium` (from
    `insufficient`). Run it across the seed issuers and issuers become gradeable
    immediately -- no waiting for live traffic.
  - **Calibration when triggered:** mirror `calibration_lock.py` / the sybil_ring
    graduation — pin the grade→floor mapping against the accrued corpus, add a
    reversibility lock (`ISSUER_TRUST_GATES=False` default), fold HOLD-only via
    `decide_payment` (like `verified_floor`), and prove the false-flag rate on known-good
    issuers (Backed/Ondo) is ~0 before flipping the lock on.
  - **REMAINING for the grade to fully discriminate:** the backfill gives settlement +
    volume but NOT value-outcome (no price-paid on a bare transfer). The underwater/mark
    dimension needs the organic flywheel (real buys carry the paid price) or a paired-swap
    parser that recovers the USDC leg of a historical buy.
  - **SETTLEMENT-RELIABILITY AXIS — DORMANT-READY BUILT** (`revert_scan.py`): the backfill is
    survivorship-biased (only landed transfers → `settle_rate` always 1.0), so the missing
    denominator is FAILED attempts. Two live spikes settled feasibility: (1) failed txns to
    an RWA token ARE queryable and the per-tx detail endpoint returns a DECODED revert reason;
    but (2) the reverts on freely-transferable RWAs (Backed/Ondo today) are generic "exceeds
    balance" NOISE, not restriction reverts, and (3) their transfers carry NO paired USDC leg
    (0/16 sampled) so the underwater axis is also unfeedable here. So the axis isolates the
    RESTRICTION-CLASS revert (allowlist/KYC/frozen/paused), stays behind its own
    `REVERT_AXIS_GATES=False` lock, and is DORMANT until a permissioned issuer with real
    restriction reverts is ingested — verified live (8 real Backed reverts → all balance →
    axis dormant). Auto-activates with zero code change when the data appears (rams_readiness
    pattern). **REMAINING:** ingest a genuinely permissioned issuer (ERC-3643/T-REX,
    ERC-1404) whose transfers revert on restrictions, then calibrate + flip the lock.
  - **INGESTION + CALIBRATION PASS — RUN, measured:**
    - *Ingestion (where can the signal even exist?):* probed ALL **535** distinct corpus
      tokens × 9 interface probes (`identityRegistry` / `isWhitelisted` /
      `detectTransferRestriction` / `preTransferCheck` / `canTransfer` / `paused` /
      blocklist), with a `decimals()` CONTROL per token. Result: **535/535 alive,
      0/535 expose ANY permissioned interface.** So restriction reverts are *structurally
      impossible* on today's corpus — the axis is provably, not merely observedly, dormant.
      Sourcing a permissioned issuer is therefore a prerequisite, not an optimization.
    - *Calibration (is the classifier right on REAL strings?):* harvested ground-truth
      revert strings live by `eth_call`-simulating transfers FROM publicly OFAC/Circle-
      blacklisted addresses. `classify_revert` scores **4/4**: USDC's
      `"Blacklistable: account is blacklisted"` → `restriction` (true positive),
      `"ERC20: transfer amount exceeds balance"` → `balance` (true negative).
    - *Gap found + closed:* USDT's pre-0.8 blacklist reverts with **no reason string**
      (`invalid opcode: INVALID`). Added an explicit `opaque` class so reason-less
      compliance blocks are VISIBLE rather than buried in `other`. Opaque reverts
      UNDER-count restrictions (conservative/fail-safe — never over-flags an issuer) but
      cap recall on old-style tokens. All four real strings are now regression fixtures.
  - **PERMISSIONED ISSUERS SOURCED + INGESTED — and the axis is now ADJUDICATED.** Found
    real permissioned RWAs via a TRANSFER SIMULATION (eth_call `transfer` from a real holder
    to a fresh non-KYC address — decisive even when NO standard interface is exposed, which
    was true for 535/535 of the old corpus). Each VERIFIED on-chain by name()/symbol();
    unverifiable candidates were dropped, never seeded. Ingested 674 acquisitions across
    Ondo OUSG, BlackRock BUIDL, Matrixdock STBT, Hashnote USYC.
    - *Classifier recall was the first casualty:* on real permissioned reverts it caught
      only **1 of 3** — missing BUIDL's `"Wallet not in registry service"` and STBT's
      `"STBT: NO_RECEIVE_PERMISSION"` (the latter structurally, since `_` is a word char so
      a word-boundary match cannot see inside a SCREAMING_SNAKE identifier). Fixed with
      punctuation normalization + live-calibrated vocabulary; now **3/3**, still 0 false
      positives on the adversarial negation set. BUIDL's history added two more real
      strings: `"Under lock-up"` (restriction) and `"Not enough tokens"` (balance).
    - *The axis ACTIVATED:* BUIDL 20 restriction reverts (9.1% of attempts), STBT 7 (3.4%).
    - **VERDICT — `REVERT_AXIS_GATES` MUST STAY OFF, PERMANENTLY IN THIS HOME.** The measured
      counterfactual is that flipping it downgrades **BlackRock BUIDL to LOW** — one of the
      most reputable RWA issuers alive — *because* its lock-up and registry checks reject
      non-allowlisted wallets, i.e. because it is a properly permissioned security working
      AS DESIGNED. This empirically confirms the semantic trap flagged before the build:
      **a restriction revert measures TRANSFER FRICTION, not issuer untrustworthiness.**
      Pinned by `test_revert_axis_lock_stays_off_reputable_issuer_would_false_flag`.
    - **SIMULATION-BASED READINESS — BUILT** (`transfer_sim.py`): the interface probe's
    0/535 result is now moot. `SimulationReadinessSource` eth_calls the transfer to the
    agent's own wallet and reads the revert, with a CONTROL call so a sender-side failure
    is never blamed on the receiver. It emits the existing probe shape, so it folds through
    `apply_rwa_readiness` unchanged. Verified live: STBT -> blocked
    ("STBT: NO_RECEIVE_PERMISSION"), BUIDL -> blocked ("Wallet not in registry service"),
    freely-transferable control -> ready (GO preserved, no false positive).
  - **RE-HOMING is the real next step** (not flipping a lock): the signal is genuinely
      valuable to an agent buyer ("transfers here revert ~9% of the time — yours may too"),
      but it belongs beside `rwa_readiness` as an ASSET-level readiness signal — the
      realized, empirical counterpart to that module's proactive `canTransfer` probe.
  - **NEXT (unbuilt, promising):** since 0/535 of the ORIGINAL corpus tokens expose a
    restriction interface,
    `rwa_readiness` returns "unknown" for the entire corpus — but the blacklist simulation
    above shows an `eth_call` transfer SIMULATION yields a definitive pre-trade answer with
    NO interface required. A simulation-based readiness probe would cover the ~100% of
    tokens the interface probe misses.
- **THE LEARN STAGE IS DATA-STARVED, and no amount of backfilling fixes it (measured).**
  The corpus now holds 9,690 buys and 9,690 outcomes, but the outcome labels present are
  ONLY `settled` / `holds_balance` — **zero** `underwater`, **zero** `mark_ratio`. Every
  outcome came from backfill, where a landed transfer proves settlement but carries no
  price paid. So `settlement_success_rate` is 1.0 *by construction*, `issuer_trust` grades
  everything `medium`, and the grade cannot discriminate no matter how much history we
  ingest. This is why `ISSUER_TRUST_GATES` is still descriptive-only, and it is a
  DATA-AVAILABILITY fact, not a deferred implementation: the value-outcome dimension needs
  organic buys (real agents, real prices recorded at buy time) or a paired-swap parser that
  recovers the USDC leg of a historical acquisition. A live spike found 0/16 sampled RWA
  transfers carried a USDC leg in the same transaction, so the parser is not cheap either.
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
- ~~**True token-market peg + liquidity filter + QuoterV2**~~ — **BUILT** (`dex_price.py`):
  reads the token's live Uniswap-v3 price and compares to the underlying (Pyth); >10% off
  NAV -> HOLD. Discovers the DEEPEST pool (USDC-balance dust floor, env `BLACKWALL_DEX_MIN_LIQ`)
  and prefers **QuoterV2** `quoteExactInputSingle` for the EXECUTABLE, size-aware price +
  slippage (catches thin liquidity at the agent's trade size; verified live vs the USDC/WETH
  pool). Opt-in `BLACKWALL_DEX`, HOLD-only, fail-open.
- ~~**Holder-concentration rug-check**~~ — **BUILT** (`holder_concentration.py`): keyless
  Blockscout; a dominant non-contract holder -> HOLD (contract/issuer-custody excluded).
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
- ~~Update the `xpaysh/awesome-x402` entry to "Live on Base".~~ **Done** (PR #667).
- ~~Submit to additional registries once MCP-over-HTTP exists.~~ **This gate was
  never actually closed.** `mcp.blackwalltier.com` (the Cloudflare Worker proxying
  the engine) has been a reachable hosted MCP endpoint over HTTP for a while, and
  that is what the registries wanted. The `serve_http()` transport shipped in #5 is
  a DIFFERENT thing — it makes the Python server itself `mcp add`-able and
  self-hostable. Useful, but it was never the blocker. The underlying confusion is
  worth naming: x402 is HTTP by construction (the 402 status code) and the verdict
  API has always been HTTP; MCP is how an agent discovers and calls tools, and OUR
  MCP server was stdio-only. "MCP-over-HTTP" was about the second, and nothing
  about it ever gated x402.
- **Smithery: already listed** — verified 2026-08-30 at
  `smithery.ai/servers/@bluetier-operations/blackwall` (hosted as
  `blackwall--bluetier-operations.run.tools`; one tool, `forecast`; API key
  required). **Do NOT double-publish** a second listing for the x402 engine — that
  fragments the presence. See `docs/REGISTRIES.md` for the identity split and the
  canonical copy per surface.
- **Glama: status UNKNOWN, not "eligible" and not "listed".** Reported listed, but
  unverifiable from here — their API needs a key and the public search page is
  client-rendered, so an absent result proves nothing either way. Confirm before
  treating it as either an open task or a done one.
- `Merit-Systems/awesome-x402` — the second active index, and the one genuinely
  unconfirmed target. Distinct from `xpaysh/awesome-x402` above, which is done;
  `docs/REGISTRIES.md` §5 carries both, and the bare name "awesome-x402" is
  ambiguous between them.
- Every listing must point at `blackwall-free.onrender.com`; the old
  `agent-egress-proxy.onrender.com` returns 404.

---

## Infra & scale  *(post-traffic — don't pre-build)*

### Mainnet persistence + rolling aggregate
Move the store/ledger onto a **persistent disk** ($7/mo tier) so the data flywheel
survives restarts; add a rolling reputation aggregate + bounded history/nonce
eviction so memory/state stays flat under load.
- **Why deferred:** there's no traffic yet. Pay for persistence when there's data
  worth keeping (watch Render logs for `/v1/forecast-payment` + the payTo wallet).

### MCP-over-HTTP  — **SHIPPED**
An HTTP transport so *remote* agents and MCP registries can reach the MCP server,
which was previously stdio/local-only.
- **Shipped** as `mcp_server.serve_http()` / `http_handler_class()`: MCP
  Streamable HTTP on a single POST endpoint, stateless, 202 for notifications,
  405 on GET, body cap, optional `Origin` allowlist (DNS-rebinding guard).
  Hardened for chunked-body pollution and slowloris.
- **KNOWN GAP:** unauthenticated with **no rate limit**, only a body cap — unlike
  `blackwall.py`'s HTTP server. It binds `127.0.0.1` by default and nothing
  deploys it, so nothing is exposed; do not put it on a public bind without a
  limiter in front.
- Separately, `mcp.blackwalltier.com` is a Cloudflare Worker proxying the verdict
  engine. That is a different thing: this transport makes the engine itself
  `mcp add`-able and self-hostable.
