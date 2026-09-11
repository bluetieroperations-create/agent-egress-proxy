# CLAUDE.md

Guidance for working in this repo.

> ⚠️ **Multiple sessions?** Two separate projects live in this repo as two branches —
> **Blackwall** (payment-verdict engine, repo root, branch
> `claude/blackwall-x402-integration-j3rdab`) and **Traceipt** (`traceipt/` dir,
> branch `claude/x402-product-ideas-6adgah`). **Do NOT merge them** — the Traceipt
> branch also modifies root `render.yaml`, which is Blackwall's live Render blueprint.
> Before pushing: `git fetch` + rebase onto the remote tip, then verify
> `local HEAD == remote HEAD` (a same-branch overwrite already happened once). The
> `traceipt_*.py` files are a shared seam — coordinate schema changes.
>
> Session handoffs are **not kept in this repo** — it is public. They are delivered
> to the operator directly. Ask for the current one rather than looking for a file.

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
  targeted not firehose; idempotent. TRUNCATION IS REPORTED, NOT SWALLOWED:
  `collect_paged` returns `(items, truncated)` and `backfill`'s summary carries a
  `truncated` count plus a per-payee flag, because the bare list it used to return
  let a capped walk read as a complete history. MEASURED COST of that: the shipped
  `data/reputation_seed.db.gz` holds 239 of Bitrefill's 29,231 Base transfers
  (0.8%) and 70 of 281 payees (25%) sit on an exact 50-multiple >= 100 -- the page
  cap, not the ecosystem. No verdict flips on it (the thin/Sybil gates need >= 20
  and >= 3, and a capped payee has >= 100; `stale` reads `last_seen`, which is
  exact because the pager walks newest-first), but `age_days` INVERTS -- Bitrefill
  reads as a 4-day-old merchant -- and `burst_sybil` was calibrated on the
  artifact. `strict=True` / `--strict` raises `IncompleteHistory` for a deliberate
  full-depth pull, where a capped payee means the run is wrong rather than merely
  bounded -- and it covers a payee that FAILED TO FETCH too, which is more
  incomplete than a truncated one (audit finding: it originally enforced only the
  page-cap half of its own promise). EXIT CODES: 3 = refused to ship (--strict),
  1 = incomplete AND the caller passed `--fail-on-incomplete`, 0 otherwise. The
  non-zero exit is OPT-IN because making it the default was a REGRESSION, caught
  by audit and reproduced before fixing: `scripts/refresh_seed.sh` runs `set -eu`
  at `--max-pages 4`, so the bounded walk that script ASKS FOR killed the
  scheduled refresh at that line every run -- and that refresh is what keeps the
  corpus off the 90-day `stale` cliff, so the safety change disabled the safety
  mechanism. A cap you passed being reached is not a failure; the warning and the
  `truncated` field print either way. `rwa_backfill.collect_paged` still has the
  old shape and says so in its docstring. Tests: `test_chain_backfill.py`, 41
  tests, 30 mutations verified killed (incl. guards for that regression and for
  the discarded-partial defect).
  A TRANSPORT FAILURE MID-WALK IS ALSO TRUNCATION, not an error: the
  exception used to unwind `collect_paged` and DISCARD every page already
  fetched -- measured, 3 good pages became 0 rows ingested and the payee was
  filed as `{"error": ...}`, which downstream is indistinguishable from "this
  payee has no history". Reported by the corpus-depth session against 4add6e6.
  Now the partial is KEPT and marked truncated; only a failure on page ONE
  re-raises, because then there is no partial to label and it is a genuine
  fetch failure. Plus a bounded per-PAGE retry (`--retries`, default 2,
  exponential backoff) on top of `http_util`'s own transient retries: the
  indexer fails ~2% of page fetches when healthy (n=45), so a 5-page walk
  completes only 0.98^5 = 90.4% of the time -- ~27 of 281 payees fetching
  NOTHING per run. Invisible in the shipped corpus because runs accumulate and
  ingest is idempotent; fatal for the ONE-SHOT full-depth pull, which has no
  next run to fill the gap. The retry lives in `_fetch_page`, separate from the
  walk, so it can never advance the cursor past a page it did not read --
  retrying with `params` already rebound would skip history and call the result
  complete. MUTATION-TESTING HAZARD found here: restoring a SAME-SIZE mutation
  lets CPython reuse the MUTANT's `.pyc` (invalidation is mtime+size, and `cp`
  preserves mtime within the second), which can report a phantom failure or a
  phantom SURVIVAL -- clear `__pycache__` between mutations. A second hazard
  found the same way: three mutants survived because no test asserted the
  BACKOFF DELAY -- the guard only gates the sleep, so removing it left retries
  working and every test green, while retrying instantly against a
  rate-limited indexer is what produces the 429s. And a retry with no injected
  clock quietly turned this suite from 0.1s into 12s via one PRE-EXISTING test
  that had no sleep seam),
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
  `x402_challenge.py` (the ONE parser for a 402 challenge -- requirements arrive in
  ANY OF THREE carriers: the JSON body, `WWW-Authenticate: X402 requirements="<b64>"`
  (the v2 style), or a bare-base64 `payment-required:` header (by far the most common
  -- see SCOPE below), and every consumer used to read only the body. Pure+stdlib, tolerant (never raises on
  third-party junk), BODY WINS on disagreement since that is what other x402 clients pay.
  `accepts_from_http_error` reads a raised 402 -- body ONCE, since HTTPError wraps a
  stream and a second read silently downgrades a real challenge to "unreadable".
  Consumed by `directory_liveness` (which it was factored out of), `discovery_crawl`
  (a 402 is a price quote, not a fetch failure -- it carries the SOURCE URL in as the
  parent resource, or the record loses its category and per-resource price key) and
  `clients/x402_pay` (whose `_http_json` now returns response headers instead of
  discarding them). SCOPE, measured not asserted: `hdr_accepts` (the
  WWW-Authenticate form) is only 2 of 195 hosts. The 86 filed as `opaque_402` were
  NOT a different problem, as first believed -- they were a THIRD carrier: probing
  all 86 on 2026-08-28 found 80 serving a complete v2 challenge in a
  `payment-required:` header with `{}` as the body, and the other 6 simply moved or
  gone (400/404/405/410). Implementing that one carrier moved scoreable hosts from
  73/195 (37.4%) to 153/195 (78.5%). These are not demos: api.ipintel.ai, one of the
  80, has 78 distinct payers and 145 settlements -- people were paying endpoints we
  could not read. Matched by EXACT header name; the discovery probe decoded every
  header looking for x402, which is right for discovery and far too permissive to
  ship. Verified live against blockrun.ai (WWW-Authenticate form) and api.ipintel.ai
  (payment-required form), both harvested end-to-end by the crawler.
  Tests: `test_x402_challenge.py test_token_decimals.py`, `test_x402_pay.py`),
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
  `integrations/agentcore/` (AWS Bedrock AgentCore Payments adapter -- gate
  `ProcessPayment` with the verdict BEFORE it signs. AgentCore is GA, speaks x402
  AND MPP, and connects to Coinbase and Stripe/Privy; a payment session constrains
  exactly `limits.maxSpendAmount` + `expiryTimeInMinutes` and NOTHING else -- no
  payee allowlist, no counterparty screening -- and the merchant's `payTo` is
  forwarded VERBATIM into the signature. So it enforces HOW MUCH and never asks
  WHO. GO calls through, HOLD asks a human (refuses by default), STOP withholds so
  no proof is ever generated -- the only durable control, since a returned
  `PROOF_GENERATED` puts the signed payload in the agent's hands. Reads both
  payment types (`cryptoX402.payload`, and MPP's raw `WWW-Authenticate: Payment`
  challenge via `x402_challenge`), and carries `permit2AllowanceLimit` through
  under AWS's own spelling so `upto_scheme` sees the allowance a spend cap cannot.
  Dependency-free core `agentcore_guard.py` + thin `strands_plugin.py` /
  `langgraph_middleware.py`; own tests run from that dir),
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
  `upto_scheme.py` (the x402 `upto` (metered) scheme and the Permit2 allowance it
  requires. `exact` moves a fixed amount via EIP-3009 and the signed authorization IS
  the exposure; `upto` quotes a CEILING, meters below it, and settles through Permit2
  `transferFrom` -- so the wallet must first grant an ERC-20 allowance, a SEPARATE and
  LONGER-LIVED exposure that no spending control in this market can see. AWS Bedrock
  AgentCore Payments (GA, x402+MPP) enforces `limits.maxSpendAmount` plus an expiry and
  NOTHING else -- no payee allowlist, no counterparty check -- and an allowance is not a
  spend, so a $1 session budget coexists with an approval over the whole balance. Its own
  docs offer granting an UNLIMITED allowance as a normal option and note `approve` SETS
  rather than adds. That is the drainer pattern `calldata.py` already hard-STOPs as
  calldata, so this recognizes it arriving as a payment INTENT instead. UNLIMITED -> hard
  STOP (reuses calldata's `UNLIMITED_MIN` so the two cannot drift on what "unlimited"
  means); SCREEN SELECTION CORRECTED (2026-08-30, reported by the cold-start
  session, confirmed here end to end before accepting): this keyed off
  `is_upto(scheme)`, but PERMIT2 IS USED WITH `exact` TOO -- advertised as
  `extra.assetTransferMethod: "permit2-exact"` -- so an UNLIMITED allowance on an
  `exact` payment was screened NOT AT ALL: not gated, not warned, not recorded, and
  measured returning a clean GO. The exposure is created by the ALLOWANCE, not the
  scheme name, so the screen now runs whenever an allowance is actually stated,
  whatever the scheme calls itself; absent stays `not_applicable` (`unknown` for
  `upto`). THREE tests encoded the old behaviour and were replaced, one of them
  asserting the opposite outcome on the wrong rationale that the field is
  "meaningless for `exact`". Boundary re-measured: only genuinely unlimited
  approvals STOP; 1x/3x/99x/101x/10^6x all stay GO with the ratio lock off, so the
  widening adds no false-positive class. The GRADUATION RULE for `EXCESSIVE_GATES`
  is also corrected: the shipped corpus can NEVER supply it, because an allowance is
  PAYER-side -- it appears in a ProcessPayment request, never in a 402 challenge --
  so the honest gate is N real requests through the API, not corpus observation.; >100x the ceiling -> `excessive`, which escalates GO->HOLD behind the
  reversibility lock `EXCESSIVE_GATES` (DEFAULT OFF -- advisory until flipped;
  approving once and metering many calls under an approval is normal use, so the
  false-HOLD rate wants measuring on the shipped corpus first, the way sybil_ring
  graduated. AUDIT: this line previously claimed "HOLD only" and the code did NOT
  hold -- `excessive` went into `warnings`, and forecast only extends `reasons`
  with warnings, so a 10^6x disproportionate allowance returned GO with a note.
  Confirmed live before the fix); absent/unreadable -> no gate, FAIL-OPEN. Reads the AgentCore spelling
  `permit2AllowanceLimit` (nested or top-level) as well as our own. Also FIXED a dormant
  inversion in `x402.payment_satisfies`: the non-`exact` branch demanded `value >=
  required`, exactly backwards for a ceiling. Unreachable because we only ever issue
  `exact` ourselves -- which is why it survived. Pure+stdlib; imported lazily by x402 to
  break the upto->calldata->x402 cycle. Tests: `test_upto_scheme.py`),
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
  `directory_liveness.py` (does the ecosystem map still RESOLVE? `ecosystem_scan` writes
  `data/directory.json` from what payees ADVERTISE; this probes every distinct host and
  reports the answer in the terms that matter -- not "is it up" but "can we PARSE its
  payment requirements", since `forecast` scores from the challenge's `accepts[]`.
  Classes: body_accepts / hdr_accepts / wellknown / opaque_402 / other / dead / blocked.
  Guards two artifacts that both UNDERCOUNT the live ecosystem, each a real error made
  while running the survey by hand: GET-only probing (a 405 is a POST endpoint, not a
  dead one -- the retry recovered 14 scoreable hosts) and body-only challenge parsing
  (the x402 v2 style carries requirements in `WWW-Authenticate: X402 requirements=`).
  FINDING (CLOSED): nothing in this repo read that header -- every consumer took `accepts`
  from the body -- so a v2 endpoint was uncrawlable and unpayable though the engine would
  score it fine. `parse_challenge` now lives in `x402_challenge.py` and this module
  delegates to it, so survey, crawler and paying client agree on one parser. Measured
  2026-08-18: 73/195 hosts live+scoreable, 86 serving an opaque 402. Pure helpers +
  injected network; `rank_leads` is prioritisation only and NEVER touches a verdict.
  See `docs/DIRECTORY_LIVENESS.md`. Tests: `test_directory_liveness.py`),
  `advertised_prices.py` (supply each payee's OWN advertised price bounds to the verdict --
  the second arm of `price_stop_is_corroborated`. `ecosystem_scan` already writes
  min/max per payee to `data/directory.json`; this turns that artifact into a
  reputation source composing through `merge_records`, which had to learn to carry
  the pair (it builds a FIXED dict, so unknown keys were silently dropped -- the
  reason the arm was inert). Contributes ONLY the bounds: no settlements, no payer
  counts, so it cannot move the reputation/Sybil/thin gates. MONOTONICALLY PERMISSIVE
  (it only ever withholds a STOP). AUDIT FINDING (fixed): the range is NOT trustworthy
  ALONE -- discovery_crawl derives it from the payee's OWN advertised
  `accepts[].maxAmountRequired`, so it is attacker-authored content we merely harvest,
  and [min,max] is the HULL of a price list, not the list. Letting the hull vouch by
  itself waved through 89/111 top-of-catalog quotes at >=8x the settled median, the
  worst with ZERO settlements near the price. The catalog now only LOWERS the tier
  arm's payer floor (2 -> 1) and still requires real NON-SELF settlement evidence at
  the quoted price. Remaining safeguards: loaded at startup from our committed crawl
  -- never the request -- and it never reaches GO. The pair is ATOMIC across sources: a
  min from one and a max from another would synthesize a range no catalog advertises.
  Address keys are lowercased because a live 402 returns an EIP-55 CHECKSUMMED payTo
  while the crawl stores lowercase (that join silently missed 64 of 69 live endpoints).
  Fail-open: missing/corrupt artifact -> empty index -> "unknown", never "out of range".
  Measured: takes live STOPs 3 -> 0 with ZERO attacks escaping at >=8x. Tests:
  `test_advertised_prices.py`),
  `receipt_signer.py` (INDEPENDENTLY-VERIFIABLE receipts -- the Ed25519 half.
  `sign_receipt`'s `receipt_id` is HMAC: a fine audit-trail id and ledger join key,
  but SYMMETRIC -- verifying needs our secret, so only we can check it and anyone
  holding it can forge. Blackwall nonetheless advertised "an independently-verifiable
  Ed25519 signed receipt"; this makes that true. Envelope is byte-compatible with
  Traceipt's (`{protected,payload,signature}`, signing input =
  canonical_json({"payload","protected"}), kid = first 16 hex of sha256(pubkey)), so
  ONE verifier covers both products -- `clients/traceipt-verify` reads this shape
  already. STDLIB-ONLY: signs via `cdp_auth.ed25519_sign` (RFC 8032, hashlib only,
  SIGN-ONLY by design -- exactly right, since we sign and third parties verify).
  `build_claims` curates WHAT is signed: not the whole verdict, because `signals` is
  a large version-unstable blob carrying floats, and floats have no canonical JSON
  form -- `score` is emitted as a decimal STRING (signing the raw verdict made
  canonical_json raise, and fail-soft then dropped EVERY receipt while the service
  looked healthy). KEY HANDLING: no dev-key fallback (a receipt signed with a
  committed key is WORSE than none -- it looks verifiable), fail LOUD at boot on a
  malformed seed (set-but-bad means the operator intended signing), and
  BLACKWALL_RECEIPT_KEY is explicitly refused as the seed. Served at BOTH
  `/jwks.json` and `/.well-known/blackwall-receipt-key.json` -- the latter is the URL
  `blackwall-mcp-remote` already fetches, so serving it fixes an existing broken
  dependency. Retired keys stay published so receipts survive rotation. Opt-in via
  `BLACKWALL_SIGNING_SEED`; absent -> no `receipt` field, which is honest.
  AUDIT FINDINGS (fixed): the pure-Python signer is CORRECT (3/3 RFC 8032 vectors)
  but (1) added 48x latency -- measured 3.6ms -> 172.6ms end-to-end -- and (2) is
  VARIABLE-TIME: `_scalarmult` adds only on SET bits, so runtime tracks the scalar's
  Hamming weight (weight 1 -> 55ms, 126 -> 82ms, 253 -> 109ms). That scalar is the
  Ed25519 nonce r = H(prefix||msg), and leaking it recovers the private key --
  farmable by an unauthenticated caller. Fixed with a PLUGGABLE BACKEND that prefers
  a native constant-time library (`cryptography` if installed, ~1000x faster) and
  falls back to pure Python; plus a SAFE-BY-DEFAULT guard -- the server REFUSES to
  boot with signing enabled on a PUBLIC bind using the variable-time backend unless
  `BLACKWALL_ALLOW_SLOW_SIGNING=1`. Also `typ` is `blackwall-verdict+json`, NOT
  Traceipt's `x402-receipt+json`: a verdict is a different claim from a receipt, and
  a shared label would let a verifier trusting both issuers' keys accept one for the
  other. Post-quantum (ML-DSA-65 hybrid) is phase 2 -- see `docs/RECEIPT_SIGNING_SCOPE.md`.
  Tests: `test_receipt_signer.py`, incl. cross-verification under Node WebCrypto),
  `payload_sim.NON_USD_ASSETS` / `is_non_usd` (WHICH corpus assets are not US
  dollars -- the companion to the decimals table, because knowing an asset's
  SCALE is not knowing its PRICE and the two mistakes have the same shape.
  `decide_payment` compares the amount to a DOLLAR threshold
  (`HOLD_AMOUNT_THRESHOLD`, and whatever a treasury deployment raises it to);
  for a non-dollar asset that comparison is meaningless. MEASURED before the
  fix: 5.00 SOL -- roughly $500 -- returned a clean GO while 50.00 USDC (~$50)
  correctly escalated. That is the GUSD spending-cap bypass from
  docs/DECIMALS_AUDIT.md again, by CURRENCY rather than by decimals. JPYC and
  EURC were already in the corpus and harmless by luck (yen numbers are large so
  they err toward HOLD; EURC is near parity); SOL, added 2026-09-05, is the
  first where the error is large AND unsafe (~100x understated). NOT a
  conversion -- a hardcoded rate is stale the day it is written -- so a known
  non-dollar amount simply cannot auto-approve: HOLD-only, `blast_radius` reads
  `unknown` rather than `bounded`, and an UNRECOGNIZED asset is never gated,
  since "not known to be USD" is not "known not to be USD". Blast radius
  measured at 5 of 371 live quotes. `asset_coverage.NON_USD` is the SAME object,
  not a second copy. Redteam: 1 attack + 1 restraint control),
  `approvals.py` (the HUMAN-IN-THE-LOOP half of a HOLD. Every gate here is
  HOLD-only by design -- it refuses to auto-approve and hands the question to a
  person -- but the engine had nowhere to hand it TO, so every integration got
  "HOLD" back and had to invent the workflow, which is how a HOLD ends up
  configured away. FOUND BY COMPETITIVE RE-VERIFICATION, not imagination:
  TollWarden's live spec (paysafe-agent.com/openapi.json v1.5.0, re-pulled
  2026-09-05) ships `/v1/approvals/config` + `/v1/approvals/{id}`; it was the one
  thing in their product this engine had no answer to, and it is a WORKFLOW gap
  rather than a detection gap -- the kind a detection-focused project fails to
  notice about itself. An approval is NOT an upgrade: `redeem` returns "HOLD,
  approved by a human", never GO. FIVE SECURITY PROPERTIES, each a bypass if
  skipped: (1) STOP IS NEVER APPROVABLE -- `APPROVABLE` is a frozenset of HOLD
  alone, and a HOLD carrying `hard_stop` is also refused as incoherent; (2)
  BOUND TO THE EXACT CLAIM via a digest over `BOUND_FIELDS` (counterparty,
  amount, asset, chain, payer) -- without it, getting $0.05 approved authorizes
  $500, i.e. the mechanism becomes a laundering step. Deliberately NOT the whole
  claim, so extra context does not invalidate a human's answer; digest is
  case/whitespace-normalized because a live 402 returns EIP-55 while crawls store
  lowercase (the join that missed 64 of 69 endpoints in advertised_prices); (3)
  SINGLE USE -- redeeming consumes it, and a FAILED redemption does not burn it;
  (4) EXPIRES (`DEFAULT_TTL` 900s, matching the AgentCore session window),
  compared with >= so it dies ON its expiry second; (5) OWNER-ONLY via the same
  HMAC capability pattern as `sign_report_token`, domain-separated with
  "approve:" so a report token can never authorize a payment. `public_view`
  withholds the digest (BOUND_FIELDS is short and low-entropy, so publishing it
  would let an id-holder brute-force amounts and payees) and the token. Terminal
  states are final -- a retry loop cannot grind an approval out of a decline.
  Store is injected; `MemoryApprovalStore` is bounded and evicts OLDEST first,
  which fails safe; a restart loses pending approvals, which also fails safe.
  Served at POST `/v1/approvals`, POST `/v1/approvals/decide`, GET
  `/v1/approvals/{id}`; a wrong token gets 403 for a REAL id and an unknown one
  alike, so the endpoint is not an enumeration oracle. BINDING HAZARD: this is a
  STORE, not a `*_source`, so `test_honeypot`'s parity guard did NOT cover it --
  `test_approvals` widens the property to every PUBLIC attribute a handler
  method actually reads off `self`, which needs no naming convention.
  AUDIT FINDINGS, all four fixed, and the first is the one that matters:
  (1) HIGH -- `redeem` was implemented, unit-tested and CALLED BY NOTHING on the
  wire, so properties 2 and 3 were unreachable: a caller polled, saw "approved",
  and proceeded, and an approval for $0.05 authorized anything. The
  wired-and-inert pattern, in a module written the same hour as a test class
  about that hazard. `POST /v1/approvals/redeem` makes it reachable, and
  redeeming requires the token because spending is not a read. (2) HIGH, a CLAIM
  rather than a code defect -- the docstring said "a record that a HUMAN was
  asked". The engine CANNOT KNOW THAT: the token goes to whoever opened the
  approval, and if that is the agent it can approve itself in the next call
  (measured: 40ms). What this actually provides is a SECOND, EXPLICIT, AUDITED
  act naming an `actor`, bound to the payment, expiring, single-use; whether a
  person performs it is the INTEGRATOR's job -- give the token to the approval
  UI, not to the agent. Said plainly instead of implied. (3) MEDIUM -- the store
  evicted the OLDEST row regardless of state, so a flood flushed a live PENDING
  approval out (measured: six opens against a limit of three erased the victim).
  Terminal rows are evicted first and the store then REFUSES with 503 rather
  than dropping a live question. (4) LOW->MED -- any caller-supplied verdict was
  accepted, so an approval could be opened for a payment the engine never
  scored; a `receipt_id` now requires the matching `report_token`, reusing the
  existing HMAC.
  (5) MEDIUM, found while auditing the fix for the audit trail itself --
  `decided_by` and `reasons` are BOTH caller-supplied and BOTH echoed to an
  unauthenticated poller, and neither was sanitized. Newlines, carriage
  returns, NUL and ANSI escapes passed straight through, so an ops console or
  plain-text log rendering an approval could be made to show lines nobody
  wrote, and the field whose whole job is to say WHO approved could claim to be
  someone else (`"alice@corp\n  approved-by: security-team"`). THIRD instance
  of this defect class here -- `payee_syntax` echoed a merchant-controlled hint
  into `reasons[]` raw, and `secret_scan` exists because free-text fields reach
  places that render them. `_safe_text` escapes both the same way (repr minus
  its quotes: control characters become visible escapes, ordinary text stays
  completely readable). And the audit trail was UNREADABLE before that: `decide`
  stored the actor and `public_view` withheld it, so the mitigation this module
  offers in place of enforced human review left no evidence it had happened --
  found on PRODUCTION, because the tests asserted `decided_by` on the record and
  never on the view.
  Tests: `test_approvals.py`, 47 tests incl. a REAL server, 15 mutations
  verified killed -- including the seventh-edit binding omission, the removal
  of the redeem route, and un-sanitizing either echoed field),
  `confidence.py` (how much EVIDENCE backs a verdict -- `assess_confidence(record,
  signals)` -> {level high/medium/low, score 0..1, backed_by[], missing[]} across
  five weighted dimensions: history depth, payer breadth, cross-counterparty
  corroboration, outcome/dispute depth, freshness. PURE + DESCRIPTIVE -- never
  changes the verdict; folded into every `decide_payment` response as `confidence`
  so a caller can tell a GO on real history from a cold-start default),
  `asset_coverage.py` (writes `data/asset_coverage.json`, the COMMITTED census
  behind every prevalence claim about the ecosystem -- added because the
  AgentCore demo asserted "10 of 12 endpoints quote `exact`" from a number no
  committed artifact could reproduce, in the most public place we make claims;
  does the decimals table still cover what the ecosystem
  QUOTES? `KNOWN_DECIMALS_BY_CHAIN` is a SNAPSHOT of one day's corpus; an asset
  missing from it resolves to unknown -- safe, but the amount check is off for
  that payment, and nothing told us when that started. One pass over the live
  hosts answers three questions: COVERAGE (which (network, asset) pairs we cannot
  scale, with the hosts that introduced them -- the work list), DRIFT (a BROKEN
  identifier is separated from a merely unknown one), and SANITY (with the table
  applied, does every quote land at a plausible price? a wrong entry shows up as
  an absurd implied price -- this is how the corpus corroborated Stellar's 7).
  Also CENSUSES the payment schemes and Permit2 transfer methods the corpus
  advertises, with the rows behind the count -- added because a cross-session
  prevalence claim could not be reproduced from any committed artifact and the
  receiving session had to take it on trust. Measured 2026-09-05: `exact` 363,
  `upto` 4, `batch-settlement` 3, `aggr_deferred` 1; and 13 entries on 7 hosts
  (CoinMarketCap and Nansen among them) advertise a Permit2 transfer method, 9
  of them on `exact` -- which is why `upto_scheme` screens the ALLOWANCE rather
  than the scheme name. Those figures move: they are the LIVE ecosystem, so the
  committed `data/asset_coverage.json` is dated and `test_agentcore_guard`
  asserts the demo's copy of them against it -- a stale artifact fails as a
  tripwire rather than passing as a fact.
  DELIBERATELY does NOT resolve on-chain and write the table: that table gates
  payments, and a scale from a single public RPC is a value that RPC's operator
  chose, so resolution stays a REVIEWED step (read every public RPC the chain
  lists, require agreement -- see docs/DECIMALS_AUDIT.md). Reuses
  `payload_sim.known_decimals` as the injected resolver (so the report is the
  ENGINE's answer, not a reimplementation) and `upto_scheme.parse_ceiling` for
  the atomic-vs-human rule (load-bearing there, must not drift). Exits 1 when a
  person should look, so a scheduled run is actionable without reading it. FIRST
  LIVE RUN found two seller bugs on one host: a BSC asset truncated to 39 hex
  chars, and a Solana `payTo` with `FACILITATOR_URL=https://...` concatenated
  onto it -- the address an agent would PAY. Both fail safe today for INCIDENTAL
  reasons, which is not the same as being detected: the truncated asset resolves
  to unknown decimals, and the glued payee is simply an unknown counterparty, so
  it draws a cold-start HOLD. Neither is recognised as malformed by the engine --
  `normalize_address` is applied to `payer` ONLY, never the counterparty, which
  gets `is_evm_address` purely to decide whether to lowercase. Measured: the glued
  payee and a clean Solana payee return byte-identical verdicts. Stated precisely
  because the earlier wording implied a validation layer stands between a
  malformed payee and a signature, and the next person to rely on that inherits a
  gap that reads as covered. CLI:
  `python asset_coverage.py data/liveness.json [--json report.json]`.
  Tests: `test_asset_coverage.py`),
  `payee_syntax.py` (is the address the agent is about to PAY a possible address?
  Found in the wild by `asset_coverage` on 2026-08-30: a live seller advertised a
  Solana `payTo` with `FACILITATOR_URL=https://...` concatenated onto it -- almost
  certainly a missing newline in a `.env` -- and a payment there cannot arrive.
  SINCE FIXED BY THE SELLER (2026-09-05), and the three-step road there is the
  point: probe 1 found 0 malformed among 175 answering (20 silent) -> "fixed or
  gone quiet, unknown which"; probe 2 found the host, `apiwitchcraft.duckdns.org`,
  back up and STILL advertising it -> "it went quiet, never fixed"; the monthly
  run found 0 again, but this time the host ANSWERS and its `payTo` reads clean,
  VERIFIED against the host rather than inferred from the count. What separates
  fixed from silent: that seller had TWO defects and repaired one -- its 39-hex
  BSC asset is still reported, on the same host in the same run, which is the
  proof the host answered. A silent host and a healthy one produce the same
  absence of findings, which is why every run leads with how many answered. The
  gate is not weakened by its motivating case being repaired: one seller fixing a
  `.env` does nothing about the next, and the corpus still carries a malformed
  identifier today. In `data/asset_coverage.json`. The engine
  could not tell it from a clean one: MEASURED, that payee and a clean Solana
  payee returned BYTE-IDENTICAL verdicts, both HOLD because the counterparty was
  UNKNOWN rather than impossible. That HOLD clears the moment the payee has
  history, and a broken address does not get better with settlements. TWO GRADES,
  split on EVIDENCE not taste: `malformed` (content that cannot appear in an
  identifier on ANY chain -- `://`, `=`, and any whitespace or non-printable
  character) GATES, because that is the
  case found in the wild; `invalid_hex` (`0x` but not a valid EVM address) is
  RECORDED and does NOT gate, because 0 of 292 real payees exhibit it, its only
  real instance was an ASSET field, and gating it failed 15 tests across 8 modules
  -- every one a synthetic placeholder like `0xKNOWNGOOD00...`. A rule whose only
  hits are fixtures is not ready to refuse a payment. Chain-agnostic on purpose:
  no base58/base32 guess that would condemn real Solana, Stellar and Algorand
  payees. AUDIT FINDINGS (both fixed): the impossible-content rule was a literal
  tuple of ASCII spaces, so it was ASCII-ONLY -- a NON-BREAKING space (a Windows
  `.env`, a copy-paste out of a rendered page) or a ZERO-WIDTH space glued a URL
  onto an address and graded `unknown`, which does not gate; the same shape as the
  case found in the wild, walking straight through. Now `isspace() or not
  isprintable()`, which also covers NUL, the bidi overrides (a lookalike-address
  trick in its own right) and the zero-width characters, measured at 0 additional
  corpus flags. And the redacted `hint` went into `reasons[]` RAW, so a payee
  carrying a newline forged a line in any plain-text log printing a reason (JSON
  escapes it; a terminal does not) -- now escaped. FALSE-FLAG RATE MEASURED BEFORE
  SHIPPING IT ON, the way sybil_ring graduated: 0 of 292 DISTINCT real payees.
  AUDIT CORRECTION: this first said "0 of 558" by ADDING directory.json (266) to
  the seed manifest (292), two sets that are nearly the SAME set -- the union is
  292, so the claim overstated its own evidence 1.9x. `test_payee_syntax.py` now
  COMPUTES the union from committed artifacts rather than restating a number, on
  the principle that a prevalence claim another session cannot reproduce is worth
  nothing. HOLD-only, never STOP
  (defensible but declined pending real request traffic), fail-open, pure, 1.5us.
  Redteam: 2 attacks (the glued payee, the non-breaking-space evasion) + 1
  restraint control (a raw base58 Solana payee must not be condemned). ALWAYS-ON,
  so it is advertised in `discovery.py`'s signal list -- it was missing at first
  because #42 and #43 landed in parallel, which is why `test_discovery.py` now
  DERIVES that list from a bare verdict instead of restating it. Also the fifth
  scenario in `integrations/agentcore/demo.py`, the sharpest form of that demo's
  claim: AgentCore forwards `payTo` VERBATIM into the signature, so it never asks
  whether the payee is an address at all.
  Tests: `test_payee_syntax.py`),
  `bounded_server.py` (ADMISSION CONTROL -- a ceiling on requests IN FLIGHT.
  MEASURED on the live free deploy: at 120 concurrent, 43% of verdicts failed
  while p50 stayed FLAT at ~2s. The service was not getting slow, it was
  DROPPING work -- as an edge 502, which a caller cannot distinguish from
  "broken". `ThreadingHTTPServer` is thread-per-request with no cap.
  DIAGNOSIS, measured not assumed: `/healthz` served 100/100 concurrent cleanly
  while verdicts shed 26% at 80, so the saturating resource is per-request
  COMPUTE, not connections. But the verdict is only 3.4ms server-side (2.05ms
  profiled locally) -- the real constraint is that a Render free instance is
  ~0.1 CPU, so 3.4ms of work costs ~34ms of wall clock: ~29/s theoretical,
  ~14/s measured. NOTHING IN OUR CODE CHANGES THAT ORDER OF MAGNITUDE, and this
  module does NOT claim to: it adds ZERO throughput. What it changes is the
  SHAPE of overload -- admitted requests keep their latency, excess ones get an
  immediate honest `503` + `Retry-After` instead of being timed out into a 502.
  For a PAID endpoint that is the difference between a caller retrying and a
  caller concluding the service is down. BoundedSemaphore (not Semaphore) so an
  unbalanced release raises instead of silently restoring the unbounded
  behaviour; acquire BEFORE the thread is spawned; release in
  `process_request_thread`'s finally, the one place that runs for every admitted
  request. TWO BUGS FOUND BY RUNNING IT, both invisible to the unit tests:
  (1) 50 concurrent POSTs produced 5 TRANSPORT ERRORS (3 broken pipe, 2 RST) --
  socketserver's default listen backlog is 5, and closing a socket that still
  holds unread inbound data makes the kernel RST away the 503 we just wrote;
  fixed with `request_queue_size=256` + half-close-and-drain, measured 5 -> 0.
  (2) that drain then ran ON THE ACCEPT-LOOP THREAD, so shedding stalled new
  accepts by up to 0.5s EACH -- load-shedding as a self-inflicted outage; moved
  to short-lived capped threads, measured /healthz at 1-2ms while 31 requests
  shed. HONEST TEST LIMITATION, found by mutation testing and left documented
  rather than papered over: the backlog and RST fixes are NOT killed by any unit
  test -- loopback accepts too fast to overflow a backlog and a 200KB body fits
  in local socket buffers, so neither condition reproduces. Their evidence is
  the real-path measurement, and `test_every_client_gets_an_HTTP_RESPONSE_not_a_reset`
  says so in its docstring instead of implying coverage it does not have.
  PRE-DEPLOY AUDIT found a HIGH one this had introduced: `/healthz` went through
  the SAME ceiling, so under saturation a health check could be shed with 503 --
  and a platform that restarts an instance on a failed health check turns
  load-shedding into an OUTAGE, strictly worse than the 502s being replaced. It
  measured clean live (25/25 while 34 verdicts shed) purely by timing luck, since
  verdicts are 3.4ms and permits turned over between probes. Health is now exempt
  via a NON-BLOCKING MSG_PEEK at bytes the kernel already holds (never waits --
  blocking on the accept loop is the same mistake as the drain above), and the
  exemption is ITSELF capped (`MAX_EXEMPT_INFLIGHT`) because an exemption is not
  a bypass: a flood of GET /healthz would otherwise restore unbounded threads.
  Verified under REAL saturation: ceiling=1 with 50 flooding threads (2391 shed,
  2318 served) and 40/40 health probes returned 200.
  Also fixed: `_refusing` leaked if `Thread.start()` raised, so after
  MAX_REFUSE_THREADS such failures NO refusal would ever drain again and the
  RSTs returned permanently.
  Ceiling via `BLACKWALL_MAX_INFLIGHT` (default 40, the last clean rung
  measured); always re-measure on the box you actually run on.
  Tests: `test_bounded_server.py`, 7 tests, 5 of 7 mutations killed (the 2
  unkillable ones named above)),
  `remote_ledger.py` (durable ENCRYPTED mirror of the append-only verdict ledger --
  the answer to "no persistent disk". MEASURED on the live free-tier deploy, not
  assumed: the SQLite reputation store needs NO durability (its only writer,
  `reputation_store.ingest_from_chain`, is gated behind `BLACKWALL_INGEST=0`, and
  five payees' settlement/distinct-payer counts came back byte-identical to the
  baked 46,031-row seed), while the LEDGER is the only thing that accumulates --
  and `aggregate_counterparties` folds it into the recency-weighted
  `recent_dispute_rate` behind `going_bad`. So the problem is not "persist a
  database", it is "persist an append-only log". Subclasses `EventLedger` and
  overrides exactly TWO things -- the single write point (`_append`) and boot
  (`hydrate`) -- so every reader keeps reading the LOCAL file unchanged.
  AES-256-GCM per record with a random nonce and the envelope version bound as
  AAD; key DERIVED (`HMAC-SHA256(secret, label)`) not used raw, and `load_key`
  refuses a secret reused from the signing seed / receipt key. AT-LEAST-ONCE
  DELIBERATELY: a duplicate row is harmless because settlements dedupe by tx hash
  (`ledger.py:134`), while a LOST row erases an outcome and a missing dispute
  makes a bad counterparty look BETTER than it is -- so a transient failure is
  retried. FAIL-OPEN on the payment path (local write first and unconditional;
  one serialized worker; failures counted, never raised; a bounded queue with
  `put_nowait` so the durability feature cannot OOM or block the service it
  protects). NO PLAINTEXT FALLBACK: AES-GCM is not stdlib and this does not
  hand-roll one -- if the cipher is unusable the service REFUSES TO BOOT (exit 2).
  AUDIT FINDING, found by RUNNING it against a genuinely broken `cryptography`
  install rather than by reading the code: "installed" is not "working" -- a
  broken native build imports fine then raises `pyo3_runtime.PanicException`,
  which derives from `BaseException`, so `except Exception` did NOT catch it; it
  escaped every fail-open guard and surfaced as a 500 on the payment path. Now
  `_guard` converts it (passing KeyboardInterrupt/SystemExit through) and
  `ensure_cipher()` proves the cipher round-trips AT BOOT. Restore is BYTE-EXACT
  (`seal` serializes exactly as `_append`), so an operator can verify with `diff`.
  Verified end to end against a real HTTP KV: 16 events restored byte-for-byte
  across a full container wipe, with zero plaintext (not the counterparty, amount,
  asset, outcome, or even the JSON field names) visible to the provider. SHARP
  EDGE: lose `BLACKWALL_LEDGER_KEY` and the log is unreadable -- rows under an old
  key are skipped, counted, and announced in the boot banner.
  LIVE AUDIT (2026-09-07), found by the mirror writing NOTHING while every log
  line looked healthy: (1) a Redis-REST store reports a COMMAND-level failure in
  the response BODY with HTTP 200 -- a READ-ONLY token, NOPERM, WRONGTYPE, a
  quota refusal -- and `_cmd` read only `result`, so each became `None`; `append`
  ignores its return, so `_mirror` counted the record MIRRORED. Silent total data
  loss with the banner still reading ON, which is exactly the failure this module
  exists to prevent. An `error` body now raises, naming the command and quoting
  the store. (2) the banner asserted a capability it never tested: `hydrate` only
  READS, so a read-only token / wrong database / revoked permission all boot
  clean. `verify_writable()` probes a real write at boot (`SET <list_key>:probe
  ... EX 60` then `GET` -- a separate self-expiring key, never the log) and the
  banner reports DEGRADED -- NOT WRITABLE instead of ON. NOT fatal: one probe
  cannot separate a bad token from a KV outage, and taking the payment path down
  over a third party is the worse error, so it warns and keeps serving. Verified
  end to end against a read-only stub (DEGRADED banner + named cause + verdict
  still served + local row kept) and a healthy one (ON, 3 local == 3 mirrored,
  probe key absent from the log).
  PRE-DEPLOY AUDIT, two more: (1) `close()` was implemented, unit-tested and
  CALLED BY NOTHING -- the wired-and-inert pattern again -- so every record still
  queued at shutdown was lost, and a REDEPLOY is exactly when that queue is
  non-empty; and the obvious fix would have been inert too, because it only ran
  on KeyboardInterrupt while a platform stops a container with SIGTERM. Both
  wired; verified 12/12 rows mirrored on a real SIGTERM. (2) the KV response was
  read with an unbounded `r.read()` -- the store is a THIRD PARTY, and a broken
  or hostile one could be buffered straight into a 512MB box; `http_util.py`
  caps its reads for exactly this reason and this path did not. Capped at 64MB,
  with a restraint control so an over-tight cap cannot silently disable
  mirroring. See
  `docs/DURABLE_LEDGER.md`. Tests: `test_remote_ledger.py`, 48 tests, 31 mutations
  verified killed),
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
  `honeypot.py` (the EXIT check -- can the agent SELL what it is about to buy? Every
  other acquisition gate asks whether the BUY clears (`rwa_readiness`: may the receiver
  hold it; `settlement_sim`: will the stablecoin leg settle; `holder_concentration`: can
  one wallet dump on you; `dex_price`: is the price real). None asks whether the position
  can be EXITED, which is the whole honeypot mechanic: buys perfectly, cannot be sold.
  THE DISCRIMINATOR is the control simulation, and it is why this may gate where
  `revert_scan`'s bare revert axis may not -- that axis tried to downgrade BLACKROCK
  because BUIDL rejects non-allowlisted wallets, i.e. because it works AS DESIGNED, so
  `REVERT_AXIS_GATES` stays off. A transfer revert alone cannot tell a trap from
  compliance. The rule that can: A RESTRICTION THAT PERMITS AN ARBITRARY FRESH WALLET
  AND FORBIDS THE MARKET IS NOT COMPLIANCE, IT IS A TRAP. So the flag requires
  `RECEIVER_BLOCKED` from `transfer_sim.attribute` -- the token's own deepest USDC pool
  reverts while a fresh control EOA succeeds. A permissioned security blocks BOTH
  (nothing is allowlisted) -> `SENDER_BLOCKED` -> reported `restricted` and DEFERRED to
  rwa_readiness, never called a scam; verified as a redteam CONTROL, not just a unit
  test. The revert CLASS is deliberately not consulted on the receiver path: a honeypot
  is free to borrow compliance-shaped wording, and "allowlisted only, except any wallet
  at all, except the pool" is not a coherent posture. SECOND, SOFTER AXIS: round-trip
  retention (quote buy then sell through the same pool) catches the token that IS
  sellable but takes 95% on the way out -- behind the reversibility lock
  `SELL_TAX_GATES`, DEFAULT OFF (legitimate fee-on-transfer tokens exist; the threshold
  wants measuring on a real corpus first, the way `EXCESSIVE_GATES` and `SYBIL_RING_GATES`
  graduated). HOLD-only, NEVER STOP (this is inference from a simulation, not proof --
  sanctions and payload-mismatch keep the STOP authority), FAIL-OPEN everywhere: no pool,
  no holder, or an unreachable RPC all return `unknown`, and an unlisted token is not a
  honeypot, it just has no market to probe. Reuses `transfer_sim` (simulation +
  control attribution) and `dex_price.best_pool` (deepest-pool discovery, made public for
  this). Opt-in `BLACKWALL_HONEYPOT=1` + an EVM RPC; folded via `honeypot_source`.
  Measured: baseline GO -> HOLD on a honeypot, GO preserved on a permissioned security.
  HOT-PATH COST, measured not assumed: pool discovery is one eth_call PER FEE TIER plus
  the simulation, so a DEGRADED (hanging) RPC cost 7.53s end-to-end at the 2.5s default
  -- bounded to 1.5s per call for the source this constructs itself. A payment with no
  `acquires` never reaches the source at all (1.6ms, unchanged). The HEALTHY-path cost is
  NOT measured: doing that honestly needs a real node, not a loopback stand-in.
  BINDING HAZARD found here and now guarded by a STRUCTURAL PARITY TEST: adding a source
  takes SEVEN edits, six of them signatures and the seventh a DICT LITERAL in
  `serve_forever`'s `_BoundHandler`. Omitting that seventh raises nothing -- the handler
  keeps its `None` default, the check never runs, and the startup banner still announces
  it as ON. That is exactly what happened here: unit tests passed, redteam passed, and
  the live endpoint answered in 7ms because it was doing nothing. `test_honeypot.py`
  asserts the PROPERTY (every `*_source` on `_Handler` is bound in `serve_forever`) so
  the next source added cannot repeat it.
  Tests: `test_honeypot.py`, 31 tests, 8 mutations verified killed),
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
  EXISTING verdict path with zero new plumbing. Wired FIRST in the server's
  `CombinedRwaReadinessSource` (that class takes the FIRST non-None signal), with
  `BlockscoutHolderLookup` supplying the funded sender a live request never carries --
  otherwise the source silently answers "unknown" for everything. It returns None rather
  than an `unknown` signal precisely so it DEFERS to the interface probe / RAMS axis
  instead of shadowing them. Verified live: STBT + BUIDL -> blocked with
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
 test_auth_sim.py test_directory_liveness.py test_price_corroboration.py test_advertised_prices.py test_deploy_manifest.py test_receipt_signer.py test_x402_challenge.py test_x402_pay.py test_screen_payer.py test_mcp_http.py test_upto_scheme.py test_asset_coverage.py test_payee_syntax.py test_honeypot.py test_remote_ledger.py test_bounded_server.py \
 test_approvals.py test_ci_coverage.py test_seller_intel.py test_solana_backfill.py test_token_decimals.py test_user_agent.py test_volume_integrity.py
```

`clients/demo_flywheel.py` demonstrates the verdict->outcome->reputation->verdict loop
end to end (LABELED SIMULATION -- real EIP-3009 signature on the payment leg via
eth-account, settlement mocked; a funded round-trip is the operator's to run): a
merchant Blackwall knows nothing about earns GO purely from its own settled verdicts,
then loses it (going_bad) when recent outcomes turn to disputes. Guarded by
`test_demo_flywheel.py`.

`redteam.py` is the adversarial coverage scorecard: it drives a battery of attacks +
legit controls through the engine and derives each disposition (CAUGHT / KNOWN GAP /
CLEAN / FALSE POSITIVE / MISS). TWO families: `SCENARIOS` runs through `decide_payment`
(reputation/price/Sybil core), and `SIM_SCENARIOS` runs through `forecast` with INJECTED
simulation sources, because the settlement / authorization / RWA-readiness gates fold
there, not in decide_payment. The sim family also pins the RESTRAINT properties that keep
those gates from over-blocking: a SENDER-side revert is not blamed on the receiver, an
underfunded payer does not gate, and an unreachable RPC fails OPEN. `test_redteam.py`
guards it -- the caught set may not shrink, no control may become a false positive, and
any attack that gets GO must be an EXPLICIT `known_gap`. MUTATION-VERIFIED: disabling the
settlement escalation, the auth replay gate, or the control-attribution each makes the
suite fail by name. Current: 31 attacks caught, 2 documented gaps, 0 false positives.

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
