# Audit: hardcoded asset decimals — 2026-08-27

Prompted by the corpus finding that chain **4217 (Tempo)** is live and being paid,
which made "everything is Base USDC" worth testing rather than assuming.

## Severity table

| # | Site | Effect | Severity | Status |
|---|---|---|---|---|
| 1 | `blackwall.forecast` → `check_payment_authorization(..., decimals=6)` | Atomic comparison mis-scales by 10^(d-6). **Bypasses the payload-sim STOP authority.** | **HIGH** | Fixed |
| 2 | `wallet_guard.claim_from_tx(token_decimals=6)` | Amount handed to the verdict wrong by 10^(d-6), **in the signing path**. Cap bypass. | **HIGH** | Fixed |
| 3 | `wallet_guard` placeholder `amount = "0.000001"` | Conflates "no value moved" with "amount unscalable", presenting any transfer as trivial | **HIGH** | Fixed |
| 4 | `discovery_crawl.price_observations` | Baselines wrong for non-6dp payees; **price-anomaly gate silently inert** for them | **HIGH** (re-graded) | Fixed |
| 5 | `settlement_sim._base_units(decimals=6)` | Simulated transfer amount wrong; HOLD-only, never STOP | LOW | Documented, not fixed |
| 6 | `x402.to_atomic`, `discovery.human_price` | Library defaults; correct where callers pass a value | INFO | No change |

## 1 & 2 — demonstrated, not theorised

**Payload-sim bypass.** Claim "1.0 DAI"; agent signs 10^6 atomic
(0.000000000001 DAI). With `decimals=6` the engine computes `want = 10^6`, sees a
match, and the hard STOP **never fires**:

```
CASE B: 10^6 atomic DAI against a 1.0 DAI claim
  matches: True   <-- underpayment ACCEPTED
```

The mirror case is equally wrong: a **valid** 1.0 DAI payment (10^18 atomic) was
reported as a mismatch and became a hard STOP, blocking a correct payment.

**Wallet-guard cap bypass.** Direction depends on the token, and my first guess
was wrong — 18-decimal tokens *over*-report:

| Token | Real transfer | Reported as | Consequence |
|---|---|---|---|
| DAI (18dp) | 1,000 | **1,000,000,000,000,000** | false STOP |
| **GUSD (2dp)** | **50,000** | **5** | **sails under a `hold_above=100` cap** |

GUSD is a real 2-decimal ERC-20. A 50,000-token transfer presented to the verdict
as "5" is a spending-cap bypass in the signing path.

## The fixes

`payload_sim.resolve_decimals(claim, decimals)` — explicit caller value wins, then
a table of known token addresses, then symbols. **Returns None when genuinely
unknown**, and the amount check is then reported as `amount_status:
"unverified_decimals"` with a warning, rather than silently assuming 6. This
mirrors the existing `signer_status: "deferred"` pattern: a GO must never be
mistaken for amount-verified.

`blackwall.forecast` now reads `decimals` from the request (the x402 v2 challenge
carries it in `methodDetails` — see `x402_challenge.py`) and passes it through.

`wallet_guard` resolves decimals from the token contract and, when it cannot,
flags `amount_unverified` — and the guard **forces a human CONFIRM on a GO** whose
amount was never really checked, withholding the signature until approved.

### A bug introduced and caught during the fix

Returning `None` for an unknown token initially flowed into a pre-existing
`amount = "0.000001"` placeholder meant for zero-value calls like `approve` — so
an unknown-token transfer of any size was still presented as trivial. That is the
same bypass wearing a different hat. The two cases are now explicitly separated:
the placeholder stays for calls that move no value, and a real transfer we cannot
scale is flagged and escalated.

## Verification

- 8 new tests in `test_payload_sim.py`, 8 in `integrations/wallets/test_wallet_guard.py`,
  each naming the mutation it kills.
- **798 tests green** across the affected repo suites; 44 green in `integrations/wallets`.
- USDC behaviour explicitly regression-tested as unchanged — it is ~all of today's
  traffic and must not move.

## #4 — measured separately, and re-graded from MEDIUM to HIGH

Measured before changing anything.

**Exposure:** 1,792 of 10,668 crawled payment options are **not USDC (16.8%)**,
across 23 of 198 payees (11.6%). Probing the affected payees live returned real
non-USDC assets, and `decimals()` on-chain (bsc-dataseed, 2026-08-27) says:

| Token | Address | Decimals |
|---|---|---|
| BSC-USD (USDT on BSC) | `0x55d3…7955` | **18** |
| BUSD | `0x8ac7…580d` | **18** |
| USD1 | `0x8d0d…8b0d` | **18** |
| (BSC) | `0xce24…6666` | **18** |

USDT is **6 on Ethereum and 18 on BSC** — precisely the trap a single global
default walks into.

