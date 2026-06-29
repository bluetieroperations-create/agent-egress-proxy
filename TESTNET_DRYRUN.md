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

The funded-signer client is now built — `clients/x402_pay.py` (see
`clients/README.md`). It signs a real EIP-3009 authorization with `eth-account`
(test-only dep) and reads the token's EIP-712 domain on-chain via `--rpc`.

1. Fund a **throwaway** Base-Sepolia wallet — testnet USDC (the asset transferred)
   + a little testnet ETH. Export its key: `export SIGNER_PRIVATE_KEY=0x…`.
2. Start Blackwall advertising the testnet network/asset (the `--network` flag
   added with this client; asset defaults to Base-Sepolia USDC):
   ```sh
   python blackwall.py --pay-to <your-sepolia-payTo> \
       --network base-sepolia --facilitator https://facilitator.x402.rs
   ```
3. Run the client:
   ```sh
   pip install -r clients/requirements.txt
   python clients/x402_pay.py --url http://localhost:8402/v1/forecast-payment \
       --counterparty 0x… --amount 5.00 \
       --network base-sepolia --rpc https://sepolia.base.org
   ```
4. Expect: unpaid → 402 → signed retry → facilitator verify+settle succeed →
   verdict served + real settlement tx on Base Sepolia.

The full loop (402 → sign → X-PAYMENT → verify+settle → verdict) is verified
locally against the built-in mock facilitator (see `clients/README.md`); the only
piece that can't run from this sandbox is the real on-chain settlement, which
needs the funded wallet above.
