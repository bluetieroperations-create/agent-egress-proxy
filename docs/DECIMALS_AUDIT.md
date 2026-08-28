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