**Damage, measured.** A 1.00 BSC-USD quote entered the baseline as
**1,000,000,000,000**. Peer medians turn out to be resilient — unchanged at 17%
and even 30% poisoning — so the cross-counterparty case was not the problem. The
real failure is a payee whose OWN history is priced in an 18-decimal asset:

| Scenario | Anomaly ratio | Caught? (STOP threshold 8.0) |
|---|---|---|
| 10x overcharge, before fix | **1e-11** | **No — gate silently inert** |
| 10x overcharge, after fix | **10.0** | **Yes** |

That is why this was re-graded: not noisy baselines, but a **price-anomaly gate
that cannot fire at all** for any payee priced in an 18-decimal asset.

**The fix.** `asset_decimals()` resolves per asset; `price_observations` emits an
observation **only when the decimals are known**. Losing an observation costs a
little baseline coverage; keeping one that is wrong by 10^12 disables a gate.

6 new tests in `test_discovery_crawl.py`, including the end-to-end assertion that
a 10x overcharge now exceeds `STOP_ANOMALY_RATIO`.

## Not fixed, and why

**#5 `settlement_sim._base_units`** affects a simulated transfer amount only. That
gate is HOLD-only and never STOPs, and an underfunded result is explicitly
non-gating, so a mis-scaled amount cannot block a payment.

## Residual exposure

`KNOWN_DECIMALS` is a static table. A non-6-decimal token that is not in it
resolves to unknown — which is now **safe** (reported and escalated) rather than
wrong, but it is not *verified*. Reading `decimals()` from the token contract via
the existing `rpc_node.py` path would close this properly, at the cost of a
network call on the hot path.

---

# Closing the remaining items — 2026-08-28

## #5 `settlement_sim._base_units` — fixed

Now resolves decimals from the token rather than assuming 6, falling back to 6
only as a last resort. This gate stays HOLD-only and non-gating, so a mis-scaled
amount could never block a payment — but a simulation run at 10^12 the intended
size is a meaningless simulation, and learning whether the transfer would revert
is the entire point of the call.

## Non-EVM assets — closed

`token_decimals` now reads SPL mint accounts. The layout is 82 bytes with
`decimals` as the single byte at **offset 44**, verified live against USDC on
mainnet-beta (returns 6, owner `TokenkegQfeZ…`).

Two traps handled: the account length is checked, because a token ACCOUNT (165
bytes) is also a valid account and offset 44 there is part of somebody's balance;
and the mint string is **not lowercased**, because base58 is case-sensitive and a
lowercased mint addresses a different, nonexistent account.

Live: Solana USDC -> 6, BSC CAKE -> 18, a non-token -> None.

## Zero-decimals ambiguity — closed

A contract with a catch-all fallback returning zeros was indistinguishable from a
genuine 0-decimal token, and reading 0 for an 18-decimal asset mis-scales by
10^18. Zero-decimal tokens are rare, so the resolver now confirms with **one
extra call in that case only**: a real ERC-20 also implements `totalSupply()`. A
non-zero answer costs no extra round trip.

## Multiple `WWW-Authenticate` headers — closed

HTTP permits the header to repeat, and `dict(resp.headers.items())` keeps only
the last — so a server sending `Bearer` alongside `Payment` could hide the
payment challenge entirely. `www_authenticate_values()` reads every occurrence
(supporting the `email.Message` `get_all` API that `http.client` actually
returns, plus pair lists and plain dicts) and is wired into
`directory_liveness`, `traceipt_attest`, `clients/x402_pay` and
`accepts_from_response`.

## The 49 still-opaque 402 hosts — diagnosed, not a bug

Probed all 49 that remained opaque after the v2 header fix:

- **0 carry a `WWW-Authenticate` header of any kind.** No parser change can reach them.
- 46 return valid JSON, but the bodies are error strings, hints or docs links —
  not payment requirements. Several return literally `{}`.
- Only 2 carry `x402Version` and 2 carry a `price` field.

They return 402 as a status code without ever advertising **how** to pay. That is
a broken or out-of-band endpoint, not a gap in our parsing. The `opaque_402`
class is now fully accounted for: 5 were the header-format gap (fixed), 49 are
genuinely non-advertising, and the remainder were 404/405/dead.

## Final state

| Item | Status |
|---|---|
| payload_sim STOP bypass | Fixed |
| wallet_guard cap bypass | Fixed |
| placeholder conflation | Fixed |
| discovery price gate inert | Fixed |
| settlement_sim base units | Fixed |
| unverified tokens (EVM) | Closed — on-chain, cached forever |
| unverified tokens (Solana) | Closed — SPL mint read |
| zero-decimals ambiguity | Closed — totalSupply confirmation |
| multiple auth headers | Closed |
| 49 opaque hosts | Diagnosed — not our defect |

