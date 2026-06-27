# Testnet dry-run — Blackwall x402 billing against a real facilitator

**Date:** 2026-06-27 · **Network:** Base Sepolia · **Facilitator:**
`https://facilitator.x402.rs` (real, public; advertises `base-sepolia` / `exact`
/ x402Version 1 at `GET /supported`).

## What was exercised (real)

`x402.HttpFacilitator` was pointed at the real facilitator and called with a
Base-Sepolia payment requirement (testnet USDC
`0x036CbD53842c5426634e7929541eC2318f3dCF7e`):

- `GET /supported` → 200; confirms `base-sepolia`/`exact` support.
- `POST /verify` → the facilitator accepted our request envelope, reached Base
  Sepolia, attempted **on-chain EIP-3009 signature recovery**, and **rejected**
  our (deliberately unsigned) payment:
  `invalidReasonDetails: "execution reverted: ECRecover: invalid signature length"`.
- The adapter returned `{valid: False, reason: "unexpected_error"}` and
  `settle → {success: False}` — **fail-closed**, with the facilitator's real
  reason surfaced.

This proves, against real infrastructure: the request **shape is correct** (the
facilitator processed it on the right network/asset), the **response parsing is
correct**, and the adapter **fails closed** on rejection.

## Bug found and fixed by this dry-run

The real facilitator returns its structured rejection with **HTTP 500**
(`{isValid:false, invalidReason:...}`), not 200. The adapter was discarding
non-2xx bodies and reporting a useless `"facilitator unreachable"`. Fixed
`HttpFacilitator._post` to parse a structured error body out of an `HTTPError`
and surface the real `invalidReason`; opaque (non-JSON) errors still propagate →
fail closed. Regression: `test_facilitator.test_structured_rejection_at_non_2xx_is_surfaced`.

## What was NOT exercised (and why)

A **completed paid transaction** could not run from this environment:

- **No signing key / wallet.** Constructing a valid `X-PAYMENT` requires signing
  an EIP-3009 `transferWithAuthorization` (secp256k1 + keccak256) with a funded
  Base-Sepolia wallet. This repo is stdlib-only (no keccak/secp256k1) and holds
  no keys — by design, Blackwall is the *resource server*, not the paying client.
- Consequently `verify`/`settle` can only be driven to the **rejection** path
  here, not to a successful settlement.

## To complete a full paid testnet transaction (next, outside this sandbox)

1. Fund a Base-Sepolia wallet for Blackwall (the `payTo`/recipient) — testnet ETH + USDC.
2. Run an x402 **client** (e.g. the `x402` JS/Python SDK with a funded signer) to
   produce a real signed `X-PAYMENT` for `POST /v1/forecast-payment`.
3. Start Blackwall: `--pay-to <sepolia wallet> --price 0.001
   --facilitator https://facilitator.x402.rs` (and set the resource's network to
   `base-sepolia` + testnet USDC asset).
4. Expect: unpaid → 402 → signed retry → facilitator verify+settle succeed →
   verdict served + real settlement tx on Base Sepolia.

The Blackwall side of every step above is built and tested; the gap is purely a
funded signer, which is a wallet/ops task, not a code task.
