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

## ✅ COMPLETED: full paid transaction settled on-chain (2026-06-30)

The end-to-end paid path was run from an operator machine with a funded
Base-Sepolia wallet and **settled on-chain**:

- **Result:** `POST /v1/forecast-payment` → `402` → signed EIP-3009 → `X-PAYMENT`
  → facilitator **verify + settle** → `HTTP 200` + verdict `HOLD` (correct: the
  counterparty was unknown/thin) + signed `receipt_id`.
- **On-chain proof:** an ERC-20 `transferWithAuthorization` of **0.001 USDC**
  (the verdict price), payer → `payTo`, in block **43504014** on Base Sepolia.
  Gasless — the facilitator sponsored gas (payer held 0 ETH).
- **Facilitator:** `facilitator.x402.rs` was DOWN at run time; switched to
  **`https://facilitator.xpay.sh`** (live, supports x402Version 1 / base-sepolia,
  gas-sponsored).

### Bugs the live run surfaced (that mocked tests missed) — all fixed

| Symptom | Root cause | Fix |
|---|---|---|
| RPC `--rpc` → 403 | default Python urllib UA | browser UA on client RPC |
| facilitator → timeout | default urllib UA (Cloudflare tarpit) | browser UA on `HttpFacilitator` |
| facilitator → `missing_eip712_domain` | 402 omitted the asset EIP-712 domain | emit `extra:{name,version}` in `build_requirements` |
| (pre-empted) Blockscout ingest | non-browser `DEFAULT_UA` | browser-prefixed UAs across all outbound calls |

The lesson: **public endpoints behind Cloudflare reject the default Python UA.**
All outbound HTTP now sends a browser-prefixed, self-identifying User-Agent.

### Reproduce (operator machine, funded throwaway wallet)
```sh
pip install -r clients/requirements.txt
# server:
python blackwall.py --pay-to <your-sepolia-payTo> \
    --network base-sepolia --facilitator https://facilitator.xpay.sh
# client (separate shell), SIGNER_PRIVATE_KEY = the funded throwaway key:
python clients/x402_pay.py --url http://localhost:8402/v1/forecast-payment \
    --counterparty 0x… --amount 5.00 \
    --network base-sepolia --rpc https://base-sepolia-rpc.publicnode.com
```
Verify the settlement on `sepolia.basescan.org` → the `payTo` address →
Token Transfers (ERC-20).

The full loop (402 → sign → X-PAYMENT → verify+settle → verdict) is verified
locally against the built-in mock facilitator (see `clients/README.md`); the only
piece that can't run from this sandbox is the real on-chain settlement, which
needs the funded wallet above.
