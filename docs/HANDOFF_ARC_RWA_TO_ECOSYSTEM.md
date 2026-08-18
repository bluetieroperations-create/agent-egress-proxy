# Handoff — the RWA → simulation → ecosystem-reality arc

**For:** the Traceipt session (`claude/x402-product-ideas-6adgah`, `traceipt/`), and
any future Blackwall session picking this up cold.
**Covers:** `4ada19a` (2026-08-16) → `515919c` (2026-08-18) — 39 commits,
22 new modules, 22 new test modules, ~14k lines.
**Companion to** `docs/HANDOFF.md` (branch rules, ownership, the shared seam). Read
that first for the "don't overwrite each other" rules; this is the *what changed*.

---

## TL;DR for Traceipt

Three things here are yours to use. Everything else is context.

1. **A measured map of the live x402 seller ecosystem** — `data/liveness.json`,
   195 hosts probed 2026-08-18, with per-host payer counts and prices. Your
   `OUTREACH_TARGETS.md` is a *hypothesis* list curated from awesome-x402 and
   blogs; this is *measurement*. **Only 3 of your 19 named targets appear in it**
   (§5). That is not a claim your list is wrong — it is a claim the two lists
   barely overlap, and the 38 names in ours that you don't have are backed by
   observed on-chain settlement.
2. **A finding that touches your service directly** — nothing in this repo reads
   the `WWW-Authenticate` 402-challenge carrier, and Traceipt's `x402_gate.py`
   *emits* challenges. If you emit body-only, you are compatible with the
   majority but invisible to v2-header-only clients. Worth a five-minute check
   on your side (§4).
3. **A reusable prober** — `directory_liveness.py`, stdlib, injected network.
   Point it at any payee list and it tells you who is live and parseable.

**No action required from Traceipt on anything else in this arc.** The RWA and
simulation work is Blackwall-internal and does not touch `traceipt/` or the
`traceipt_*.py` seam. No schema changes were made to the shared seam in this arc.

---

## 1. The map: what the survey did, in order

This is the "map" of the last piece of work, since it is the part with the most
reuse value outside Blackwall.

```
data/directory.json          198 payees, harvested 2026-08-02 by ecosystem_scan.py
  │                          = what sellers ADVERTISE in the Bazaar
  │
  ├─ targets_from_directory  dedupe to 195 distinct HOSTS
  │                          (payees advertise dozens of routes on one host;
  │                           probing every route = 2500 requests for no new info)
  │
  ├─ probe_host              GET the first advertised route
  │    │                     ├─ 402 → parse_challenge(body, headers)
  │    │                     ├─ not-402 → RETRY with POST {}     ← artifact #1
  │    │                     └─ transport error → dead
  │    │
  │    └─ parse_challenge    look in BOTH carriers:               ← artifact #2
  │         ├─ body   {"x402Version":2,"accepts":[…]}      (v1 style, majority)
  │         └─ header WWW-Authenticate: X402 requirements="<base64 json>"  (v2)
  │
  ├─ classify                body_accepts / hdr_accepts / wellknown /
  │                          opaque_402 / other / dead / blocked
  │
  └─ rank_leads              scoreable AND ≥8 distinct payers AND own domain
                             → 39 names worth a conversation
```

**The question it answers is not "is it up."** It is *"can we read its payment
requirements"* — because `forecast` scores from the challenge's `accepts[]`. An
endpoint that is alive but advertises in a format we don't parse is, operationally,
the same as one that is down. Traceipt has the mirror-image version of this
question: an endpoint whose settlement you can't parse is one you can't receipt.

### Result (2026-08-18, 195 hosts)

| class | hosts | meaning |
|---|---:|---|
| `body_accepts` | 71 | `accepts[]` in the body. Scoreable today. |
| `hdr_accepts` | 2 | v2 header style. Scoreable only if the caller reads the header. |
| `wellknown` | 23 | No inline challenge; `/.well-known/x402.json` serves a catalog. |
| `opaque_402` | 86 | 402 with nothing machine-readable anywhere. |
| `other` | 12 | Answers, but not a 402 — route moved, keyed, or retired. |
| `dead` | 1 | DNS/TLS/connection failure. |

**73 of 195 (37%) live and scoreable**, advertising 2,524 priced resources.

The `opaque_402` bucket is the one nobody expected: **44% return a bare 402 with
nothing actionable.** `pro-api.coingecko.com` answers
`{"error":"Payment required","message":"Payment is required to access this resource"}`.
`api.ipintel.ai` answers `{}`. Both advertise a price in the Bazaar while serving a
challenge no agent can act on without out-of-band knowledge. **"Listed in the Bazaar"
is not "transactable"** — that is the single most transferable conclusion here, and it
applies to any ecosystem-size claim either project makes.