971 root tests green, 44 in `integrations/wallets`.

---

# Resolving the corpus, not just the mechanism — 2026-08-29

The work above made an unknown asset **safe** (reported as `unverified_decimals`
rather than silently assumed to be 6). It did not make it **known**. This pass
closes that: every asset the live x402 corpus actually advertises was read
on-chain once and committed.

## Method

Re-probed all 195 hosts in `data/liveness.json` through `x402_challenge.parse_challenge`
(so all three carriers are read, not just the JSON body) and collected every
`accepts[].asset` with its network. That yields **41 distinct (network, asset)
pairs**, of which 17 already resolved and **24 did not** — 12% of live options
priced in an asset whose scale we could not verify.

Each unresolved pair was then resolved at its own source:

| Family | How | Result |
|---|---|---|
| EVM (16) | `decimals()` via `token_decimals.py`, routed per chain | all 16 read |
| Solana (2) | SPL mint account, decimals byte at offset 44 | both 6 |
| Algorand (1) | asset params from algod | 6 |
| Stellar (2) | protocol constant (stroops), confirmed via Horizon | 7 |
| Hyperliquid (1) | canonical `spotMeta` `weiDecimals` | 8 |

**Cross-checked, not taken on trust.** Each EVM value was re-read from every
public RPC the chain lists, up to five. 12 of the 16 agreed across **two or more
independent providers**; the other 4 (Monad, Sei, Celo, SKALE) have only one
public endpoint and are marked `1 rpc` in the table. **Zero disagreements.**
The Stellar value is the one entry that is not a contract read, so it carries two
independent corroborations: Horizon reports that asset's balances as
`"0.0000000"` — exactly 7 fraction digits — and two unrelated hosts advertise
their Stellar leg at exactly 10x their 6-decimal legs. Horizon also confirms the
`contract_id` of the classic asset is the same SAC the corpus advertises.

## What the corpus turned out to contain

**Two of the 41 pairs are not 6 decimals, and one is not even dollars.**

* **JPYC on Polygon (`0x431D…7BDB`) is 18 decimals — and denominated in yen.**
  A live seller (`api.anchor-x402.com`) advertises `amount=1000000000000000000`
  on that leg, i.e. **1.0 JPYC**, alongside a 6-decimal Solana leg priced at
  `5000` ($0.005). Read at the corpus-default 6 that quote is 1,000,000,000,000
  — a trillion — which is exactly the mis-scaling this gate exists to prevent.
* **Stellar is 7, Hyperliquid is 8.** Neither is exotic; both are simply not 6.

Knowing the scale is **not** knowing the price. JPYC is yen and EURC is euro, so
even correctly scaled they are not comparable to a USDC quote. The price gates
continue to observe **only** known 6-decimal USDC (`_is_usdc6`), which stays
correct — a currency conversion is a separate problem and is not attempted here.

## Why a second, chain-keyed table

`KNOWN_DECIMALS` is keyed by **address alone**. That is sound only while every
entry agrees across chains, which held while the table was all USDC. It stops
holding the moment an entry is 18: the same 20 bytes are a different contract on
a different chain, and nothing prevents an address collision between a 6-decimal
token and an 18-decimal one. So the 22 new values live in
`KNOWN_DECIMALS_BY_CHAIN`, keyed by `(network, asset)`, consulted **before** the
flat table, and are deliberately **not** added to it. `chain` is a required
request field, so this covers every real request; a claim with no chain resolves
to unknown, which is the module's existing safe answer.

## Audit finding — HIGH: a correct payment was hard-STOPPED

Found while exercising the new coverage on the real HTTP path, and **independent
of this change** — it simply could not be seen before, because every asset we
could test with was on Base.

`x402._CAIP2` mapped only Base and Ethereum, and `to_caip2` returns an unknown
name unchanged. The payload-sim network check compares
`to_caip2(payment.network)` against `to_caip2(claim.chain)` — so an agent that
spelled its chain the ordinary human way, `"polygon"`, against a correct payment
on `eip155:137`, compared `"polygon"` to `"eip155:137"`, called it a network
mismatch, and returned **`STOP`, `hard_stop: true`** on a payment that was
entirely correct:

```
signed payment network eip155:137 != the scored chain polygon
```

This is the worst error class for this product — refusing a legitimate payment —
and it applied to **every non-Base chain**, which is most of the corpus (Polygon,
Arbitrum, BSC, Avalanche, Celo, Sei, Monad, X Layer, World Chain, and more).
Sellers write bare names too: 15 corpus entries advertise `"base"` and 3
advertise `"solana"`.

