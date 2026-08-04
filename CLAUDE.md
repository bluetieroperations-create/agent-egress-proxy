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
  `mcp_server`. NOTE: after the signal-stability eval, `captive_sybil` GATES (HOLD)
  but `sybil_ring` is ADVISORY (surfaced, not gated -- it over-flags at partial
  ingestion coverage); see `docs/PAYER_GRAPH.md`. Also exposes the PAYER side as a
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
  payees; real burst detection needs COMPLETE history. Folds via
  `temporal_signal`/`velocity_source`; wired into `mcp_server`),
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
python -m unittest test_egress_proxy.py test_blackwall.py test_ledger.py test_reputation_onchain.py test_settlement_watch.py test_addresses.py test_x402.py test_mcp_server.py test_reputation_store.py test_facilitator.py test_discovery.py test_sanctions.py test_readiness.py test_ap_gate.py test_cdp_auth.py test_creds_local.py test_traceipt_attest.py test_traceipt_ingest.py test_traceipt_verify.py test_payload_sim.py test_traceipt_pull.py test_keccak.py test_secp256k1.py test_eip712.py test_calldata.py test_seller_audit.py test_aa_cosigner.py test_chain_backfill.py test_discovery_crawl.py test_ecosystem_scan.py test_http_util.py test_payer_graph.py test_payer_reputation.py test_settlement_velocity.py test_confidence.py test_redteam.py test_demo_flywheel.py test_verdict_anchor.py test_categories.py test_category_pricing.py test_check_seed_age.py test_price_integrity.py test_ratelimit.py test_fuzz_verdict.py
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
GO must be an EXPLICIT `known_gap`. Current: 15 core attacks caught, 3 documented
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