---

## 2. Two measurement artifacts — both undercounted, both were my errors

Documented because a future session will hit them, and because both made the
ecosystem look deader than it is. Guarded in code, mutation-tested.

**GET-only probing.** A `405 Method Not Allowed` is a POST endpoint, not a dead one.
Retrying the non-scoreable hosts with `POST {}` recovered **14 live scoreable hosts**,
including `api.anchor-x402.com` (52 distinct payers) and `api.loyalspark.online` (50).

**Body-only challenge parsing.** The v2 style carries requirements in a
`WWW-Authenticate` header. Reading only the body classified ~80 hosts as "402 with no
`accepts[]`" — in the output, *identical to a broken endpoint*. The tell was
statistical: 80 simultaneous failures of the same kind is a parser bug, not 80
broken servers.

**A third caveat needs no code:** single-run results carry transient noise.
`hirescrape.com` failed its TLS handshake in one pass and returned a clean challenge
in the next. **Treat any single host's `dead` as provisional.** Re-run before acting
on one.

---

## 3. What the whole arc built (39 commits)

Grouped by the question each group answers. All stdlib, all fail-open, all opt-in
behind an env flag, all with `test_*.py`. **Core verdict path is byte-identical when
the flags are off** — that is the invariant the whole arc preserved.

### 3a. Tokenized-RWA pre-trade gate — "will this security transfer actually land?"

The wedge: an agent buying a tokenized RWA pays USDC (so ~90% of the existing stack
applies to the payee), but the *asset* leg can silently fail — a permissioned security
reverts a transfer to a wallet that isn't KYC'd, is frozen, or while paused. Pay
stablecoins, receive nothing.

| module | does |
|---|---|
| `rwa_readiness.py` | reads the receiver-side restriction interface; folds a grade |
| `solana_rwa.py` | the SPL Token-2022 leg (Backed/Ondo settle heavily on Solana) |
| `tokenized_stock_registry.py` | token contract → issuer/underlying/ISIN. Descriptive, never gates |
| `pyth_price.py` | peg/NAV divergence — are we paying near the real stock price? |
| `backed_oracle.py` | proof-of-reserves backing gate + authoritative Pyth feed ids |
| `dex_price.py` | the token's real *market* price from a Uniswap-v3 pool (what the oracle can't see) |
| `holder_concentration.py` | one dominant non-contract wallet ≥50% of supply |
| `aave_reserve.py` | is it vetted enough for Aave to list, and has Aave since frozen it |
| `rams_readiness.py` | ERC-8226 agent-authorization axis. **Dormant** until a request advertises a mandate registry |
| `rwa_aggregate.py` | the confidence layer — tiers each signal `gate` vs `advisory`, one weighted risk view |

### 3b. The accumulation corpus — the flip from *reading* public data to *owning* a private one

| module | does |
|---|---|
| `rwa_ledger.py` | append-only JSONL of every RWA buy + context; per-asset/issuer rollups |
| `rwa_outcomes.py` | the T+N labeler — "what we decided" → "what actually happened" |
| `rwa_balance.py` | definitive settlement label via pre/post balance delta |
| `rwa_backfill.py` | **seed the corpus from public chain history with zero customers.** On-chain history is already labeled: a transfer that landed IS a settlement |
| `rwa_report.py` | turn the corpus into operator intelligence — issuer directory, overpriced/underwater leaderboards |
| `issuer_trust_gate.py` | graduate the earned per-issuer grade into the verdict |

### 3c. Simulation — the technique that replaced interface probing

**The pivotal negative result of the arc.** Interface probing *failed*: all 535 corpus
tokens × 9 probes → 535/535 alive, **0/535 exposing any permissioned interface**. So
`rwa_readiness` answered "unknown" for the entire corpus.

Simulation needs no interface. Ask the chain "would this transfer succeed?" via
`eth_call`, then decode and attribute the revert.

**The control is the point.** Every assessment runs **twice** — target address and a
control address — so a revert is attributed only when they disagree:

```
target fails + control OK   → RECEIVER is blocked
both fail                   → SENDER is at fault, never blamed on the receiver
```

