# CLAUDE.md

Guidance for working in this repo.

## Repo

Two complementary AI-agent guardrails, stdlib-only Python, TDD-first:

- **egress_proxy.py** — network-layer egress control (localhost forward proxy;
  logs/gates every destination an agent reaches). Tests: `test_egress_proxy.py`.
- **blackwall.py** — action-layer payment verdict (pre-signature x402 GO/HOLD/STOP).
  Supporting: `ledger.py` (verdict→outcome moat flywheel),
  `reputation_onchain.py` (live Base data spike),
  `settlement_watch.py` (trustless on-chain settlement confirmation),
  `addresses.py` (EVM address validation/normalization),
  `x402.py` (Blackwall's own x402 billing: 402 challenge, facilitator seam,
  replay guard, sessions),
  `cdp_auth.py` (pure-Python Ed25519 (RFC 8032) + CDP Bearer-JWT, so the
  `CdpFacilitator` in x402.py can settle through the authenticated Coinbase CDP
  facilitator -- the one whose settlements Bazaar catalogs),
  `mcp_server.py` (MCP stdio server wrapping the verdict engine),
  `reputation_store.py` (SQLite indexed reputation store + record merging),
  `facilitator_sim.py` (reference x402 facilitator for the HttpFacilitator path),
  `discovery.py` (x402 service-discovery descriptor),
  `sanctions.py` (OFAC sanctions screening -- the "superset of free" layer),
  `readiness.py` (folds an ENDPOINT-readiness grade into the verdict; fail-open,
  conservative-only. Two sources: SELF-OWNED `LocalReadinessSource` (scores public
  signals we observe ourselves -- no third-party call, no query leak; preferred)
  and external `OntarioReadinessSource` (their free can-pay)),
  `clients/x402_pay.py` (TEST-ONLY funded-signer dry-run client; the one place
  that uses a dep -- `eth-account` -- to sign a real EIP-3009 X-PAYMENT;
  see `clients/README.md`. Deploy: `Dockerfile`, `fly.toml`, `render.yaml`),
  `BLACKWALL.md`, `DISCOVERY.md`, `DEPLOY.md`, `COMPETITIVE.md`, `PRICING.md`,
  `ap_gate.py` (treasury/AP payout gate -- folds the verdict into a
  RELEASE/REVIEW/BLOCK decision at the approve-&-release step; see
  `docs/TREASURY_AP.md`),
  `payload_sim.py` (payload simulation: cross-check the agent's ACTUAL signed x402
  payment -- from the request-body `payment_authorization`, NOT the fee header --
  against the claim being scored. Phase 1: recipient/amount/asset/chain field match;
  Phase 2: recover the EIP-3009 signer and confirm == stated payer, binding chain +
  asset via the EIP-712 domain. Any mismatch is a hard STOP folded into the verdict.
  See `docs/STRATEGY_REVIEW.md`), `keccak.py` (pure-Python Keccak-256 -- Ethereum's,
  not FIPS SHA3), `secp256k1.py` (pure-Python secp256k1 ECDSA public-key recovery),
  `eip712.py` (EIP-712 typed-data hashing for transferWithAuthorization + address
  derivation) -- the stdlib crypto behind payload-sim Phase 2,
  `traceipt_attest.py` (anchor a verdict digest via Traceipt `POST /attest`, with
  x402 auto-pay + spend cap), `traceipt_ingest.py` (map on-chain-verified Traceipt
  receipts into reputation), `traceipt_verify.py` (pure-Python Ed25519 JWKS
  verification of Traceipt receipt envelopes -- the authenticity gate for ingest),
  `traceipt_pull.py` (pull signed receipts by id from live Traceipt -- `GET
  /receipts/{id}` + `GET /jwks.json` -- verify, and ingest the authenticated
  payments into a ReputationStore; the live half of the receipts->reputation
  flywheel, fail-closed on any unverified receipt),
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
python -m unittest test_egress_proxy.py test_blackwall.py test_ledger.py test_reputation_onchain.py test_settlement_watch.py test_addresses.py test_x402.py test_mcp_server.py test_reputation_store.py test_facilitator.py test_discovery.py test_sanctions.py test_readiness.py test_ap_gate.py test_cdp_auth.py test_creds_local.py test_traceipt_attest.py test_traceipt_ingest.py test_traceipt_verify.py test_payload_sim.py test_traceipt_pull.py test_keccak.py test_secp256k1.py test_eip712.py
```

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