**Fixed** by extending `_CAIP2` with the corpus's chains. Every added mapping is
**unambiguous** — a truthful name for exactly one chain — so making them compare
equal cannot let a payment on one chain pass as a payment on another; it only
stops us calling one chain two different things. Ambiguous names are deliberately
**absent**: bare `"solana"` names neither mainnet nor devnet, and guessing there
*would* be a real loosening. Verified live end-to-end: the correct payment now
scores clean, a payment genuinely on Base against a Polygon claim still hard-STOPs.

## Second pass — audit / eval / verify on the staged change

Re-run adversarially against the merged tree, not just the branch.

### Findings

| # | Severity | Finding | Status |
|---|---|---|---|
| 1 | **MEDIUM** | An amount carrying more precision than the asset has (`0.00000001` on the 7-decimal Stellar asset) makes `to_atomic` return `None`. The three distinct reasons a comparison could not happen were collapsed into one message that always read as a comparison — *"signed payment value is 0.01 but you asked me to score 0.00000001"* — while `amount_status` still reported **`verified`**. That asserts a check that never ran: the same category error the surrounding code exists to prevent. Newly reachable, since 7- and 8-decimal assets are new. | **Fixed** — `unrepresentable_amount` / `unreadable_payment_value` statuses and reasons that say what actually happened. 5 tests. |
| 2 | LOW (deliberate) | Widening the aliases means a claim naming **Base USDC on a non-Base chain** can now build an EIP-712 domain where it previously could not, so signer recovery runs and fails — a **hard STOP** where the old behaviour was a warning. The pair is incoherent (that token is a Base deployment) and the signature genuinely binds a different chainId, so the stop is correct. | **Pinned** with 2 tests so it stays intentional. |
| 3 | None | All 25 `_CAIP2` aliases checked one by one against the chain registry — every name maps to the chain it actually denotes. | Clean |
| 4 | None | Chainless callers (`settlement_sim`, `wallet_guard`, which call `resolve_decimals({"asset": …})`) are unchanged: the new entries need a chain and correctly answer `None` without one. | Clean |
| 5 | None | 8 hostile/malformed claims (non-string asset, list chain, NUL bytes, a 10,000-char asset, padded whitespace, mixed case) — no raise, correct answers. | Clean |
| 6 | None | Base58 keys are matched case-insensitively. An attacker would have to grind a keypair whose address matches a specific 44-character string up to case; and succeeding would only mis-scale their own token. | Accepted, documented |

### Eval — do the resolved scales imply sane prices?

A wrong decimals value does not hide: it shows up as an absurd implied price.
Every live quote was re-priced through the table (yen and euro converted, so a
JPYC quote is judged on value rather than face number).

**363 of 363 quotes across all 41 pairs, 0 implausible, 0 unresolved.** Prices
land between **$0.0001 and $29** — micro-payments, as expected. Three
corroborations fell out of it:

* **Stellar at 7 dp prices to `$0.002` and `$0.02`** — exactly matching the same
  sellers' 6-decimal legs. Independent confirmation of the one value that came
  from the protocol rather than a contract read.
* **JPYC at 18 dp prices to `$0.0067`** — one yen. Correct.
* The pre-existing 18-decimal **BSC** entries all price to `$0.01`, confirming
  the older table too.

The single initial outlier was an artifact of the eval, not the table: one host
(`api.hyperextend.xyz`) advertises `amount` as a **human decimal string**
(`0.00335`), not atomic units, so dividing by 10⁸ produced nonsense. It is the
only non-integer quote in the corpus. Worth knowing — an atomic reader gets it
wrong — but it is a seller-side quirk, not a decimals error.

## Verification

| Check | Result |
|---|---|
| Full root suite | **1,729 tests green** |
| Adversarial scorecard (`redteam.py`) | 25 caught, 2 known gaps, **0 false positives**, 0 misses |
| `integrations/wallets` + `integrations/langchain` | 44 + 26 green |
| Corpus coverage | **41 of 41 (network, asset) pairs resolved — 0 unknown** |
| Live HTTP path | correct JPYC payment → clean; 10⁻¹² underpayment → STOP; contradicting caller decimals → STOP; wrong-chain payment → STOP |
| Table self-consistency | no flat/chain contradictions; no address carrying two different values |

## Not fixed, and why

* **`clients/x402_pay.py` has its own `_CAIP2` with the same gap.** Its
  consequence there is a printed warning, not a refusal, and the client's
  `CHAIN_IDS`/`DEFAULT_RPC` support Base only — extending the alias map alone
  would be cosmetic. It is a test-only dry-run client.
* **Non-USD assets are still excluded from price baselines.** Correct for now:
  scaling JPYC properly does not make a yen quote comparable to a dollar one.
* **Coverage is a snapshot.** The table covers what the corpus advertised on
  2026-08-29. New assets still resolve to unknown — safely — until either the
  on-chain resolver is enabled or the table is refreshed.