| module | does |
|---|---|
| `transfer_sim.py` | the shared simulation core + control attribution |
| `settlement_sim.py` | pre-signature x402 USDC feasibility — is the payee Circle-blacklisted? |
| `auth_sim.py` | simulates the *actual* EIP-3009 `transferWithAuthorization`: replay (nonce state read directly), expiry (pure clock check), execution |
| `revert_scan.py` | reads *failed* transfer attempts from chain history, classifies the revert reason |
| `rpc_node.py` | our own JSON-RPC front door — single egress point, cache, single-flight, method allowlist, self-host switch |

**Verified live on mainnet**, not just in tests: a real Circle-blacklisted payee →
`payee_blocked` with the real revert string; STBT and BUIDL → blocked with their real
strings; a clean payee → `ready` and ungated; `authorizationState` returns clean false
for an unused nonce.

### 3d. Two locks that stayed OFF, deliberately

The arc's discipline: a signal is **descriptive first**, and only graduates to gating
when measurement shows ~0 false flags. Two did not graduate.

- **`ISSUER_TRUST_GATES = False`** — descriptive until the corpus proves it.
- **`REVERT_AXIS_GATES = False`** — **and this one should stay off permanently.** The
  axis activated (BUIDL 20 restriction reverts / 9.1%, STBT 7 / 3.4%) and its first act
  was to try to downgrade **BlackRock** to LOW *because its lock-up and registry checks
  reject non-allowlisted wallets* — i.e. because it works exactly as designed. **A
  restriction revert measures transfer friction, not issuer untrustworthiness.** Re-home
  it as an asset-level signal; do not flip the lock.

This is the most portable lesson in the arc: *a signal that fires correctly can still
be measuring the wrong thing.* Traceipt has the same exposure anywhere a receipt's
absence gets read as a negative signal rather than as missing data.

### 3e. Infrastructure

- **CI** (`.github/workflows/tests.yml`) — the full suite + `redteam.py` as a *gate*, on
  every push and PR, matrixed 3.11/3.12, plus a separate job for the integration
  surfaces. It caught a real Python 3.12 production crash on its first run
  (`{}[2:]` became `KeyError` instead of `TypeError` — gh-101264).
- **`redteam.py`** gained a `SIM_SCENARIOS` family driven through `forecast` with
  injected sources. Now **24 attacks caught, 2 documented gaps, 0 false positives.**
  It pins *restraint* properties too: a sender-side revert is not blamed on the
  receiver, an underfunded payer does not gate, an unreachable RPC fails open.
- **Seed refresh** — merge-not-replace semantics, `gating_capable` retention guard.

---

## 4. The finding that touches Traceipt

`grep -rin www-authenticate` over this repo returns **zero hits**. Every consumer takes
`accepts` from the JSON body: `clients/x402_pay.py`, `discovery_crawl.py`,
`readiness.py`, `traceipt_attest.py`, `blackwall.py`, and the OpenClaw hook's
402-challenge recognizer.

So a v2 header-style endpoint is **uncrawlable and unpayable** by us, even though the
verdict engine would score it fine if handed the requirements. Not a verdict bug — a
*reach* bug.

Measured impact is honestly small: **2 of 195 hosts.** But one is `blockrun.ai`, second
on the lead list by distinct payers with 27 resources — and this is the *newer* spec
style, so the count should grow, not shrink.

**Why this is Traceipt's business:** `traceipt/traceipt/x402_gate.py` *emits* 402
challenges (`server returns 402 with accepts = payment requirements`). Two questions
worth five minutes on your side:

1. Do you emit body-only? If so you're compatible with the majority today, but a
   v2-header-only client can't see your price.
2. Does anything on your side *consume* a third-party challenge? If so it inherits
   this same blind spot.

`directory_liveness.parse_challenge()` is the reference implementation — ~25 lines,
stdlib, tolerant (a malformed header must not mask a good body), and unit-tested
including the case-insensitivity of the scheme token, which real servers vary.

**Not fixed here on purpose.** The fix belongs in each consumer, and changing payment
parsing is not something to slip into a survey commit.

---

## 5. Your outreach list vs. the measured map

Cross-referenced `traceipt/OUTREACH_TARGETS.md` against `data/liveness.json`:

| Traceipt target | in the measured map? | class | distinct payers |
|---|---|---|---:|
| Stratalize | **yes** | `body_accepts` | 13 (187 resources) |
| Yield.xyz | yes | `wellknown` (route 404s) | 13 |
| AI Rook | yes | `other` (not a 402 today) | 3 |
| *16 others* | not present | — | — |

**3 of 19.** Read this carefully — it does **not** mean the other 16 are fake or dead.
Our corpus is 198 payees derived from a Bazaar crawl, which is itself partial, and
plenty of real operators never list there. What it does mean:

