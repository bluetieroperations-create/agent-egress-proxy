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
> **MCP / ecosystem-history research lives on `research/mcp-and-ecosystem-history`,
> not here.** `mcp_trust.py`, `mcp_history/`, `ecosystem_history/`, their data
> (`data/mcp_descriptions.json`, `data/mcp_snapshots/`, `data/snapshots/`) and the
> six docs that describe them were split off before merge. They were never
> reachable from any entrypoint -- no import path from `blackwall.py`,
> `mcp_server.py`, `seller_portal.py` or `seller_report.py` reaches them, and the
> Dockerfile's `COPY *.py` never shipped the two directories -- so this removes
> unaudited code from the deploy, not a working feature. Two docs deliberately
> STAYED because they are not MCP research: `docs/X402_CANNOT_PRICE.md` (measured
> from 298 `accepts[]` entries in the payee directory) and
> `docs/COMPETITOR_COVERAGE.md` (no MCP content). Audit that branch before
> anything imports it back.
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
  `hmac_key.py` (the ONE owner of the HMAC capability secret. THREE separate
  modules each grew their own COMMITTED fallback for it and each was found as a
  separate audit finding -- `blackwall._DEV_RECEIPT_KEY`, `approvals._key()`'s
  placeholder, and `seller_audit._DEV_AUDIT_KEY`. A COMMITTED SECRET IS NOT A
  SECRET, and every capability token here is an HMAC under this one:
  `sign_report_token` (authorizes writing an OUTCOME, which feeds the reputation
  ledger the entire product is built on), `approvals` decide/redeem (marks a
  HOLD human-approved), and `seller_audit.sign_revoke_token`. With the fallbacks
  in force anyone who could read the public repo could mint all three.
  MEASURED ON THE LIVE DEPLOY BEFORE FIXING: a dev-key-forged report token was
  REFUSED with 403, so `BLACKWALL_RECEIPT_KEY` is set in production and this was
  LATENT rather than breached -- which is why it could be fixed properly instead
  of as an emergency, and why the fix could afford to change boot behaviour.
  WHY A RANDOM PER-PROCESS KEY rather than refusing to boot: `receipt_signer`
  can turn signing OFF when unset because a verdict without a receipt is still a
  valid verdict, and that option does not exist here -- `receipt_id` is emitted
  on EVERY verdict and is the ledger join key, so the capability is MANDATORY.
  Refusing to boot would break every deploy that has not set it, including the
  free public smoke-test configuration whose own blueprint says to leave the
  secret blank. The one real cost of the random key is that tokens DO NOT SURVIVE
  A RESTART, and that cost FAILS SAFE: after a redeploy an in-flight outcome
  report is REJECTED, never accepted as a forgery. It is confusing if
  unexplained -- intermittent "invalid report_token" with no cause -- so
  `describe()` says exactly that and the boot banner prints it. A SHORT secret is
  ACCEPTED and reported WEAK rather than refused, because an operator's existing
  short secret must not stop a deploy and turning that into a boot failure would
  be a breaking change dressed as a security fix. REVOCATION IS THE ONE
  CAPABILITY THAT STILL REFUSES an ephemeral key (`RevocationNotConfigured`): a
  revoke token that works only until the next redeploy is worse than none, since
  an operator would mint one, hand it to whoever does the revoking, and it would
  silently stop working in precisely the situation where trust needs
  withdrawing. An explicitly-set secret ALWAYS wins over an
  already-generated ephemeral one, or a call-order accident would keep the random
  key after the operator configured a real one. `test_hmac_key` also SCANS THE
  SOURCE for the three literals, and that scan is deliberately BLUNT -- it cannot
  tell a mention from a use, which it proved by failing on the docstrings that
  explain the fix. That is the right trade: a scan clever enough to allow
  mentions can be talked into allowing a use, so the convention is to DESCRIBE
  these constants in prose and never quote them. Verified on the real boot path
  in all three states (unset -> WARNING ephemeral, set -> configured, short ->
  WARNING weak). 7 mutations verified killed),
  `x402.py` (Blackwall's own x402 billing: 402 challenge, facilitator seam,
  replay guard, sessions. A HALF-SET CDP PAIR IS NOW A BOOT ERROR
  (`FacilitatorConfigError`), not a silent fallback -- found 2026-09-15 by the
  parallel Migrations session while reviewing the CDP cutover plan, and confirmed
  end to end before fixing. `choose_facilitator` gated on
  `if cdp_id and cdp_secret`, so setting ONE of them fell through to
  `facilitator_url` -- which reads as harmless and is not: on MAINNET that URL is
  a keyless facilitator that settles real USDC perfectly well. So an operator who
  pasted `CDP_API_KEY_ID` and fumbled the secret got a service taking real
  payments through the OLD facilitator while believing they had cut over, and
  their evidence that CDP worked was a settlement CDP never touched. The failure
  mode is not "it doesn't work", it is "it works and proves the wrong thing" --
  the same shape as `cdp_preflight.py` defaulting to TRACEIPT's `payTo` under a
  comment asserting it was the live one. Setting either variable states the
  operator's intent; honouring half of it answers a different question. Same rule
  `receipt_signer.py` already applies to a malformed signing seed: set-but-bad
  means they intended the feature, so fail LOUD at boot. Bounded blast radius --
  the caller is inside `if args.pay_to`, so it can only stop the deploy that turns
  billing ON. `billing_preflight.check_facilitator` catches it and grades FAIL
  (never WARN: no amount of waiting fixes a misconfiguration), which mattered
  because that module MODELLED the old fallback faithfully and therefore BLESSED
  it -- with the secret missing it probed the keyless URL, found mainnet
  supported, and returned OK, so the check whose entire job is "what happens if I
  flip billing on?" PASSED the most likely way a cutover fails. TWO TESTS encoded
  the old behaviour and were replaced, one of them (`test_partial_creds_fall_back_
  to_the_url_path`) asserting OK as correct. MEASURED LIMIT, so the operator is
  not misled twice: the boot banner proves the vars are PRESENT, not that the
  credential is VALID -- a garbage secret still boots and still prints
  "CDP facilitator (authenticated) ... Bazaar-eligible". Validity is
  `check_settlement_auth`'s 401/403 -> FAIL, or the settlement itself. Restraint
  controls: neither var set still boots keyless, and billing OFF is unaffected.
  6 mutations verified killed),
  THE 402's `resource.url` IS OURS, NOT THE CALLER'S (2026-09-15,
  `canonical_resource_url`). Found while fixing the Bazaar listing and it is the
  more serious half: `_challenge` passed the request's `resource` field into
  `build_resource_info` VERBATIM, and that field is CLIENT-SUPPLIED. MEASURED ON
  THE LIVE SERVICE before fixing -- `https://evil.example/owned`,
  `javascript:alert(1)` and `//evil.example/x` each came back inside a real 402
  advertising our `payTo`. The 402 is the document CDP indexes into the Bazaar,
  so an attacker could pay 0.001 USDC with a foreign `resource` and have THEIR
  url catalogued against OUR payout address, borrowing our settlement history for
  one call; and `javascript:` in a field a catalog UI renders is an XSS primitive
  we would publish ourselves. THE ORIGIN IS OURS, THE PATH IS THEIRS: scheme and
  netloc are discarded unconditionally (which also disposes of javascript:/data:/
  file: and of `//host/x`, whose netloc urlsplit parses out), traversal segments
  normalize away, control characters are stripped (the value is echoed into a
  base64 header where a newline forges header structure -- the untrusted-echo
  class again), and the length is capped. The PATH still comes from the request
  because different paths are different priced resources. Fed from
  `BLACKWALL_ORIGIN`/`--origin`, which ALREADY EXISTED for openapi.json's
  `servers[]` and was simply never used here -- which is also why our url was
  RELATIVE and why we are not in the Bazaar (measured: 2000/2000 catalogued
  entries carry an ABSOLUTE url, and an indexer cannot invent a host from a
  path). CORRECTION TO MY OWN FIRST DIAGNOSIS: `build_resource_info` returns a
  v2-spec ResourceInfo OBJECT, so the object shape was right all along and CDP's
  string is its projection of `.url`; the fix is absoluteness, not stringness.
  ONE TEST encoded the old behaviour (`resource.url == "https://r"` -- i.e. it
  pinned the caller's own url being echoed) and was replaced. Restraint: with no
  origin configured the path is unchanged, so no existing deploy breaks, and a
  hostile origin is discarded either way. `extensions.bazaar.info` is present in
  2000/2000 catalogued entries and we emit only `schema` -- DELIBERATELY left
  alone, because the absolute url is the one change with a mechanism behind it
  and changing two things at once means a listing that appears tells you nothing
  about which mattered. POST-MERGE FUZZ of `canonical_resource_url` (43 cases: unicode, percent-
  encoding, NUL, bidi overrides, backslashes, 5KB paths, non-string types) --
  THE ORIGIN GUARD HELD IN EVERY CASE, verified by netloc rather than by
  substring. ONE real finding, LOW: `%2f`/`%5c` are not literal separators, so
  `/..%2f..` was ONE segment that merely CONTAINED ".." and the traversal text
  reached the advertised url. Not a host escape -- every case stayed on our own
  origin -- but a consumer that percent-decodes then resolves would land outside
  the path space we serve. Encoded separators are now decoded ONCE before
  splitting, deliberately NOT to a fixed point: then the number of rounds is the
  attacker's choice and each round can synthesize separators the previous one
  lacked, which is why double-encoded `%255c..` correctly stays literal text.
  TWO OF THE THREE FUZZ FLAGS WERE FALSE POSITIVES IN THE ASSERTION, not
  defects: `https:///evil.example/x` and a backslash-prefixed host land as a
  PATH on our own origin, and a substring grep for a hostname cannot tell a host
  from a path. Corrected to assert `urlsplit(got).netloc`. The corrected
  assertion then swung TOO LOOSE -- mutation testing showed the segment check
  ALONE passes with the decode deleted -- so BOTH halves are asserted now: no
  `..` SEGMENT and no encoded separator remaining. LOOSENING AN ASSERTION TO
  KILL A FALSE POSITIVE CAN WALK STRAIGHT PAST THE TRUE ONE, which is the lesson
  worth keeping. 9 mutations verified killed, three of which caught a TEST
  defect rather than a code one: the control-character case held LITERAL
  backslash-r-n from a heredoc, so stripping could be removed with the test still
  green; it is built from `chr()` now. See `docs/BAZAAR_LISTING.md`.
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
  `integrations/lucid/` (Lucid Agents Commerce SDK gate (daydreamsai/lucid-agents)
  -- an ADAPTER, not a clone. Lucid's buyer path composes fetch wrappers and its
  own `wrapBaseFetchWithPolicy` inspects the unpaid x402 requirement and reserves
  budget BEFORE a signature exists, refusing with `403 policy_violation` -- the
  right place to stand, which is why this is a fetch wrapper too. THE GAP is the
  one `integrations/agentcore/` documents about AWS: their budget tier constrains
  `allowedRecipients` (a STATIC allowlist), `maxPaymentUsd` and
  `maxTotalUsd`/`windowMs`, so it enforces HOW MUCH and WHO only from a list a
  human typed. An allowlist cannot say a payee is a wash-trading Sybil ring,
  OFAC-sanctioned, quoting 50x its category median, silent for 90 days, or
  advertising a `payTo` that is not a possible address. TWO DESIGN POINTS THE
  WRAPPER POSITION FORCES, both bugs if done the obvious way: (1) SCORE EVERY
  `accepts[]` ENTRY -- the client picks a requirement later, INSIDE
  `wrapFetchWithPayment`, so scoring `accepts[0]` and letting it pay `accepts[1]`
  scores a payment that never happened; the choice is unknowable here so the only
  sound rule is "safe for whichever it picks" and the combined decision is the
  MOST CONSERVATIVE across entries (openclaw reads `accepts[0]` and is RIGHT to:
  it sits at a tool-call boundary where the payload is already chosen). (2) READ
  ALL THREE CARRIERS -- MEASURED, the body alone leaves 86 of 195 live hosts
  unreadable and 80 of those serve a complete v2 challenge in `payment-required`
  with `{}` as the body, so a body-only gate fails OPEN on ~41% of the live
  ecosystem while looking healthy. Turns a 402 into a 403, the same currency
  Lucid's policy already speaks, so no signature is ever created; never signs,
  holds a key or moves money. FAIL-CLOSED default (an unscored payment is
  irreversible, a stopped agent is not) and `mode: "observe"` overrides
  everything. `decide` is IMPORTED from `../openclaw/core.js`, not copied -- ONE
  place where GO/HOLD/STOP becomes allow/confirm/block. THAT IMPORT FORCED A
  SPLIT worth noting: openclaw's `index.ts` had one
  `import ... from "openclaw/plugin-sdk/plugin-entry"` holding the claim parsing
  AND the decision hostage, making the module unimportable outside an OpenClaw
  plugin -- so the dependency-free half moved to `core.ts` and `index.ts` became
  the thin host adapter that re-exports it, which is the shape
  `blackwall_guard.py` / `wallet_guard.py` / `agentcore_guard.py` already had and
  this one did not. VERIFIED LIVE against the real endpoint: a sanctioned payee
  -> 403 STOP, a cold-start payee -> 403 HOLD, and one clean + one sanctioned
  entry -> STOP with both entries' reasons merged. HONEST LIMITS in its README:
  the thin shim is UNVERIFIED against a live Lucid install (composition order and
  the 403 convention come from their published docs), amounts assume 6 decimals,
  and A2A/ERC-8004 are untouched. TypeScript + vitest; own tests run from that
  dir. POST-MERGE AUDIT finding (MEDIUM), reproduced before fixing: the
  challenge is authored by the SELLER BEING SCREENED and every `accepts[]` entry
  cost one forecast request, so a hostile 402 with 500 entries produced exactly
  500 parallel calls against the operator's own Blackwall -- one-request-to-N
  amplification, and money on a paid endpoint. `MAX_ACCEPTS = 16` is DERIVED
  FROM THE CORPUS, not taste: 370 priced quotes across 177 answering hosts is a
  mean of 2.09 entries per host, and our own live endpoint serves 2, so 16 is
  ~8x the mean. OVER THE CAP IS A REFUSAL, NOT A TRUNCATION -- scoring the first
  16 and allowing would let an attacker put the bad entry at position 17, which
  is the ordering bug design point 1 exists to avoid; a challenge advertising
  more than 16 payment options is itself anomalous. Restraint control: 1, 2, 3,
  8 and 16 entries all pass unaffected. 30 tests, 14 mutations verified killed),
  `integrations/openclaw/` (OpenClaw/NemoClaw plugin -- a `before_tool_call` hook
  that recognizes payment-shaped tool calls (flat payTo/amount, 402-challenge
  accepts[], or a signed X-PAYMENT header -> passed through for payload-sim),
  forecasts them, and blocks non-GO. Enforce + fail-closed by default; keyless
  (free-tier endpoint), claim-only egress. TypeScript + vitest; own tests run
  from that dir (`npm install && npm test`), not the root command. Canonical
  source for the nemoclaw-community `blackwall-x402-payment-gate` example. SPLIT
  2026-09-15 into `core.ts` (dependency-free: claim parsing, `decide`, `postJson`,
  config) and `index.ts` (the thin OpenClaw host adapter, re-exporting the core),
  because a single host-only import made the decision logic unimportable from
  `integrations/lucid` -- the alternative was a second copy of "what does a
  verdict mean". Its 38 tests pass unchanged),
  `BLACKWALL.md`, `DISCOVERY.md`, `DEPLOY.md`, `COMPETITIVE.md`, `PRICING.md`,
  `ap_gate.py` (treasury/AP payout gate -- folds the verdict into a
  RELEASE/REVIEW/BLOCK decision at the approve-&-release step; see
  `docs/TREASURY_AP.md`),
  `seller_audit.py` (seller-side "verified merchant" tier -- EARNED not paid: audit
  an endpoint from readiness + on-chain history + sanctions + price-fairness, issue a
  signed/expiring/revocable attestation granting a bounded trust FLOOR that waives the
  thin-count gate but never the Sybil gate and never overrides a STOP; folds into
  decide_payment via `verified_floor` + forecast via a `SellerRegistry`.
  SIGNING IS Ed25519 AS OF 2026-09-15 (was HMAC-SHA256), and TWO DEFECTS WERE FIXED
  TOGETHER because either alone is worse than both. (1) HMAC IS SYMMETRIC: the
  badge's whole selling point is that a merchant shows it and a buyer checks it,
  and only the holder of the secret could do either -- who could also forge one.
  (2) `_audit_key` FELL BACK TO A COMMITTED KEY, `_DEV_AUDIT_KEY =
  b"blackwall-dev-audit-key-not-for-prod"`, in the PUBLIC repo; with
  `BLACKWALL_AUDIT_KEY` unset -- the shipped default -- any reader of GitHub could
  forge a badge granting a trust floor. `receipt_signer.py` documents exactly this
  lesson ("a receipt signed with a committed key is WORSE than none -- it looks
  verifiable") and this module never got it. UNEXPLOITABLE IN PRODUCTION ONLY
  BECAUSE `seller_registry` IS NEVER BOUND in `serve_forever` -- two defects
  cancelling, and the FIFTH instance of the wired-and-inert pattern here. Which is
  also why the signing fix had to land BEFORE wiring the registry: doing them in
  the other order would have activated the forgeable badge. NO FALLBACK now -- an
  unconfigured signer RAISES `AttestationUnavailable` rather than issuing an
  unsigned badge, deliberately asymmetric with `ReceiptSigner.sign()` returning
  None, because a verdict without a receipt is still a valid verdict while an
  unsigned attestation is pure assertion that would still grant a floor. The
  envelope is byte-compatible with `receipt_signer`'s, so ONE verifier reads
  verdicts, Traceipt receipts and attestations, and the key is ALREADY published at
  `/jwks.json` -- no new key to manage. `typ` is
  `blackwall-seller-attestation+json`: one key signs both claim types, so the label
  is the only thing separating "we vouch for this merchant" from "we judged this
  payment", and since ANY anonymous caller can get a verdict signed,
  `verify_attestation` CHECKS typ FIRST -- without it a signed verdict is a valid
  badge, and the signature is genuine so nothing else catches it. Required
  parameterizing `receipt_signer.TYP`, which is BOUND AT CONSTRUCTION and
  deliberately NOT a `sign()` argument (a per-call typ lets the verdict path
  mislabel a verdict with one wrong keyword; a structural test asserts `sign()`
  takes only `payload`). FLOATS NOW TRAVEL AS DECIMAL STRINGS (`_decimalize`): the
  HMAC version signed raw floats and got away with it ONLY because we were the
  only possible verifier -- floats have no canonical JSON form and `canonical_json`
  refuses them, so `sign_attestation` RAISED the moment it reached the real signer.
  `floor` is now "0.850000"; `blackwall.py` applies it through `float()` and is
  unaffected. Verification for the SEED HOLDER is a re-sign-and-compare, sound
  because Ed25519 is DETERMINISTIC and dependency-free (`cdp_auth`'s Ed25519 is
  sign-only by design); a THIRD PARTY never calls `verify_attestation` -- they take
  the envelope plus the public key and use any standard implementation, which
  `TestThirdPartyVerifiable` does against `cryptography`, a test that was
  IMPOSSIBLE to write under HMAC and is the whole finding. `SellerRegistry.add`
  keys off the SIGNED subject, never an outer field, or an envelope claiming a
  different subject than its own signature gets filed under the attacker's
  address. NO DOWNGRADE PATH: a legacy flat `sig` attestation is refused outright
  rather than kept for compatibility, which would be algorithm confusion -- safe
  because the registry was never wired, so none exists in the wild.
  NOW WIRED AND REVOCABLE (2026-09-15, the same day, because the two had to land
  in this order). The tier was `seller_registry` -- a `forecast` PARAMETER bound
  by NOTHING -- so for its entire life no badge could be issued, consulted or
  revoked on the wire while every unit test passed. Wiring it took the SEVEN
  edits `honeypot.py` counts, and the SIXTH (the `BlackwallServer.__init__`
  parameter) was caught by an AttributeError AT BOOT: the loudest of the seven
  and the only one that is not silent. The seventh (`_BoundHandler`) is covered
  by `test_approvals`' parity guard, and `test_seller_audit.TestTheLiveWire`
  drives a REAL server because five earlier instances of this pattern were all
  invisible to unit tests. Loaded from `BLACKWALL_SELLER_REGISTRY`;
  revocations persist to `BLACKWALL_SELLER_REVOCATIONS`.
  REVOCATION IS DURABLE, and the old in-memory `_revoked` was not a fail-open --
  fail-open means declining to add caution, while a restart RESTORED every
  revoked badge along with its trust floor, actively granting trust the operator
  had withdrawn. Bounded by the badge TTL rather than unbounded, which is what
  made it easy to under-rate. `FileRevocationStore` is append-only, fsynced, and
  FAILS CLOSED on write -- deliberately opposite to `reachability_ledger`'s
  fail-soft, because a diagnostic that cannot log should still answer while a
  revocation that silently did not persist leaves the operator believing trust
  was withdrawn. Keys are NORMALIZED (lower+strip) or a revoked merchant reads as
  trusted again under EIP-55 capitalization -- the join that missed 64 of 69
  endpoints in `advertised_prices`, here as an evasion. `load_registry`'s failure
  is ASYMMETRIC and that is the design: a bad ATTESTATION file costs a merchant
  its floor (conservative -> fail-open, tier still runs), an unreadable
  REVOCATION list means loading badges we cannot check revocation for (-> refuse
  outright). Verified at BOOT in both directions.
  MONOTONICALLY SAFE BY CONSTRUCTION: there is NO un-revoke, on the store, the
  registry or the wire, so whoever holds a token can only REMOVE trust from ONE
  merchant, never grant it. `sign_revoke_token` is PER-SUBJECT (a leak is not the
  whole registry) and domain-separated with "revoke:"; `POST /v1/seller/revoke`
  returns the SAME 403 for a known and an unknown subject so it is not an
  enumeration oracle for who holds a badge, and 503 rather than 200 when the
  store refused. Issuance is deliberately NOT exposed -- granting a floor stays
  an operator act. `GET /v1/seller/revocations` PUBLISHES the list, which is what
  completes 3b: a badge anyone can verify against `/jwks.json` whose revocation
  nobody can see is only as good as its TTL.
  POST-MERGE AUDIT (2026-09-15), three findings, all fixed, and the first was
  found by MEASURING the hot path rather than reading it:
  (1) HIGH -- `credential_for` called `verify_attestation`, whose signature check
  is a RE-SIGN-AND-COMPARE, so every verdict naming a badged counterparty
  performed an Ed25519 signing operation, reachable by any anonymous caller of
  `/v1/forecast-payment`. MEASURED: 0.133ms on the native backend and
  **221.864ms on the pure-Python fallback**, against a 0.109ms verdict -- ~2000x
  the thing it decorates, which on a 0.1-CPU box is a self-inflicted outage, not
  a slow path. It was ALSO a variable-time oracle (`_scalarmult` leaks the
  nonce's Hamming weight and the nonce derives from the secret prefix), though
  that half was already covered INCIDENTALLY: the attestation signer shares
  `BLACKWALL_SIGNING_SEED` with receipt signing, so the existing public-bind
  boot guard fires. The LATENCY was covered by nothing, and the FATAL message's
  "~170ms per verdict" understated it. THE FIX IS ALSO THE RIGHT DESIGN: the
  signature protects against a tampered FILE, which is a load-time concern, and
  nothing mutates the in-memory envelope between entry and use -- so
  `verify_envelope` (shape/typ/signature) runs ONCE in `add`/`issue`, which now
  REFUSES an unverifiable envelope, and `check_window` (expiry/revocation) runs
  per request because those are functions of the clock and of operator state.
  RE-MEASURED: 221,864us -> 0.87us, and BACKEND-INDEPENDENT, so the oracle is
  gone rather than mitigated. `verify_attestation` still composes both halves as
  the full public check. Verified on the REAL boot path: a file with one good and
  one tampered row reports "1 badge(s) loaded, 1 skipped".
  (2) MEDIUM -- `sign_revoke_token` fell back to `blackwall._receipt_key()`,
  which returns `_DEV_RECEIPT_KEY` (`b"blackwall-dev-receipt-key-not-for-
  production"`, IN THE PUBLIC REPO) when `BLACKWALL_RECEIPT_KEY` is unset.
  MEASURED: a token forged from that constant was ACCEPTED, so any reader of
  GitHub could strip any merchant's badge. Bounded by this module's
  monotonic-safety design -- revocation only ever REMOVES trust, so it is
  merchant griefing rather than escalation -- hence medium. THIRD instance of
  this root cause here after `_DEV_AUDIT_KEY` and the reason `receipt_signer`
  refuses to have one. `_revoke_key` now has NO fallback and raises
  `RevocationNotConfigured`; the route answers 503 "revocation not configured"
  rather than a misleading 403. Verified live: the dev-key forgery gets 403, the
  real operator token gets 200.
  (3) See `integrations/lucid` for the third (fan-out amplification).
  7 further mutations verified killed, one of which SURVIVED and was a real gap:
  the 503-when-unconfigured branch was implemented and no test reached it over
  HTTP.
  12 mutations verified killed, TWO of which SURVIVED the first pass and were
  real test gaps worth naming: the domain-separation test compared against
  `approvals.sign_approval_token`, which carries its OWN prefix, so deleting
  "revoke:" left them unequal and the test PASSING -- a check aimed slightly to
  the left of the thing it verifies, same shape as `cdp_preflight`'s wrong
  default payee; and the asymmetric-failure rule was implemented with no test
  reaching either branch, because bad LINES in a readable file are skipped
  line-by-line and never touch the unreadable-FILE path. A DIRECTORY in place of
  the file is how both are now exercised, since `chmod` does nothing as root),
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
  `billing_preflight.py` (ANSWER "if I flip billing ON, what happens?" BEFORE the
  deploy. Turning billing on is a one-line config change (`BLACKWALL_PAY_TO=0x...`)
  and every way it fails is QUIET: a payee that is well-formed but wrong; an asset
  the decimals table does not cover, quoted in units nobody agrees on; a 402 that is
  well-formed to us and UNREADABLE to a real client (in which case we are not
  charging, we are refusing); a facilitator that answers and does not settle our
  network; and a pricing policy that is perfectly valid and collects NOTHING, which
  looks exactly like success until the month ends. ELEVEN checks, four of which exist
  nowhere else. (1) CHALLENGE ROUND-TRIP -- emit the 402 we would serve and re-parse
  it with our OWN `x402_challenge.parse_challenge`, through BOTH carriers
  independently (the body path would otherwise shadow the header path, and 86 of 195
  live hosts serve requirements ONLY in a header), comparing every field a payer
  SIGNS against the config; the only check that proves a stranger can PAY us.
  (2) REVENUE, projected against the committed corpus as an INTERVAL not a point --
  `data/directory.json` stores the min/max HULL of each payee's price list, not the
  list, the exact caveat `advertised_prices.py` documents, so collapsing it would
  invent a distribution we never measured. Reproduces the hand-measured figure: value
  pricing bills 1-19 of 265 corpus payees, $0.007-$0.314 for one forecast each.
  (3) PROPORTIONALITY -- `x402.PricingPolicy` enforces a fee/amount bound whose own
  comment names the reason ("the median live x402 quote is $0.005"), but
  `BillingGate._price_for` consults a policy ONLY when one is configured, and the
  SHIPPED DEFAULT is flat pricing with no policy. So the default path never applies
  the bound its own module documents as necessary: measured on the corpus at the
  shipped $0.001, the fee is a MEDIAN 20% of the payment being screened and exceeds
  the 1% bound for 251 of 265 payees. Also `check_network`, found by this module's
  own tests -- `default_billing_asset` falls back to Base MAINNET USDC for an
  unrecognized network and `to_caip2` passes an unknown name through by design, and
  those compose into an eip155 asset advertised on `solana`; `check_asset` CANNOT
  catch it because `known_decimals` falls back to an address-only table that answers
  6 whatever the chain says. And a NOTE, not a gate: value pricing derives the fee
  from `payload["amount"]`, which the caller writes and nothing verifies -- declaring
  a sub-threshold amount gets the counterparty screen free (the budget/blast-radius
  half degrades, which is self-limiting). AUDIT FINDING (fixed): a facilitator's
  `/supported` document is written by a THIRD PARTY and was echoed into the report
  RAW -- a scheme containing a newline forges its own line in the report an operator
  reads before deciding to send that facilitator money. FOURTH instance of this class
  here (`payee_syntax`'s hint, `approvals`' `decided_by`, and `secret_scan`'s whole
  reason for existing); sanitized at the trust boundary in `supported_kinds` so every
  consumer is covered, and the echo is bounded. FIRST LIVE RUN falsified our own
  docs: `DEPLOY.md` paired `BLACKWALL_FACILITATOR=https://facilitator.x402.rs` with
  the default `BLACKWALL_NETWORK=base`, and that facilitator lists 31 kinds with NO
  `eip155:8453` -- every EVM network it settles is a testnet, as is all of
  `x402.org/facilitator`'s EVM support. The documented copy-paste mainnet deploy
  would have had every payment rejected while looking healthy. CORRECTED 2026-09-15,
  and the correction is the more useful finding: that sentence used to read "Base
  mainnet needs the authenticated CDP facilitator", which GENERALIZED from two
  measured facilitators to every keyless one. `facilitator.payai.network` is keyless
  and DOES settle Base mainnet -- 33 kinds including `exact`/`eip155:8453` at x402
  v2, measured live. It is what the live service has been settling through all
  along. So keylessness is not the property that matters; whether a facilitator
  LISTS your (scheme, network, version) is, which is exactly what the preflight
  checks and what a prose claim about "keyless facilitators" cannot. CDP remains the
  only Bazaar-listing path -- a DIFFERENT claim, and one taken from Coinbase's docs
  rather than measured here. SECOND LIVE-RUN FINDING (2026-09-07, against a real payout address): with CDP
  creds set the facilitator check returned NOTE and the text "/supported is
  authenticated, so it was NOT probed here" -- so INVALID CDP CREDENTIALS PASSED
  the preflight, and the single most likely way a mainnet deploy fails silently
  was the one thing the check declined to look at. Authenticated is a reason to
  MINT A TOKEN, not a reason to skip. `_cdp_get_json` now GETs `/supported` with
  a freshly minted Bearer JWT and grades the answer through the SAME
  `_grade_kinds` the keyless path uses, so CDP is not exempt from the network
  check that makes the keyless facilitator FAIL on mainnet. 401/403 -> FAIL
  ("rejected the credentials"), because a wrong key is never transient and
  presents in production as every settlement failing while the service reports
  healthy; anything else -> WARN, so a blip cannot block a correct config.
  Verified live against api.cdp.coinbase.com: it 401s a bad token and accepts
  GET (no header and a garbage bearer both 401), so a 401 with a properly minted
  JWT really does mean refused -- a POST-only endpoint would answer 405, which
  routes to WARN. THREE TESTS encoded the old behaviour and were replaced, one
  of them (`test_cdp_does_not_probe_the_network`) PINNING the skip. (10) SETTLEMENT COST, added 2026-09-11 -- every other money check here asks
  what we CHARGE; none asked what charging COSTS. Collecting an x402 payment
  means the facilitator broadcasts an onchain settlement, and CDP prices that at
  $0.001 past a free first 1,000/month. A fee below it is not thin margin, it is
  a payment we lose money by accepting, and it looks identical to revenue in
  every report until the invoice arrives. MEASURED on the committed corpus at the
  shipped value pricing: 41 of 46 payees billable at the CHEAPEST end of the
  price hull (89.1%) and 133 of 164 at the dearest (81.1%) are billed below cost,
  mean shortfall $0.000882. An earlier note said "140 of 164" -- that took the
  first billable of each payee's min/max and so belonged to NEITHER end of the
  hull; both real ends are now reported, and the detail line names which one it
  measured. Break-even lands at $0.9995, not the $1.00 the bps arithmetic gives,
  because the real fee function ROUNDS -- which is why `_breakeven_amount`
  BISECTS that function rather than inverting the formula, an inversion being a
  second implementation of pricing free to drift from the one that quotes. Graded
  WARN and NEVER FAIL: billing still works -- the 402 is valid, the payer pays,
  the money arrives -- and selling below cost is a decision an operator may make
  deliberately (a loss-leader buying the verdict->outcome history this engine
  exists to accumulate). The real fix at volume is CDP's `batch-settlement`
  scheme, which collapses the per-payment cost instead of raising the price.
  `SETTLEMENT_COST` is a THIRD PARTY'S price, so it is DATED and overridable via
  `--settlement-cost` rather than treated as a constant of nature. CORRECTED
  2026-09-15, reported by the billing session after a real mainnet settlement,
  and the correction is the finding: dating the constant was NOT ENOUGH, because
  the cost belongs to the FACILITATOR YOU CONFIGURED and the check hardcoded
  CDP's while production settled through PayAI -- so it reported a shortfall
  computed from a price sheet nobody was paying. `settlement_cost_for` now reads
  the facilitator the config would actually USE, mirroring `choose_facilitator`
  (both CDP creds present means CDP whatever the URL says, or a config settling
  through CDP would be priced as PayAI). A facilitator absent from
  `SETTLEMENT_COSTS` yields UNKNOWN and the check DECLINES TO GRADE -- NOTE,
  reporting the break-even (a property of pricing alone, true regardless) and
  asking for `--settlement-cost`, rather than borrowing another facilitator's
  number. PayAI is deliberately NOT recorded as $0: two settlements showed no
  ON-CHAIN deduction, which is evidence about the chain and not about commercial
  terms, and a fee billed off-chain looks identical from a receipt -- calling it
  free would be the same leap as "valid checksum" -> "right payout address" and
  "two testnet facilitators" -> "keyless is testnet-only", both of which this
  project has already made. An explicit `--settlement-cost` always wins: that is
  the operator's measured number and it beats anything on file. Two mutations
  survived the first pass and both were the wired-and-inert pattern in miniature:
  the flag could be dropped from the assembly leaving it parsed, documented and
  INERT, and the mean shortfall could be hardcoded to zero. (11) SETTLEMENT AUTH, folded 2026-09-11 from the PARALLEL SESSION's
  `cdp_preflight.py` (branch `claude/blackwall-x402-integration-j3rdab`) -- both
  sessions built a CDP preflight without knowing about the other, and that one
  probed `POST /verify` as well as `GET /supported`. NOT redundant: `/supported`
  is a READ and settlement is a WRITE, and a CDP Secret API Key carries
  RESTRICTIONS (the keys already in the operator's project are scoped `Portfolio:
  Primary / Trade - View`), so a key can pass the capability read and be refused
  on the path that moves money -- invisible until production, the exact shape
  this module exists to catch. NO MONEY MOVES: only `/settle` transfers, and the
  payload is a deliberate throwaway with an empty `payload{}`, so a structured
  x402 validation error is the EXPECTED answer and a PASS. 401/403 -> FAIL and
  names SCOPE as the likely cause when `/supported` passed; anything else -> OK;
  unreachable -> WARN; no creds -> NOTE. The same fold also brought the
  x402Version DISCRIMINATION `supported_kinds` structurally cannot make -- it
  reduces to (scheme, network) and DROPS the version, so a facilitator settling
  Base-mainnet `exact` at v1 only, while our 402 advertises v2, matched the pair
  and graded OK while the real paid call would be rejected as an unsupported
  kind. `kind_versions` reads versions for ONE (scheme, network); an EMPTY set is
  no opinion, never a mismatch, because many facilitators omit the field.
  MEASURED LIVE on facilitator.x402.rs: all 31 entries state a version (5 v1, 26
  v2), and the gate FAILs `solana-devnet` (exact/v1 only) end to end. A NUANCE
  that nearly became a false finding and is now pinned by test: that facilitator
  lists the SAME chain under BOTH spellings at DIFFERENT versions --
  `exact/base-sepolia` v1 AND `exact/eip155:84532` v2 -- so the CAIP-2 spelling
  must be consulted FIRST and its versions read alone. The parallel session's
  `_is_base_mainnet` merges both spellings, which is why this is a fold rather
  than a copy. Pure
  core; network and corpus injected; exits 0/1/2 (ready / a person should look / it
  would not work) so a scheduled run is actionable. Tests:
  `test_billing_preflight.py`, 115 tests, 63 mutations verified killed),
  THE PAYOUT ADDRESS is `BLACKWALL_PAY_TO`, dashboard-set (`sync: false` in both
  blueprints) and deliberately NOT in code -- a hardcoded payout is a wrong
  payout waiting to ship. Confirmed by the operator 2026-09-11 and by the live
  `/.well-known/x402`. `cdp_preflight.py` is DELETED: its unique half (the
  `POST /verify` probe) is folded into `check_settlement_auth` above and tested
  there, nothing imported it, and it DEFAULTED its payee to
  `0x3ec5e0ec...9004e1` under the comment "defaults to the live one" -- which is
  TRACEIPT's payTo. So it proved a CDP key could pay a DIFFERENT product's
  address and reported success; two cross-session handoffs then repeated that
  address as Blackwall's "real payout address", which is how a wrong default
  becomes a wrong belief. `cdp_verify_probe.py` now has NO default payee and
  refuses without one, and it `raise SystemExit(main())` -- it returned 2 on a
  refusal and discarded it, so "I declined to probe" exited 0, indistinguishable
  from "the credential verified". Found by running it, not by reading it.
  `billing_preflight.py` was never affected: it reads `BLACKWALL_PAY_TO` from the
  environment and hardcodes nothing, so the other session's "passes every check"
  was a config problem in their shell, not a defect in the module.

  `seller_report.py` (the SELLER side -- "why agents are not paying you". Every
  other gate here serves the BUYER; this is the first thing that serves the party
  being screened, and it needs no new data: one payee or host, the committed
  corpus, and one live probe. THE HEADLINE is that the seller is run through the
  REAL engine (`decide_payment`), so they learn the verdict a buyer's agent
  actually gets and its reasons, not our opinion of their endpoint. The finding
  they cannot get anywhere else is CROSS-PAYEE demand authenticity: a
  receipt-window analysis can see that 200 addresses paid you, but only a
  cross-payee graph can see that not one of them ever paid anybody else -- the
  evidence lives in the OTHER payees, so no sample size fixes it. FOUR RULES, each
  from a specific past mistake: (1) NEVER REPORT OUR OWN STALE ARTIFACT AS THE
  SELLER'S BUG -- `data/liveness.json`'s `class` field predates the
  `payment-required` carrier and still says 86 of 195 hosts serve an unreadable
  402, so reading it would tell ~68 sellers their challenge is broken because OUR
  parser was incomplete; parseability is derived LIVE or reported NOT CHECKED, and
  the test asserts that against the source rather than trusting the docstring.
  (2) SILENT IS NOT BROKEN -- an unreachable host yields `unknown`, never a
  defect (the payee_syntax lesson). (3) A REPORT MUST NEVER BECOME AN INPUT TO THE
  GATE THAT SCORES THE SAME SELLER, or the diagnostic is a laundering step;
  structurally tested. (4) EVERYTHING ECHOED IS UNTRUSTED -- host, payee and
  category are authored by the party being reported on and this text is mailed to
  them; FIFTH instance of that class here. FOUR BUGS FOUND BY RUNNING IT LIVE, all
  fixed, and all of the kind that only appears when a diagnostic is pointed at a
  real business: (a) TWO BUSINESSES IN ONE REPORT -- blockrun.ai carries three
  payees, and the CLI resolved the probe and payer graph from `matches[0]` while
  the report described `max(settlement_count)`, so one payee's graph ("26 payers
  corroborated") landed in another's report ("1 distinct payer, possible
  wash-trading") with the numbers contradicting each other on the page; fixed
  structurally, `select_subject` is the ONE selection site and callers pass
  FUNCTIONS so nothing can resolve against a different row. (b) CONGRATULATED A
  SELLER ON ABSENT EVIDENCE -- the engine's Sybil flags need a minimum payer count
  to fire, so a payee with ONE payer tripped neither and fell into the positive
  branch: "0 of your payers also pay other known endpoints, which is the
  hard-to-fake half of a reputation", marked ok. Fixed with three MEASURED tiers:
  zero corroboration is 11 of 266 endpoints (4.1%) against a median of 10, so it
  is a real warning, and every tier now quotes that median -- which turns an
  accusation into a measurement the seller can check. (c) ACCUSED A GIFT-CARD
  MERCHANT OF GOUGING -- Bitrefill's dearest option is $1000 against a $0.25
  commerce median, reported as "4000x your category". The hull hazard from
  `advertised_prices.py`, compounded by the fact that the engine gates on the
  AMOUNT PAID rather than the listing, so judging the listing was stricter than
  the engine AND wrong about it; now stated as the engine's real consequence
  (where the hold line sits, which of your options cross it). (d) THE FLAGSHIP
  FINDING WAS UNREACHABLE -- `PayerReputationSource` takes EDGES, and passing the
  store raised a TypeError the fail-soft turned into a benign "not assessed", so
  the one unique finding never ran through the CLI while every test passed; the
  wired-and-inert pattern, fourth time here. A FIFTH bug found by the pre-merge pass, same class as (a) and one
  level down: a report keyed on a HOST probed the payee's WHOLE resource list,
  so `probe_resources`' first-answer rule could write it from a SIBLING host.
  Measured on the corpus: 58 of 266 payees are multi-host and EVERY one has a
  host key that would start elsewhere -- payanagent.com from api.anchor-x402.com,
  x402.ottoai.services from api.aidress.ai, which are different businesses
  sharing a payment address. `resources_for_key` scopes a host-keyed report to
  that host, EXACT-match not substring (a lookalike host must not become the
  probe target), with NO sibling fallback: a host whose own resources all fail
  IS unreachable, and answering with a neighbour's success is the bug.
  Descriptive only: nothing gates,
  scores, or changes a verdict. Exits 0/1/2 so a batch run is actionable. See
  `docs/SELLER_SIDE.md`. Tests: `test_seller_report.py`, 40 tests, 23 mutations
  verified killed),
  `seller_portal.py` (the DELIVERY MECHANISM for the seller diagnostic --
  `seller_report` ran on a laptop, which is not a product: a seller could not get
  one without us mailing it, and the seller email has been blocked for weeks.
  Serves it self-serve at `/r/<address-or-host>` (`?format=json` for machines),
  so "why is nobody buying from me" is answered by a link rather than a campaign.
  A SEPARATE PROCESS on purpose, for three reasons: (1) `seller_report`'s rule 3
  says the engine must never import the report, and serving from `blackwall.py`
  would make that structural test a lie; (2) the threat surfaces genuinely
  differ -- the verdict API takes JSON from an agent, this takes a string from a
  BROWSER and renders HTML back, which is an XSS surface the verdict API does not
  have, and a defect in a public renderer must not reach the process holding the
  signing keys; (3) it can be deployed, restarted or switched off without
  touching the endpoint agents depend on. PUBLIC rather than per-seller
  authenticated because every finding derives from public data and is the SAME
  answer `/v1/forecast-payment` already returns to any anonymous caller about
  that payee -- publishing it discloses nothing new, while withholding it would
  only mean a seller cannot see what every buyer already can.
  THE SECURITY FINDING, LATENT NOT LIVE: a resource URL is NOT our data --
  `discovery_crawl` harvests it from a stranger's own x402 advertisement, so it
  is attacker-authored content we store and later FETCH. Measured 2026-09-06 the
  corpus is clean (3827 resources, all https, no IP literals, no private hosts),
  but it is refreshed by crawling third parties and nothing stopped the next
  crawl carrying `https://169.254.169.254/x402` or a name resolving there, after
  which the report echoes the status and error string -- a usable oracle for
  mapping whatever network this runs in. `seller_report.safe_probe_url` now
  refuses non-HTTP schemes, embedded credentials (`https://trusted@evil/x` reads
  as "trusted" to a human scanning the corpus), and any host where ANY resolved
  address is loopback/private/link-local/reserved -- every address, because a
  name answering with one public and one private would otherwise pass and let
  the OS pick. DNS REBINDING IS NOW CLOSED, having first been written off as a residual gap:
  `pinned_address` returns the address it approved and `_fetch_pinned` DIALS
  THAT ADDRESS, so there is no second lookup to poison. The NAME is still what is
  presented -- SNI, certificate verification and the `Host` header all use the
  hostname -- so this pins the route, not the certificate, and does not weaken
  TLS. The tempting bad fix (an unverified context, when presenting an IP breaks
  hostname verification) is worse than the bug it appears to solve, so the test
  asserts the STATE of the context reaching the handshake rather than grepping
  the source for known-bad spellings: `_create_unverified_context()` is a third
  spelling that a grep misses. OTHER PROPERTIES, each
  mutation-tested: a caller's key can NEVER become the probe target (it is only
  ever a corpus lookup, and an unknown key makes ZERO network calls); everything
  echoed is HTML-escaped (SIXTH instance of the untrusted-echo class here and the
  first that is XSS rather than a forged log line) under a `default-src 'none'`
  CSP; reports are cached (TTL + bounded LRU) so a refresh does not re-hit the
  seller and an enumeration flood stays cheap -- misses are cached too; the payer
  graph is precomputed at BOOT, never per request, since building it takes
  minutes over the real corpus (the `issuer_trust_gate` pattern); the form
  redirects through `/go` into a shareable `/r/<key>` URL with the key
  percent-encoded so `//evil.com` cannot become a protocol-relative open
  redirect; and it is rate-limited per client because each report costs a
  STRANGER a request. A SECOND, NARROWER BOUND keyed to the party actually being
  protected: `PROBE_COOLDOWN` caps how often ONE HOST is touched however many
  people ask -- PER HOST, which is what `_mark_probed` keys on; a test asserted
  a stricter per-PAYEE bound the design never provided, and was corrected to the
  real property rather than the code loosened to a claim nobody had made. It
  cools only the hosts ACTUALLY CONTACTED: `probe_resources` stops at the first
  answer, so the ones reached are those up to and including the resource it
  returns, and cooling the rest would tell a visitor asking about a host nobody
  probed that it was "not checked" -- the cross-host attribution again. A probe
  that answers nothing still cools every candidate, since a dead seller is the
  case that would otherwise be re-probed on every cache miss. The report cache is keyed by what the visitor TYPED, and a payee is
  reachable by its address or any of its hosts -- 58 of 266 corpus payees have
  more than one -- so each spelling probed the same stranger independently. When
  every candidate host is cooling down the probe is skipped and the report says
  "not checked" beside the ledger's history, which is more honest than re-hitting
  a stranger to repeat something we already know. Verified live against the real corpus: 266 payees, 266 with
  a precomputed graph. See `docs/SELLER_SIDE.md`. Tests:
  `test_seller_portal.py`, 46 tests incl. a real server, 27 mutations verified
  killed). PRE-MERGE AUDIT (2026-09-07): the rate-limit identity was wrong for
  the topology this is meant to run in. `ratelimit.client_ip_from` takes the
  RIGHTMOST X-Forwarded-For entry, correct behind ONE proxy -- but Cloudflare in
  front of Render is TWO, so the rightmost entry is the CDN and every visitor
  collapses into a single bucket, making the limiter a GLOBAL 30/minute cap: not
  a bypass, a self-inflicted outage the first time the page gets attention.
  `client_key(xff, peer, depth)` counts trusted proxies from the right,
  `--proxy-depth` / `PORTAL_PROXY_DEPTH` configures it, and the DEFAULT
  UNDER-STATES (1) on purpose -- a short chain falls back to the raw TCP peer,
  which cannot be forged, whereas each overstated hop treats one more
  client-written entry as trustworthy and hands every visitor a forgeable
  identity),
  `reachability_ledger.py` (DID WE REACH THIS HOST, AND WHAT HAPPENED LAST TIME?
  `apiwitchcraft.duckdns.org` has been probed by this project at least four times
  and told a DIFFERENT STORY EVERY TIME -- "still live", "went quiet, never
  fixed", "answers and the payTo is repaired", and on 2026-09-06 six consecutive
  HTTP timeouts against a host whose TLS handshake completes on the first try.
  The `payee_syntax` entry documents that confusion at length, which is the tell:
  the confusion was never about the host, it was about US. Every probe OVERWRITES
  the last -- `data/liveness.json` is an undated snapshot and `asset_coverage.json`
  carries a single `generated_at` -- so "have we seen this before?" could only ever
  be answered from memory. This is the memory: append-only, dated, one line per
  observation.
  THE DESIGN DECISION THAT MATTERS, because getting it wrong MANUFACTURES EVIDENCE
  against innocent sellers: OUR OWN FAILURES ARE RECORDED SEPARATELY FROM THEIRS.
  A probe we declined (the SSRF guard refused the URL) or one that died inside our
  own network is `skipped` -- a fact about us -- and `summarize` EXCLUDES skips from
  every judgement, so a broken proxy or an over-strict URL guard can never build a
  case against a seller who was fine throughout. Only an attempt that reached the
  wire and got nothing is `unreachable`, and even that is worded "we could not
  reach you", NEVER "you were down": today's six timeouts came through the proxy
  while a direct TLS handshake to the same host succeeded, and from here those two
  are INDISTINGUISHABLE. The ledger records OBSERVATIONS, never uptime, and
  `test_reachability_ledger` asserts that no wording claims a seller's state in
  EITHER direction (claiming they are UP is the same error as claiming they are
  down). FOUR STATES, and the one that resolves the whole mess is `flapping`:
  answered before, silent now -- which is exactly what four sessions kept
  mistaking for "fixed" or "dead" depending on the day they looked. A
  `silent_run` needs BOTH >= RUN_FOR_CONCERN attempts AND >= DAYS_FOR_CONCERN
  days, so three timeouts in ten minutes stays a blip rather than becoming a
  condition; `never_answered` is checked BEFORE the run rule because it is the
  more specific claim.
  PRE-MERGE AUDIT FINDINGS (2026-09-07), all fixed, and the first is the one this
  module exists to prevent: (1) HIGH -- observations were recorded against
  `hosts[0]` rather than the host actually PROBED. 58 of 266 corpus payees
  advertise more than one host, `probe_resources` returns the FIRST ANSWERING
  resource, and on 24 of them the probe can land on a different host than the
  first listed -- writing false evidence in BOTH directions: a silent host
  credited with a sibling's success, and a host nobody tried charged with a
  failure. The same cross-attribution class as the two-businesses-in-one-report
  bug, committed INSIDE the module built to stop it. (2) MEDIUM -- UNBOUNDED
  GROWTH from public traffic: 514 probeable corpus hosts against the portal's
  15-minute cache is ~49k rows/day (~16 MB), with no cap, and `load` scans the
  whole file per report -- so it degrades the thing it serves (measured 0.09s at
  50k rows, climbing). Now size-triggered per-host compaction keeping the recent
  tail; the CONTRACT is a FILE bound, NOT an instantaneous per-host ceiling,
  stated precisely because the loose version misled its own test. (3) LOW --
  `keep=KEEP_PER_HOST` as a DEFAULT ARGUMENT captured the constant at definition
  time, leaving the documented retention knob silently inert. (4) LOW -- `source`
  was hardcoded, so the record could not distinguish a public portal visit from
  an operator CLI run, the exact distinction needed to read it back.
  Folded into `seller_report.assess_reach`, which is what
  stops a single timeout reading identically to a three-week silence -- but NEVER
  changes the severity: more observations make the STATEMENT stronger, not the
  accusation, so reachability stays `unknown` however long the run. Fail-soft in
  both directions (a report must not break because a log is unwritable, nor be
  blocked because one is unreadable), and the path is `BLACKWALL_REACHABILITY` so
  a deploy points it at the persistent disk -- the root `.gitignore` excludes
  `*.jsonl`, so a container otherwise boots with no memory, and the memory is the
  whole feature. CLI: `python reachability_ledger.py [host]`. Tests:
  (5) MEDIUM, closed after being written off as acceptable -- compaction could
  lose a row appended by another PROCESS mid-rewrite, and the docstring argued
  that away as a milliseconds-wide window. `_LOCK` is a THREADING lock and the
  engine and portal are separate processes, so it never ordered them at all.
  MEASURED with locking disabled: 372 of 1500 rows lost, 25%. Now an advisory
  `flock` taken by every writer; measured 1500 of 1500 survive the same race.
  Tests: `test_reachability_ledger.py`, 38 tests, 22 mutations verified killed),
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
  `payto_baseline.py` (IS THIS THE RECIPIENT THIS ENDPOINT HAS ALWAYS USED?
  x402 v2 made `payTo` DYNAMIC -- per-request routing "to addresses, roles, or
  callback-based payout logic", and the field "is no longer static". A real
  feature for marketplaces, and a new attack: a compromised or hostile endpoint
  names an attacker's wallet and is paid the RIGHT PRICE by the WRONG PARTY. The
  amount is in budget, the asset is right, the signature is valid, and NO
  SPENDING CONTROL IN THIS MARKET SEES IT. The ecosystem's published mitigations
  are "implement recipient allowlists" and "log and alert on first-seen payment
  addresses" -- the THIRD instance of the gap `integrations/agentcore/` documents
  about AWS and `integrations/lucid/` about Lucid (a STATIC LIST A HUMAN TYPED),
  and the second half is a COLD-START PROBLEM STATED AS A CONTROL: it fires on
  every legitimate new counterparty, which is how an alert gets turned off.
  Blackwall already scores whatever `payTo` arrives per request and never assumed
  a stable recipient; what it could not say is the ENDPOINT-RELATIVE fact. A
  cold-start HOLD is NOT that claim -- it clears the moment the swapped address
  has any history, and it says nothing about the endpoint.
  `ecosystem_scan` already writes per-payee resources to `data/directory.json`;
  inverting that gives host -> {advertised payees}. Built ONCE AT BOOT from OUR
  OWN COMMITTED CRAWL -- never from the request, never from a live fetch, and
  NEVER LEARNED FROM TRAFFIC, because a baseline learned from requests would let
  an attacker teach us their address and then pay it (the `advertised_prices`
  rule). MEASURED BEFORE SHIPPING, the way sybil_ring graduated: 266 payees, 514
  distinct hosts, and **8 hosts (1.6%)** advertise more than one payTo
  (api.aidress.ai 6, blockrun.ai 3, api.arkm.com 2, four gedx402 subdomains 2
  each). So 506 of 514 (98.4%) have exactly ONE recipient on record, and 0 of
  3827 (host, payee) pairs the crawl itself recorded flag -- in lowercase AND in
  EIP-55 checksummed form, which is the join that silently missed 64 of 69 live
  endpoints in `advertised_prices` and would here read as an ATTACK on the real
  recipient of every EVM endpoint in the ecosystem. `test_payto_baseline`
  COMPUTES those figures from the artifact rather than restating them.
  A HOST THAT ROTATES HAS NO BASELINE -- the one judgement call, and the hardest
  case is the one that LOOKS most like the attack: a known multi-payee host names
  a recipient we have never seen. From here that is indistinguishable from a
  marketplace onboarding a tenant, so it grades `multi_payee` and is NEVER gated.
  Gating it would put api.aidress.ai permanently on the wrong side. The
  `payee_syntax.invalid_hex` discipline: record what you cannot defend gating on.
  DEFAULT OFF (`PAYTO_BASELINE_GATES`): 1.6% is the HOST-level false-flag
  ceiling and the REQUEST-level rate cannot be derived from the corpus -- one
  high-traffic multi-tenant host could dominate live traffic while being one row
  here. HOLD-only, never STOP (inference from our own crawl, not proof).
  Fail-open in every direction -- unknown host, missing artifact, relative
  resource, absent counterparty all read `unknown`, because the live ecosystem is
  larger than 514 hosts and gating on absence would HOLD nearly everything (the
  `reachability_ledger` rule: our own missing data must never become a case
  against a seller).
  AUDIT FINDING, found by MEASURING the artifact rather than reading the code:
  `data/directory.json` CARRIES NO TIMESTAMP and was last touched 18 days before
  this landed. A seller may legitimately rotate its payout wallet, and against a
  stale baseline that ordinary event is indistinguishable from a swapped
  recipient -- so the gate would manufacture evidence against a seller who did
  nothing wrong. `index_age_days` reads an explicit `generated_at` (the shape
  `asset_coverage.json` already uses) and returns None for the bare-list shape;
  an UNKNOWN age counts as STALE, and a stale baseline RECORDS but never gates
  even with the lock on. DELIBERATELY NOT `getmtime`, which is the obvious
  implementation and is wrong here: a container clones the repo at build time, so
  every committed artifact's mtime is the BUILD date and an arbitrarily old
  corpus would read as minutes old -- the `chain_backfill` `age_days` inversion
  exactly, and the same shape as `payee_syntax`'s "0 malformed" meaning 0 SEEN.
  So dating the artifact is a PRECONDITION for the lock, not just flipping it;
  the boot banner reports the lock and the corpus age SEPARATELY because two of
  the three states look like "on", and a test asserts the shipped corpus is
  currently undated so a future change cannot quietly make the gate live.
  KNOWN LIMITS, all three stated in the module: (1) `resource` is
  CLIENT-SUPPLIED, so this defends an HONEST agent against a HOSTILE ENDPOINT --
  which is the v2 attack -- but a caller that forwards the value out of the 402
  CHALLENGE rather than the url it DIALED lets the endpoint choose the host key;
  `x402.canonical_resource_url` exists because we learned this field is
  attacker-influenced on our own server. (2) A SELLER CAN OPT OUT by advertising
  two payTos and becoming `multi_payee`; acceptable because the gate is strictly
  additive, so evading it returns the payee to the STATUS QUO (cold-start HOLD,
  sanctions, price anomaly and the Sybil gates all still apply) and grants
  nothing. Same mechanism means an attacker who gets a resource claim on someone
  else's host into our crawl can DISABLE the gate for that host -- fail-open,
  which is the right direction for a poisoning we cannot yet verify against.
  (3) A HOST IS NOT AN OPERATOR: two businesses can share one, and 58 of 266
  corpus payees span hosts.
  MEASURED COST: 70.9ms and 261KB to index 514 hosts at boot; 1.1-2.5us per
  verdict against a ~2ms verdict, so ~0.1%. Redteam: 1 attack (KNOWN GAP BY
  DESIGN while the lock is off -- flipping it turns the scorecard to 32 caught /
  2 gaps / 0 false positives, verified) + 4 restraint controls (the endpoint's
  own EIP-55 recipient, an unseen recipient on a multi-tenant host, an uncrawled
  endpoint, and a mismatch against an undated baseline). Verified on the REAL
  boot path in all four states and over REAL HTTP. 25 mutations verified killed,
  TWO of which SURVIVED the first pass and were both test defects worth naming:
  the host-sanitizer test asserted `_safe_text` DIRECTLY and claimed a control
  character "cannot survive into a host key at all", so deleting the sanitizer
  from `assess_payto` left every test green -- `urlsplit` strips ONLY CR, LF and
  TAB, and NUL, ESC and DEL pass straight into `.hostname`, making that sanitizer
  LOAD-BEARING rather than defense-in-depth (a COMPLETE ANSI sequence is refused
  one layer down because `[` makes urlsplit raise "Invalid IPv6 URL", but that is
  an accident of a bracket, not a guard this module owns); and the empty-set
  branch had no test, so `if not advertised` -> `if advertised is None` let a
  host mapped to an empty set reach the single-element unpack and raise
  ValueError out of a function documented never to raise. A THIRD test was wrong
  on first run and caught by the suite itself: it demanded `multi_payee` for a
  recipient the fixture explicitly advertised. Tests:
  `test_payto_baseline.py`, 70 tests incl. a REAL server),
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
  `cdp_bazaar_check.py` (are we in the CDP Bazaar catalog yet? NEEDS NO
  CREDENTIALS -- it used to mint a Bearer JWT and refuse to run without
  `CDP_API_KEY_ID`/`CDP_API_KEY_SECRET`, so the check went unrun for that reason
  alone; MEASURED 2026-09-15 both `/discovery/resources` and `/discovery/search`
  answer 200 UNAUTHENTICATED, which makes sense for a marketplace. THE SEARCH
  ENDPOINT CANNOT PROVE ABSENCE and that is the trap: `?q=` IS honoured when
  there are matches (`q=onesource` -> 19 of 20 contain it) but on a MISS it
  silently returns 20 ARBITRARY entries with `partialResults: true`, so a miss
  looks like a page of unrelated sellers -- search may only CONFIRM a hit, and
  absence is settled by the full offset-paginated scan against the
  `pagination.total` the API states (15,572 entries). Needles are now a host and
  the payout address ONLY: the bare product name was one, and since matching is a
  substring test over each entry's whole JSON, the search miss-fallback could
  match somebody else's description and report us LISTED when absent. Exit codes
  0/1/2 (listed / not yet / inconclusive) -- every outcome used to exit 0, so a
  scheduled run could not tell them apart. FIRST RUN after the first CDP
  settlement: NOT listed, full 15,572 scanned. Likely cause MEASURED against
  2000 listed entries -- `resource` is an absolute URL STRING in 2000/2000 and
  `extensions.bazaar.info` present in 2000/2000, while we advertise a DICT whose
  `.url` is RELATIVE and emit only `schema`. Stated as a hypothesis, not a proof:
  the catalog entry is what CDP stores and may be normalized, but an indexer
  cannot invent our host from a relative path. See `docs/BAZAAR_LISTING.md`),
  `ROADMAP.md`, `docs/DATA_SOURCE_SPIKE.md`. Tests:
  `test_blackwall.py`, `test_ledger.py`, `test_reputation_onchain.py`,
  `test_settlement_watch.py`, `test_addresses.py`, `test_x402.py`,
  `test_mcp_server.py`, `test_reputation_store.py`, `test_facilitator.py`,
  `test_discovery.py`, `test_sanctions.py`, `test_readiness.py`,
  `test_ap_gate.py`.

> **The command above must list EVERY root `test_*.py`.** Found 2026-09-07 during
> a pre-merge audit: `test_approvals.py` (47 tests, the approval-binding /
> single-use / expiry / STOP-is-never-approvable properties) and
> `test_token_decimals.py` (47) existed on disk, were named in the prose above,
> and were NOT in this command -- so 94 tests, including the whole approvals
> security suite, never ran in the documented check. `test_deploy_manifest.py`
> now asserts the list matches the directory, because a canonical command that
> silently skips files is worse than no canonical command.
>
> **The guard checks for OMISSIONS, not for DUPLICATES**, and that gap hid one:
> `test_billing_preflight.py` was listed TWICE in the `Makefile` `test` target,
> so its 115 tests ran twice and the reported total read 2725 instead of 2601.
> Found 2026-09-15 by refusing to accept a count discrepancy between two runs of
> what looked like the same file set -- not by any test. Harmless to correctness
> and actively misleading about coverage, which is the same failure mode as the
> omission above pointing the other way. Note the three lists are legitimately
> NOT identical in length: `Makefile:test` and the CI step carry 100 files, while
> CLAUDE.md's command carries 101 -- `test_remote_ledger.py` needs
> `cryptography` and runs in `make test-native`, deliberately kept out of the
> stdlib-only run.

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
 test_auth_sim.py test_directory_liveness.py test_price_corroboration.py test_advertised_prices.py test_deploy_manifest.py test_receipt_signer.py test_x402_challenge.py test_x402_pay.py test_screen_payer.py test_mcp_http.py test_upto_scheme.py test_asset_coverage.py test_payee_syntax.py test_payto_baseline.py test_honeypot.py test_billing_preflight.py test_seller_report.py test_seller_portal.py test_reachability_ledger.py test_approvals.py test_token_decimals.py test_hmac_key.py \
 test_bounded_server.py test_ci_coverage.py test_remote_ledger.py test_seller_intel.py test_solana_backfill.py test_user_agent.py test_volume_integrity.py
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
