# Audit: hardcoded asset decimals — 2026-08-27

Prompted by the corpus finding that chain **4217 (Tempo)** is live and being paid,
which made "everything is Base USDC" worth testing rather than assuming.

## Severity table

| # | Site | Effect | Severity | Status |
|---|---|---|---|---|
| 1 | `blackwall.forecast` → `check_payment_authorization(..., decimals=6)` | Atomic comparison mis-scales by 10^(d-6). **Bypasses the payload-sim STOP authority.** | **HIGH** | Fixed |
| 2 | `wallet_guard.claim_from_tx(token_decimals=6)` | Amount handed to the verdict wrong by 10^(d-6), **in the signing path**. Cap bypass. | **HIGH** | Fixed |
| 3 | `wallet_guard` placeholder `amount = "0.000001"` | Conflates "no value moved" with "amount unscalable", presenting any transfer as trivial | **HIGH** | Fixed |
| 4 | `discovery_crawl._human(atomic, decimals=6)` | Peer price baselines wrong for non-6dp payees | MEDIUM | Documented, not fixed |
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

## Not fixed, and why

**#4 `discovery_crawl._human`** feeds peer price baselines from crawl data. Its own
docstring already says "USDC (6dp) assumed". Every crawled record carries its
`asset`, so the fix is available; it is not done here because it changes historical
baseline values and should be a separate, measured change.

**#5 `settlement_sim._base_units`** affects a simulated transfer amount only. That
gate is HOLD-only and never STOPs, and an underfunded result is explicitly
non-gating, so a mis-scaled amount cannot block a payment.

## Residual exposure

`KNOWN_DECIMALS` is a static table. A non-6-decimal token that is not in it
resolves to unknown — which is now **safe** (reported and escalated) rather than
wrong, but it is not *verified*. Reading `decimals()` from the token contract via
the existing `rpc_node.py` path would close this properly, at the cost of a
network call on the hot path.