- Your list and the on-chain-settlement-backed list are **nearly disjoint**. They are
  measuring different things — yours measures *who talks about x402*, ours measures
  *who has observed settlements*.
- Of the three that do overlap, **only Stratalize is strong** (parseable challenge, 13
  distinct payers, 187 priced resources). Yield.xyz's advertised route 404s. AI Rook
  has 3 payers.

**38 of our 39 leads are not on your list.** Top of that set, all with parseable
challenges and measured distinct payers:

| payers | resources | host | price |
|---:|---:|---|---|
| 134 | 8 | `api.bitrefill.com` | $0.001 |
| 61 | 27 | `blockrun.ai` | $0.0085 *(header-style)* |
| 53 | 6 | `x402.asterpay.io` | $0.01 |
| 52 | 19 | `api.anchor-x402.com` | $0.005 |
| 50 | 29 | `api.loyalspark.online` | $0.01 |
| 48 | 10 | `company.payapi.market` | $0.001 |
| 34 | 16 | `api.nansen.ai` | $0.01 |
| 28 | 29 | `2s.io` | $0.0025 |
| 18 | 5 | `agi.apify.com` | $1.00 |
| 16 | 3 | `pro-api.coinmarketcap.com` | $0.01 |

The recognizable businesses — **Bitrefill, Nansen, CoinMarketCap, CoinGecko, Apify,
0x** — are companies large enough to have someone whose job is the audit question
Traceipt sells against. Bitrefill alone shows 134 distinct counterparties.

**`distinct_payers` is the ranking key on purpose:** an operator can inflate settlement
count by paying themselves, but not the number of independent counterparties. It is the
hardest-to-fake number in the dataset.

### Two caveats on these numbers

- **`settlement_count` saturates at 150** for most rows, because the backfill paginates
  a capped window. The *ordering* is sound; the absolute settlement figures are floors,
  not totals. Do not quote them as volume.
- **This measures reachability and format, not demand.** That 73 endpoints are live and
  parseable says valid inputs exist. It says nothing about whether their operators want
  a verdict layer or a receipt layer. Agents paying these endpoints is *necessary* for
  either pitch to matter, and not *sufficient*.

---

## 6. Where things live

| what | where |
|---|---|
| the prober | `directory_liveness.py` (stdlib; `python3 directory_liveness.py`) |
| its tests | `test_directory_liveness.py` (22 tests, mutation-noted) |
| the write-up | `docs/DIRECTORY_LIVENESS.md` |
| raw results | `data/liveness.json` (195 hosts, 2026-08-18) |
| the advertised map it probes | `data/directory.json` (198 payees, 2026-08-02) |
| RWA design notes | `docs/TOKENIZED_RWA.md` |
| RPC self-hosting | `docs/RPC_SELFHOST.md` |
| branch rules & ownership | `docs/HANDOFF.md` ← read before pushing |

**Branch state at handoff:** this arc landed on `main` through PRs #6, #7, #9. The
survey is on **`survey/directory-liveness`** (off `main`, pushed, no PR opened).

It is deliberately **not** on `claude/blackwall-x402-integration-j3rdab`: **PR #5 is
open** on that branch for unrelated MCP-HTTP work, and that branch is 38 commits behind
`main` with no CI. Do not stack unrelated work onto it.

**Verification at handoff:** 1,423 tests green on `main`'s tip, `redteam.py` green
(24 caught / 0 false positives / 0 misses), prober runs end-to-end against all 195
hosts in ~46s.

---

## 7. Open threads (not blocked, just not done)

1. **Close the `WWW-Authenticate` gap** in the consumers (§4). Small, concrete,
   `parse_challenge` is the reference.
2. **Re-home the revert axis** as an asset-level readiness signal, beside
   `rwa_readiness`. Do **not** flip `REVERT_AXIS_GATES`.
3. **Re-run the survey periodically.** It is a snapshot; single-host `dead` is noisy.
   A cadence (monthly?) turns it into a trend — *which* endpoints are gaining payers is
   a better lead signal than a one-time rank.
4. **The deferred #1–3 from the prior session:** merge PR #10 + enable Actions'
   "create and approve pull requests" permission; re-run seed-refresh; restore the dead
   Render endpoint / turn on `BLACKWALL_SETTLEMENT_SIM` / get one user.
5. **Nothing here proves demand.** Both projects now have good measurement of *supply*.
   Neither has a user. That gap is not closeable by more building.
