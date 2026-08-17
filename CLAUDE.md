# CLAUDE.md

Guidance for working in this repo.

> ⚠️ **Multiple sessions? Read `docs/HANDOFF.md` FIRST.** Two separate projects live
> in this repo as two branches — **Blackwall** (payment-verdict engine, repo root,
> branch `claude/blackwall-x402-integration-j3rdab`) and **Traceipt** (`traceipt/`
> dir, branch `claude/x402-product-ideas-6adgah`). Do NOT merge them. Before pushing:
> `git fetch` + rebase onto the remote tip, then verify `local HEAD == remote HEAD`
> (a same-branch overwrite already happened once). Don't rebuild what the handoff's
> inventory lists as done. The `traceipt_*.py` files are a shared seam — coordinate
> schema changes.

## Repo

Two complementary AI-agent guardrails, stdlib-only Python, TDD-first:

- **egress_proxy.py** — network-layer egress control (localhost forward proxy;
  logs/gates every destination an agent reaches). Tests: `test_egress_proxy.py`.
- **blackwall.py** — action-layer payment verdict (pre-signature x402 GO/HOLD/STOP).
  Supporting: `ledger.py` (verdict→outcome moat flywheel; also computes a
  RECENCY-weighted `recent_dispute_rate` over the last `RECENT_WINDOW` confirmed
  outcomes -- the "going bad" signal a volume-averaged lifetime rate hides; folds
  into the verdict as a `going_bad` HOLD gate, see `docs/GOING_BAD.md`),
  `reputation_onchain.py` (live Base data spike),
  `settlement_watch.py` (trustless on-chain settlement confirmation),
  `chain_backfill.py` (seed reputation from PUBLIC Base USDC history with zero
  customers -- paginate a KNOWN x402 payee's inbound USDC via Blockscout and ingest;
  targeted not firehose; idempotent),
  `addresses.py` (EVM address validation/normalization),
  `x402.py` (Blackwall's own x402 billing: 402 challenge, facilitator seam,
  replay guard, sessions),
  `cdp_auth.py` (pure-Python Ed25519 (RFC 8032) + CDP Bearer-JWT, so the
  `CdpFacilitator` in x402.py can settle through the authenticated Coinbase CDP
  facilitator -- the one whose settlements Bazaar catalogs),
  `mcp_server.py` (MCP stdio server wrapping the verdict engine),
  `reputation_store.py` (SQLite indexed reputation store + record merging),
  `facilitator_sim.py` (reference x402 facilitator for the HttpFacilitator path),
  `discovery.py` (x402 service-discovery descriptor -- Blackwall's OWN),
  `discovery_crawl.py` (crawl OTHERS' x402 discovery/402 docs -> extract payees +
  advertised prices -> auto-feed chain_backfill (reputation), peer price baselines,
  and readiness targets; a self-populating map of the x402 seller ecosystem),
  `sanctions.py` (OFAC sanctions screening -- the "superset of free" layer),
  `readiness.py` (folds an ENDPOINT-readiness grade into the verdict; fail-open,
  conservative-only. Two sources: SELF-OWNED `LocalReadinessSource` (scores public
  signals we observe ourselves -- no third-party call, no query leak; preferred)
  and external `OntarioReadinessSource` (their free can-pay)),
  `clients/x402_pay.py` (TEST-ONLY funded-signer dry-run client; the one place
  that uses a dep -- `eth-account` -- to sign a real EIP-3009 X-PAYMENT;
  see `clients/README.md`. Deploy: `Dockerfile`, `fly.toml`, `render.yaml`),
  `integrations/langchain/` (LangChain plugin -- "call Blackwall before you sign":
  a `BlackwallPaymentGuardTool` the agent calls + a `BlackwallGuardrailCallback`
  that enforces the verdict on a payment tool. Core `blackwall_guard.py` is
  LangChain-free/stdlib + fully tested; `langchain_blackwall.py` is the thin adapter
  needing `langchain-core`. OBSERVE/ENFORCE modes; fail-safe to human CONFIRM. Its
  own tests run from that dir, not the root command),
  `integrations/wallets/` (wallet signing-guard adapters -- gate a wallet provider's
  server-side signing call with the verdict: sign on GO, human-confirm HOLD, WITHHOLD
  the signature on STOP. Shared `wallet_guard.py` core (+ `claim_from_tx` decoding
  ERC-20 transfers) with a runtime-toggleable FAIL_CLOSED/FAIL_OPEN availability
  policy; thin `turnkey_signer.py` / `privy_signer.py` shims map each provider's
  request. Stdlib; own tests run from that dir),
  `integrations/openclaw/` (OpenClaw/NemoClaw plugin -- a `before_tool_call` hook
  that recognizes payment-shaped tool calls (flat payTo/amount, 402-challenge
  accepts[], or a signed X-PAYMENT header -> passed through for payload-sim),
  forecasts them, and blocks non-GO. Enforce + fail-closed by default; keyless
  (free-tier endpoint), claim-only egress. TypeScript + vitest; own tests run
  from that dir (`npm install && npm test`), not the root command. Canonical
  source for the nemoclaw-community `blackwall-x402-payment-gate` example),
  `BLACKWALL.md`, `DISCOVERY.md`, `DEPLOY.md`, `COMPETITIVE.md`, `PRICING.md`,
  `ap_gate.py` (treasury/AP payout gate -- folds the verdict into a
  RELEASE/REVIEW/BLOCK decision at the approve-&-release step; see
  `docs/TREASURY_AP.md`),
  `seller_audit.py` (seller-side "verified merchant" tier -- EARNED not paid: audit
  an endpoint from readiness + on-chain history + sanctions + price-fairness, issue a
  signed/expiring/revocable attestation granting a bounded trust FLOOR that waives the
  thin-count gate but never the Sybil gate and never overrides a STOP; folds into
  decide_payment via `verified_floor` + forecast via a `SellerRegistry`),
  `payload_sim.py` (payload simulation: cross-check the agent's ACTUAL signed x402
  payment -- from the request-body `payment_authorization`, NOT the fee header --
  against the claim being scored. Phase 1: recipient/amount/asset/chain field match;
  Phase 2: recover the EIP-3009 signer and confirm == stated payer, binding chain +
  asset via the EIP-712 domain. Any mismatch is a hard STOP folded into the verdict.
  See `docs/STRATEGY_REVIEW.md`), `keccak.py` (pure-Python Keccak-256 -- Ethereum's,
  not FIPS SHA3), `secp256k1.py` (pure-Python secp256k1 ECDSA public-key recovery),
  `eip712.py` (EIP-712 typed-data hashing for transferWithAuthorization + address
  derivation) -- the stdlib crypto behind payload-sim Phase 2,
  `calldata.py` (payload-sim Phase 3: decode a contract-call payment's calldata and
  flag drainer patterns -- unlimited approval / setApprovalForAll / transfer to the
  wrong recipient/amount -- as a hard STOP; from the request-body `transaction`),
  `aa_cosigner.py` (AA co-signing -- Blackwall as a MANDATORY ERC-4337/7579 guard,
  off-chain half: compute the v0.7 `userOpHash`, decode `execute` + Phase-3 screen
  the real on-chain call, then ECDSA-sign the hash ONLY on GO/approved-HOLD and
  WITHHOLD on STOP; explicit fail-open/closed. Posture change -- see
  `docs/AA_COSIGNING.md`),
  `traceipt_attest.py` (anchor a verdict digest via Traceipt `POST /attest`, with
  x402 auto-pay + spend cap; also `proof_status`/`poll_proof` -- confirm an anchor
  was actually SEALED into a Merkle batch vs still pending vs LOST/404, since a 201
  only means accepted, see `docs/TRACEIPT_ATTEST_FINDING.md`),
  `verdict_anchor.py` (OPT-IN server-side auto-anchor behind `BLACKWALL_ANCHOR=1`:
  fire-and-forget each verdict's tokenless digest to Traceipt -- NON-BLOCKING on a
  daemon thread, FAIL-OPEN, KEY-FREE core (signer lazy-loaded from
  `SIGNER_PRIVATE_KEY` only when opted in); the verdict response is unchanged. See
  `docs/TRACEIPT_INTEGRATION.md`),
  `categories.py` (SHARED stdlib service-category classifier for an x402 resource URL --
  finance/ai-agents/onchain/commerce/... else other; DESCRIPTIVE, never gates; also the
  Traceipt receipt-tag proposal, see `docs/CATEGORY.md`),
  `category_pricing.py` (per-CATEGORY on-chain price baseline: a COLD-START payee quoting
  >=50x its category's settled median -> HOLD; `load_category_index`/`load_index_json` is
  the shared HTTP+MCP index loader),
  `price_integrity.py` (advertised-vs-settled DIVERGENCE: a payee whose on-chain settled
  median runs >=10x its most-EXPENSIVE Bazaar-advertised price lists cheap but collects
  more -> HOLD (bait-and-switch); HOLD-only, fail-open, temporal-confound-aware, eval-
  calibrated; folded via `divergence_ratio`/`divergence_index`. See `docs/CATEGORY.md`),
  `traceipt_ingest.py` (map on-chain-verified Traceipt
  receipts into reputation), `traceipt_verify.py` (pure-Python Ed25519 JWKS
  verification of Traceipt receipt envelopes -- the authenticity gate for ingest),
  `traceipt_pull.py` (pull signed receipts by id from live Traceipt -- `GET
  /receipts/{id}` + `GET /jwks.json` -- verify, and ingest the authenticated
  payments into a ReputationStore; the live half of the receipts->reputation
  flywheel, fail-closed on any unverified receipt),
  `ecosystem_scan.py` (fold the discovery crawl + backfill into per-ENDPOINT
  profiles (one per payee) and derive FOUR outputs from one pass: (1) INSTANT
  VERDICTS -- a pre-warmed ReputationStore corpus so a known payee gets real
  history not a cold-start HOLD; (2) STATE OF x402 -- `ecosystem_stats()`
  counts/price-distribution/concentration; (3) TRUST DIRECTORY --
  `rank_directory()` by an explainable trust_score (distinct payers dominate,
  volume/breadth log-damped, sanctioned sink to 0); (4) BD FUNNEL --
  `audit_candidates()` active/clean/not-yet-verified endpoints to pitch the
  Verified tier. Pure+stdlib, enrichment injected; `main()` crawls the Bazaar,
  backfills the top-N, and writes report/directory/candidates),
  `confidence.py` (how much EVIDENCE backs a verdict -- `assess_confidence(record,
  signals)` -> {level high/medium/low, score 0..1, backed_by[], missing[]} across
  five weighted dimensions: history depth, payer breadth, cross-counterparty
  corroboration, outcome/dispute depth, freshness. PURE + DESCRIPTIVE -- never
  changes the verdict; folded into every `decide_payment` response as `confidence`
  so a caller can tell a GO on real history from a cold-start default),
  `http_util.py` (hardened JSON GET for the live data path: retry+backoff on
  transient 429/5xx/timeout -- honors `Retry-After`, permanent 4xx not retried --
  plus a read-size cap; transport+clock injectable. Used by `chain_backfill`'s
  `BlockscoutPager` and `discovery_crawl`. See `docs/AUDIT_ZEROCUSTOMER.md`),
  `secret_scan.py` (leaked-SECRET / PII guard for the payment PAYLOAD -- an x402 payment
  and its memo settle ON-CHAIN, public + irreversible, so a private key / seed phrase /
  API credential in a free-text field is catastrophic; a common prompt-injection
  exfiltration vector. HIGH (credential) -> STOP; MEDIUM (SSN / bare hex / mnemonic-shape)
  -> HOLD. TWO HARD RULES: never echoes/logs the secret (findings carry TYPE + FIELD +
  REDACTED hint only), and scans FREE-TEXT fields ONLY -- never the structural crypto
  fields (counterparty/asset/tx `to`/`data`/hash), which legitimately hold 64-hex, so a
  tx hash is never mis-flagged as a private key. `scan_payload`/`scan_text` pure+stdlib;
  folds into `decide_payment` via `secret_findings` and is scanned in `forecast` from the
  raw request body. Tests: `test_secret_scan.py`. Built to close a gap vs the PaySafe
  competitor -- see `COMPETITIVE.md`),
  `blockscout.py` (FREE keyless on-chain ENRICHMENT for a counterparty via Blockscout's
  public Base API: `is_scam` crowd tag, contract-vs-EOA, ENS/labels, ERC-20 activity.
  HARD BOUNDARY -- raw chain data + crowd tags, NOT a sanctions source: it can ONLY
  push a would-be GO to REVIEW (HOLD) via `is_scam`, and NEVER clears (GO) or produces
  (STOP/hard_stop) a compliance decision; OFAC/Chainalysis/TRM stay the authority.
  `address_enrichment()` is the pure derivation; `BlockscoutEnrichmentSource` does the
  live fetch -- OPT-IN (network on the hot path, behind `BLACKWALL_ONCHAIN_ENRICH=1`)
  and FAIL-OPEN. Folds into `decide_payment`/`forecast` via `enrichment`/
  `enrichment_source`, structurally added only to the `go` conditions so it can never
  reach the STOP path. Tests: `test_blockscout.py`),
  `payer_graph.py` (the cross-counterparty payer graph as a reputation signal:
  build the bipartite payer<->payee graph from ingested settlements and derive per
  payee `established_payers` (payers proven to also pay OTHER known payees --
  hard-to-fake, monotonic), `captive_ratio`, `cross_score`, and a conservative
  `captive_sybil` flag -- clears the naive distinct-payer gate yet every payer is
  captive. Folds into `decide_payment`/`forecast` via `payer_graph_signal` /
  `graph_source`: HOLD-only, never STOP, fail-open; wired into `mcp_server` off the
  same `--store`. Catches wash-farm payees the per-payee distinct count misses),
  `payer_reputation.py` (reputation for the PAYERS, propagated from trusted anchors:
  anchors = payees with many distinct on-chain payers (hard to fake); a payer's
  reputation saturates on the number of distinct anchors it pays (a proven real
  agent); a payee's `reputable_payers` / `sybil_ring` flag catches a mutually-paying
  sockpuppet RING -- clears the distinct gate, payers even have breadth>=2, yet NOT
  ONE pays an anchor -- which breadth-only `captive_sybil` misses. `PayerReputationSource`
  is a drop-in SUPERSET of `PayerGraphSource` (`.cross_signal` adds the reputation
  fields + `sybil_ring`); folded into the verdict conservatively and wired into
  `mcp_server`. NOTE: `captive_sybil` and `sybil_ring` now BOTH GATE (HOLD) -- the
  Stage-3 coverage-convergence eval (`coverage_eval.py`, `docs/DATA_COMPLETENESS.md`)
  proved `sybil_ring`'s false-flag rate on known-good payees stabilized to ~0 on the
  shipped corpus, so it graduated from advisory to a gate behind the reversibility lock
  `SYBIL_RING_GATES` (flip to False to demote instantly; HOLD-only either way). See
  `docs/PAYER_GRAPH.md`. Also exposes the PAYER side as a
  queryable output -- `payer_profile()` / `.screen()` and the `screen_payer` MCP
  tool: a facilitator/wallet screens WHO is paying (tier established/emerging/unknown,
  anchors paid, breadth) before it settles; unknown is NEUTRAL cold-start, never a
  block),
  `settlement_velocity.py` (the TEMPORAL axis: reads the settlement timestamps
  (ingested, previously unused) -> age/recency + payer-acquisition `peak_day_share`.
  `stale` (no settlement in STALE_DAYS -> possibly dead/abandoned endpoint) GATES the
  verdict (HOLD, never STOP, fail-open); last_seen is robust to the backfill window.
  `burst_sybil` (many distinct payers all first-seen in ONE day) is DIAGNOSTIC ONLY,
  never gated -- a targeted backfill captures only a recent WINDOW, so a high-volume
  payee's whole visible history compresses into ~1 day and it flags the MOST reputable
  payees; real burst detection needs COMPLETE history. ADJUDICATED (measured 8.1% of
  anchors flagged on the shipped corpus -- see `docs/DATA_COMPLETENESS.md`): stays
  advisory, not retired; do NOT re-open without a complete-history source. Folds via
  `temporal_signal`/`velocity_source`; wired into `mcp_server`),
  `rwa_readiness.py` (PRE-TRADE gate for agents BUYING tokenized RWAs with stablecoins:
  the payment leg is USDC so ~90% of the stack already applies to the payee; the NOVEL
  wedge is TRANSFER-RESTRICTION readiness -- a permissioned security (ERC-3643/T-REX,
  ERC-1400, allowlist ERC-20) REVERTS a transfer to a wallet that is not KYC-verified/
  whitelisted, or is frozen, or while paused, so an agent can pay stablecoins and receive
  nothing. Reads the token's RECEIVER-side restriction interface for the request `payer`
  and folds a grade via `apply_rwa_readiness`: `blocked` -> GO->HOLD, `ready`/`unknown`
  no-op. HARD BOUNDARY like blockscout.py -- HOLD-only, NEVER STOP/hard_stop (OFAC stays
  the STOP authority); FAIL-OPEN (`decode_bool("0x")->None`, absent method = unknown not
  false); OPT-IN behind `BLACKWALL_RWA_READINESS=1` (+`BLACKWALL_RWA_RPC_URL`), fires only
  when a request carries `acquires`. Pure core + injected eth_call transport. See
  `docs/TOKENIZED_RWA.md`),
  `tokenized_stock_registry.py` (DISCOVERY layer under the RWA gate -- recognize a token
  contract -> issuer/underlying/ISIN. DESCRIPTIVE like categories.py, never gates. Keyless
  ingest from the Backed/xStocks `/public/assets` feed (the one FREE cross-chain
  ticker->address map) + an operator STATIC_SEED for gated issuers (Ondo/Dinari/Robinhood;
  EMPTY by default -- NEVER fabricate addresses). Chain-aware normalization: EVM lowercased,
  Solana base58 case-preserved),
  `solana_rwa.py` (the SOLANA leg of the RWA gate -- SPL Token-2022 analogue of
  rwa_readiness's EVM eth_call reads, since Backed/Ondo settle heavily on Solana. Pure
  base58 + `parse_mint_extensions` (DefaultAccountState=Frozen / TransferHook /
  NonTransferable / PermanentDelegate) + `parse_token_account` (frozen state) ->
  `assess_solana_readiness` returns the SAME signal shape, folded via the shared
  `apply_rwa_readiness`. `SolanaRwaReadinessSource` reads getAccountInfo; wired through
  `rwa_readiness.CombinedRwaReadinessSource` (EVM+Solana dispatch by token format). ATA
  auto-derivation deferred -- pass `acquires.receiver_token_account` for the per-wallet
  frozen read. See `docs/TOKENIZED_RWA.md`),
  `pyth_price.py` (PEG/NAV divergence from the FREE keyless Pyth Hermes oracle -- the RWA
  analogue of the core price-anomaly gate: is the agent paying near the REAL underlying
  stock price or a big premium (depeg / stale quote / bad route)? Resolves the recognized
  `underlying_symbol` to a Pyth equity feed, compares to the per-unit price paid
  (`acquires.unit_price` or amount/quantity); OVERPAY-only, HOLD-only (oracle can be
  stale, never STOP), fail-open. `apply_peg` folds `signals.peg`; opt-in `BLACKWALL_PYTH`),
  `rwa_ledger.py` (the ACCUMULATION corpus -- the flip from READING public data to
  ACCUMULATING a private one. Append-only JSONL of every RWA buy + its context
  (asset/issuer/underlying, restriction grade, peg ratio, verdict); `asset_profile` /
  `issuer_profile` roll it into per-asset and per-issuer history (the earned issuer-trust
  input). DESCRIPTIVE data tap, fail-soft (logging never breaks a verdict), carries no
  secrets; opt-in `BLACKWALL_RWA_LEDGER=<path>`. Wired into `forecast`, keyed by
  `receipt_id` so outcomes link back. Also `pending_buys`, outcome-aware
  `asset_profile`/`issuer_profile`, and `issuer_trust` (earned grade)),
  `rwa_outcomes.py` (the OUTCOME-capture loop that LABELS the corpus -- "what Blackwall
  decided" -> "what actually happened", closing the flywheel. `assess_outcome`
  MARK-TO-MARKET via Pyth (underlying_now/price_paid; <1 = underwater, vindicating a peg
  warning) + optional injected balance-held; `OutcomeChecker`/`capture_outcomes(ledger,
  checker, horizon, now)` is the T+N labeler -- pending buys older than the horizon, not
  yet labeled, get an outcome event joined by receipt_id. Idempotent, fail-open; CLI
  `python rwa_outcomes.py rwa.jsonl --horizon-hours 24 [--evm-rpc URL] [--solana-rpc URL]`
  for a cron),
  `rwa_balance.py` (keyless "did the security arrive?" reader for the outcome loop --
  `BalanceReader` dispatches EVM `balanceOf` / Solana `getTokenAccountsByOwner` by token
  format. `__call__` -> bool holds-heuristic; `balance_of` -> raw int for the DEFINITIVE
  before/after delta: `forecast` snapshots `pre_balance` at the buy (opt-in), the outcome
  loop records `settled = post > pre` (actual arrival), preferred over the heuristic.
  Injected transports; fail-open. Residual attribution caveat: received-then-moved
  false-negatives + same-token cross-buy ambiguity -- the settlement tx hash is the real fix),
  `rwa_backfill.py` (SEED the RWA outcome corpus from PUBLIC on-chain history with ZERO
  customers -- the tokenized-RWA analogue of chain_backfill.py. On-chain history is already
  LABELED: a token transfer that landed IS a successful settlement. Paginate a known token's
  transfers (keyless Blockscout), reconstruct each acquisition (transfer to a real wallet =
  mint/secondary buy), write an idempotent buy + `settled=True` outcome keyed by tx+recipient.
  Moves an issuer from "insufficient" to a real hard-to-fake footprint (settlement volume +
  distinct buyers) so `issuer_trust` can grade it. HONEST SCOPE: no price-paid on a bare
  transfer -> no mark-to-market/underwater (those come from the organic flywheel); so a
  backfill-only issuer grades on settlement + volume, not value-outcome. Verified live: 74
  real TSLAon acquisitions / 33 distinct buyers -> Ondo graded `medium`. Targeted (per-token
  cap), fail-soft, idempotent; CLI `python rwa_backfill.py rwa.jsonl --chain ethereum`),
  `rwa_report.py` (turn the accumulation corpus into operator INTELLIGENCE -- the payoff
  that makes the moat legible: totals + verdict mix + restriction-posture map, an ISSUER
  DIRECTORY ranked by earned `issuer_trust` grade (+ settlement/underwater rates), and
  LEADERBOARDS of the most OVERPRICED (peg divergence) and most UNDERWATER assets. Pure
  derivation over ledger events; DESCRIPTIVE, never gates; CLI `python rwa_report.py
  rwa.jsonl [--top N]`),
  `backed_oracle.py` (two keyless signals from Backed's public oracle + proof-of-reserves
  endpoints: (1) a BACKING gate -- `sharesHeld / circulatingSupply` per token; materially
  < 1 -> under-collateralized -> HOLD (a novel "is it actually backed" signal); (2) an
  authoritative Pyth `hermesId` + underlying map that hardens the peg gate (exact
  issuer-declared feed, not a ticker search). `apply_backing` folds `signals.backing`;
  `BackedOracleIndex.feed_map()` seeds `PythPriceSource`. Conservative/HOLD-only, fail-open;
  wired via `backing_index`),
  `rams_readiness.py` (DORMANT-BUT-READY ERC-8226 (RAMS) agent-AUTHORIZATION axis -- reads
  `canExecute(agent,principal,asset,action,amount)` from a mandate registry and folds via
  the shared `apply_rwa_readiness`. A NO-OP until a request advertises `acquires.mandate_registry`
  (or `BLACKWALL_RAMS_REGISTRY`), then activates with ZERO code change -- wired idle into
  `CombinedRwaReadinessSource`. The authorization revert-cause our eligibility reads miss;
  HOLD-only, fail-open. Enum names + ERC-8004 agent identity pending mainnet RAMS),
  `rwa_aggregate.py` (the SIGNAL-AGGREGATION / confidence layer over the ~8 RWA gates.
  Each gate is conservative+HOLD-only so STACKING is safe, but naively letting every one
  flip GO->HOLD raises the cumulative FALSE-HOLD rate + piles up reasons. TIERS: each
  signal is `gate` (may flip GO->HOLD alone -- eligibility/backing/peg) or `advisory`
  (informs the risk view but doesn't gate alone -- noisier ones like holder-concentration
  on an RWA). `aggregate(signals)` -> one weighted risk {score, level, primary_concern,
  concerns[], gating[], advisory[]}; `apply_aggregate` records `signals.rwa_risk` + a
  COLLECTIVE rule (advisory signals agreeing past a weight threshold escalate GO->HOLD
  once). PURE, descriptive, conservative -- the strong gates already decided; this gives
  the operator ONE ranked view + controls noise. Folded last in `forecast`),
  `aave_reserve.py` (ADVISORY quality/de-risk signal from Aave v3 reserve config:
  `getReserveConfigurationData` -> is this token vetted enough for a major lending
  protocol to LIST as collateral, and has Aave since FROZEN (de-risked) it? `frozen` ->
  advisory concern (weighed collectively by rwa_aggregate, never gates alone); `listed` ->
  positive note; `unlisted` (the common RWA case) -> NEUTRAL/no signal. Keyless eth_call,
  fail-open, opt-in `BLACKWALL_AAVE`. Provider address verified live on WETH),
  `holder_concentration.py` (keyless rug/manipulation signal from token holder
  distribution (Blockscout): a single dominant NON-CONTRACT wallet holding >= 50% of
  supply -> HOLD (dump/manipulation risk). CONTRACT holders EXCLUDED (LP/issuer custody/
  bridges hold large shares legitimately -- esp. RWAs), zero/burn excluded. `top_eoa_share`
  / `assess_concentration` / `apply_concentration` pure; `HolderConcentrationSource` fetches.
  HOLD-only, fail-open, advisory (noisier for RWAs where issuer-EOA custody would false-flag).
  Folded via `holder_source`, opt-in `BLACKWALL_HOLDER_CONCENTRATION`),
  `dex_price.py` (the token's REAL on-chain market price from a Uniswap-v3 pool + a
  market-vs-NAV peg gate -- the piece the oracle-managed Pyth peg can't see (the Backed
  oracle tracks the underlying by construction, so it misses the TOKEN trading off NAV on
  an actual pool: bait/manipulated pool, thin liquidity, market depeg). `dex_token_price`
  decodes `slot0().sqrtPriceX96` -> token price in USDC (verified live against the
  USDC/WETH pool); `assess_market_peg` flags >10% deviation from the underlying (Pyth) ->
  HOLD; `DexPriceSource` discovers the DEEPEST pool via the v3 factory `getPool` across fee tiers
  (dust-filtered by a USDC-balance floor) and prefers QuoterV2 `quoteExactInputSingle` for
  the EXECUTABLE, size-aware price + slippage (falls back to slot0 spot). Conservative
  (HOLD-only, monotonic -- can only ADD caution, never clear), fail-open, opt-in
  `BLACKWALL_DEX`. KNOWN LIMITATION: no liquidity-depth check -> a dust pool can false-flag
  (bounded: HOLD-only; the Pyth paid-vs-underlying peg still fires independently)),
  `auth_sim.py` (simulate the ACTUAL EIP-3009 authorization, not a proxy transfer.
  settlement_sim simulates `transfer`, but x402 settles via `transferWithAuthorization` --
  so THREE failure modes are structurally invisible to it: (1) REPLAY, the nonce already
  used/cancelled -- read DIRECTLY via `authorizationState(authorizer,nonce)`, not inferred
  from a revert; (2) EXPIRY, validAfter/validBefore -- a pure clock check, no chain call;
  (3) EXECUTION, the real 9-arg call reverting (bad signature, blacklist, balance).
  Cheapest-first: window check is free, the state read is a tiny view, and the full
  execution sim runs ONLY if those are clean. ABI NOTE: `rwa_readiness.eth_call_data`
  handles address/uint only -- a bytes32 nonce falls through its numeric branch and encodes
  ZEROS (simulating a DIFFERENT authorization), so `encode_bytes32`/`encode_uint`/
  `encode_address`/`split_signature` are explicit + separately tested. HOLD-only, never
  STOP (payload_sim keeps the STOP authority for a mismatched/forged payload), fail-open,
  folded via `auth_sim_source` under the same `BLACKWALL_SETTLEMENT_SIM` flag. VERIFIED
  LIVE on mainnet USDC: `authorizationState` returns a clean false for an unused nonce, and
  the real transferWithAuthorization with a bogus signature reverts "ECRecover: invalid
  signature" -- proving the encoding reaches the genuine function.
  Tests: `test_auth_sim.py`),
  `rpc_node.py` (OUR OWN JSON-RPC endpoint -- the single controlled front door for every
  on-chain read. The simulation gates put a QUERY LEAK on the hot path ("this payer is about
  to pay this payee this amount"), which is exactly what readiness.py avoids, so this closes
  it. NOT a node (syncing one is TB + days; it is operated, not imported) -- it is the piece
  that makes running your own a ONE-LINE config change and shrinks the leak either way:
  SINGLE EGRESS POINT (the egress_proxy.py idea applied to chain reads), CACHE (a repeat
  check never re-leaks -- measured live: 3 upstream calls cold, 0 on repeat, identical
  verdicts), SINGLE-FLIGHT (the cache only stops SEQUENTIAL re-leaks; an audit measured 8
  CONCURRENT identical checks costing 8 disclosures -- now 8 -> 1), METHOD ALLOWLIST (read-only; no eth_send*/admin_/personal_/debug_ -- our
  endpoint can never broadcast a tx), and a SELF-HOST SWITCH (point --upstream at your own
  node -> zero third-party leakage, no code change; the env vars already take any URL).
  TTL defaults to 30s because staleness is a SAFETY tradeoff (a payee blacklisted 5 min ago
  must not read as fine); EVM reverts ARE cached (deterministic, and the blacklisted-payee
  case is the most sensitive query), bare errors are NOT (transient). `is_allowed` /
  `cache_key` / `validate_request` pure; `RpcCache` + `LocalRpcNode` + CLI
  `python rpc_node.py --upstream URL`. See `docs/RPC_SELFHOST.md`. Tests: `test_rpc_node.py`),
  `transfer_sim.py` (the SHARED transfer-SIMULATION core -- ask the chain "would this
  transfer actually succeed?" via eth_call, then decode + ATTRIBUTE the revert. Built
  because interface probing FAILED: all 535 corpus tokens x 9 probes -> 535/535 alive but
  0/535 exposing any permissioned interface, so `rwa_readiness` answered "unknown" for the
  entire corpus. Simulation needs NO interface and is how every permissioned issuer we hold
  was discovered. THE CONTROL IS THE POINT: every assessment runs TWICE (target + control
  address) so a revert is attributed only when they disagree -- target fails + control OK =>
  RECEIVER blocked; both fail => SENDER at fault, never blamed on the receiver. Reuses
  `revert_scan.classify_revert` (calibrated on real strings). `clamp_amount` /
  `decode_revert_error` / `attribute` / `to_readiness_probe` pure; `TransferSimulator`
  (injected transport) + `SimulationReadinessSource`, which emits the SAME probe shape
  `rwa_readiness.assess_transfer_readiness` already takes -- so it folds through the
  EXISTING verdict path with zero new plumbing. Verified live: STBT + BUIDL -> blocked with
  their real revert strings, a freely-transferable control -> ready. Tests:
  `test_transfer_sim.py`),
  `settlement_sim.py` (PRE-SIGNATURE settlement feasibility for the CORE x402 path -- the
  crypto-side application of the whole RWA arc. Before the agent signs an EIP-3009
  authorization, simulate the USDC transfer: if the PAYEE (or PAYER) is frozen/blacklisted
  the payment would REVERT on-chain, and a Circle-blacklisted counterparty is itself a
  serious risk signal. The CONTROL simulation separates payee-side from payer-side blocks.
  `assess_settlement` -> ready/payee_blocked/payer_blocked/underfunded/unknown;
  `apply_settlement_sim` folds `signals.settlement_sim`. HARD BOUNDARY (mirrors
  blockscout.py): HOLD-only, NEVER STOP (sanctions.py stays the compliance authority),
  never upgrades, fail-open. UNDERFUNDED is recorded but does NOT gate (balance can change
  before signing). Opt-in `BLACKWALL_SETTLEMENT_SIM=1` + an RPC; folded via
  `settlement_sim_source`. VERIFIED LIVE on mainnet USDC: a real Circle-blacklisted payee ->
  payee_blocked + GO->HOLD with the real revert string, a clean payee -> ready/ungated.
  Tests: `test_settlement_sim.py`),
  `revert_scan.py` (the settlement-reliability axis's DATA TAP -- read an RWA token's FAILED
  transfer attempts from public chain history, DECODE the revert reason, and CLASSIFY it so
  only ISSUER-CAUSED restriction reverts (allowlist/KYC/frozen/paused/compliance) count --
  never a fat-finger "insufficient balance" or a gas failure. Closes the survivorship bias:
  the backfill only sees successes (`settle_rate` always 1.0), so the missing denominator is
  FAILED attempts. PROVEN via live spike: failed txns are queryable (Blockscout `filter=to`,
  `status`), the per-tx detail endpoint returns a DECODED `revert_reason` (list view nulls
  it), but the reverts on freely-transferable RWAs are generic ("exceeds balance") NOT
  restriction -- so the signal is the RESTRICTION-CLASS revert this isolates.
  CALIBRATED on REAL strings (harvested by eth_call-simulating transfers from OFAC/Circle-
  blacklisted addrs): 4/4 -- USDC's "Blacklistable: account is blacklisted" -> restriction,
  "ERC20: transfer amount exceeds balance" -> balance. An `opaque` class covers reason-less
  pre-0.8 blocks (USDT's INVALID), which UNDER-count restrictions (fail-safe) but cap recall.
  Ingestion probe: ALL 535 corpus tokens x 9 interface probes (decimals control) ->
  535/535 alive, 0/535 permissioned, so restriction reverts are STRUCTURALLY impossible
  on today's corpus. `extract_reason`
  / `classify_revert` / `summarize_reverts` / `restriction_axis` pure; `RevertScanner`
  (two-step, injected transport) + `scan_corpus_issuers` + `main()` produce a {issuer:
  summary} the grade folds. DORMANT-BUT-READY (mirrors rams_readiness): inert until an issuer
  has >= MIN_RESTRICTION_EVIDENCE restriction reverts -- zero on today's corpus, verified live
  (8 real Backed reverts all balance-class -> axis dormant). ADJUDICATED: permissioned
  issuers WERE then sourced (transfer-SIMULATION discovery) and ingested -- Ondo OUSG,
  BlackRock BUIDL, Matrixdock STBT, Hashnote USYC, 674 acquisitions -- and the axis
  ACTIVATED (BUIDL 20 restriction reverts/9.1%, STBT 7/3.4%). Its first act was to try to
  downgrade BLACKROCK to LOW because its lock-up/registry checks reject non-allowlisted
  wallets (i.e. because it works AS DESIGNED), so `REVERT_AXIS_GATES` STAYS OFF: a
  restriction revert measures TRANSFER FRICTION, not issuer untrustworthiness. Re-home it
  beside rwa_readiness as an ASSET-level signal; do NOT flip the lock.
  Tests: `test_revert_scan.py`),
  `issuer_trust_gate.py` (GRADUATE the earned per-issuer trust grade into the RWA verdict --
  the payoff of the accumulation arc: `rwa_ledger.issuer_trust` grades an issuer from its
  LABELED settlement/outcome history (hard-to-fake), and this surfaces that grade in every
  RWA verdict. `build_issuer_grades` precomputes {issuer: grade} from the corpus ONCE at
  startup; `IssuerTrustSource.grade()` is the O(1) hot-path lookup; `apply_issuer_trust`
  folds `signals.issuer_trust`. GRADUATION DISCIPLINE (mirrors SYBIL_RING_GATES): the
  `ISSUER_TRUST_GATES` reversibility LOCK defaults False -> DESCRIPTIVE ONLY (recorded, never
  affects the verdict) until the backfilled corpus proves ~0 false-flags on known-good
  issuers; when flipped, a LOW grade becomes an ADVISORY signal rwa_aggregate weighs
  COLLECTIVELY (never gates alone, never STOP, never clears). Built at startup from the
  BLACKWALL_RWA_LEDGER corpus; folded via `issuer_trust_source`. Now also folds the
  SETTLEMENT-RELIABILITY AXIS from `revert_scan` (restriction-revert rate) behind a SECOND
  independent lock `REVERT_AXIS_GATES` (default False) -- `build_issuer_grades(...,
  revert_summaries=)` attaches it per issuer, `_fold_revert_axis` drags to LOW only when the
  lock is on AND evidence is sufficient AND the rate is material; dormant on today's corpus),
  `ROADMAP.md`, `docs/DATA_SOURCE_SPIKE.md`. Tests:
  `test_blackwall.py`, `test_ledger.py`, `test_reputation_onchain.py`,
  `test_settlement_watch.py`, `test_addresses.py`, `test_x402.py`,
  `test_mcp_server.py`, `test_reputation_store.py`, `test_facilitator.py`,
  `test_discovery.py`, `test_sanctions.py`, `test_readiness.py`,
  `test_ap_gate.py`.

Convention: the security/decision-critical logic lives in small **pure functions**
at the top of each module, unit-tested TDD-first with **mutation notes** (each
test states the mutation it kills). Keep new code stdlib-only and match this style.

Run all tests:
```sh
python -m unittest test_egress_proxy.py test_blackwall.py test_ledger.py test_reputation_onchain.py test_settlement_watch.py test_addresses.py test_x402.py test_mcp_server.py test_reputation_store.py test_facilitator.py test_discovery.py test_sanctions.py test_readiness.py test_ap_gate.py test_cdp_auth.py test_creds_local.py test_traceipt_attest.py test_traceipt_ingest.py test_traceipt_verify.py test_payload_sim.py test_traceipt_pull.py test_keccak.py test_secp256k1.py test_eip712.py test_calldata.py test_seller_audit.py test_aa_cosigner.py test_chain_backfill.py test_discovery_crawl.py test_ecosystem_scan.py test_http_util.py test_payer_graph.py test_payer_reputation.py test_settlement_velocity.py test_confidence.py test_redteam.py test_demo_flywheel.py test_verdict_anchor.py test_categories.py test_category_pricing.py test_check_seed_age.py test_price_integrity.py test_ratelimit.py test_fuzz_verdict.py test_blockscout.py test_verdict_oracle.py test_calibration_lock.py test_coverage_eval.py test_refresh_guard.py test_secret_scan.py test_bench.py test_two_stage_signer.py test_rwa_readiness.py test_tokenized_stock_registry.py \
test_solana_rwa.py test_pyth_price.py test_rwa_ledger.py test_rwa_outcomes.py \
test_rwa_balance.py test_rwa_report.py \
 test_backed_oracle.py test_rams_readiness.py \
 test_dex_price.py test_holder_concentration.py \
 test_rwa_aggregate.py test_aave_reserve.py \
 test_rwa_backfill.py test_issuer_trust_gate.py test_revert_scan.py \
 test_transfer_sim.py test_settlement_sim.py test_rpc_node.py \
 test_auth_sim.py
```

`clients/demo_flywheel.py` demonstrates the verdict->outcome->reputation->verdict loop
end to end (LABELED SIMULATION -- real EIP-3009 signature on the payment leg via
eth-account, settlement mocked; a funded round-trip is the operator's to run): a
merchant Blackwall knows nothing about earns GO purely from its own settled verdicts,
then loses it (going_bad) when recent outcomes turn to disputes. Guarded by
`test_demo_flywheel.py`.

`redteam.py` is the adversarial coverage scorecard: it drives a battery of attacks +
legit controls through `decide_payment` and derives each disposition (CAUGHT /
KNOWN GAP / CLEAN / FALSE POSITIVE / MISS). `test_redteam.py` guards it -- the caught
set may not shrink, no control may become a false positive, and any attack that gets
GO must be an EXPLICIT `known_gap`. Current: 17 core attacks caught, 2 documented
gaps, 0 false positives.

## Standing working practice: ALWAYS deep audit → eval → verify

After any non-trivial change, before reporting it done, run a full pass — do not
treat a green test suite as sufficient:

1. **Audit (adversarial).** Actively try to break the change. Hunt real bugs:
   join-key uniqueness, idempotency/replay, semantic mismatches between
   components, boundary conditions, injection/oversize, auth/abuse paths,
   collisions. Assume the happy-path tests miss things — they do.
2. **Eval.** Probe behavior across realistic and edge scenarios (not just the
   cases the tests already cover); sanity-check the decision boundaries and
   numbers against first principles.
3. **Verify.** Run the full suite AND exercise the real path (live HTTP / CLI),
   not just unit tests. Confirm fixes end-to-end. Add a regression test for every
   bug found.

Report findings honestly in a severity table, fix the real bugs, and document
the design/security limitations you are NOT fixing yet. Surface what's still
stubbed rather than implying completeness.
