# Tokenized RWAs — pre-trade gate for agents buying securities with stablecoins

Blackwall's core verdict answers *"should this agent sign this **stablecoin
payment**?"* — it scores the counterparty being paid (an address) and the USDC leg.
When an agent pays USDC to **acquire a tokenized real-world asset** (a tokenized
stock / T-bill / fund share), a second, **asset-side** question appears that a
stablecoin-only x402 flow never raises:

> Will the security actually transfer to the agent's wallet, or **revert**?

This module (`rwa_readiness.py`) answers it **pre-trade**. It is the differentiated
wedge for positioning Blackwall as *the pre-trade gate for agents buying tokenized
RWAs with stablecoins*.

## Why this is the right shape

The payment leg is still USDC, so **~90% of the existing stack already applies to an
RWA purchase with zero changes**:

| Existing layer | Applies to an RWA buy? |
|---|---|
| Reputation / Sybil / payer-graph | ✅ scores the issuer/DEX/router being paid |
| Sanctions (OFAC) — `sanctions.py` | ✅ table stakes for API payments, **load-bearing** for securities |
| Price-integrity / category pricing | ✅ over-price / bait-and-switch on the USDC amount |
| Payload-sim + calldata drainer screen | ✅ `approve`/`transfer` screening on the payment |
| Verdict, not custody | ✅ a *pre-trade compliance/suitability check that never executes the trade* is a **cleaner** regulatory posture for securities than for API calls |

What's genuinely new — and what nothing in stablecoin-land needs — is the
**transfer-restriction readiness** check.

## The wedge: transfer-restriction readiness

Many tokenized securities are **permissioned tokens** — ERC-3643 (T-REX), ERC-1400,
or a simpler allowlist ERC-20. Transfers are gated by an on-chain identity registry /
compliance module, and a transfer to a wallet that is **not KYC-verified /
whitelisted** (or that is **frozen**, or while the token is **paused**) **reverts**.
An agent that signs the USDC leg blind can:

- **pay and receive nothing**, when mint/settlement is *non-atomic*, or
- **burn gas on a guaranteed revert**, when the buy is an *atomic swap*.

Blackwall pre-checks this. The request carries an optional `acquires` descriptor, and
Blackwall reads the token's **receiver-side** restriction interface for the receiving
wallet (`payer`):

| Standard | Read (receiver only) |
|---|---|
| ERC-3643 / T-REX | `identityRegistry().isVerified(payer)`, `isFrozen(payer)`, `paused()` |
| Allowlist ERC-20 | `isWhitelisted(payer)`, `isFrozen(payer)`, `paused()` |
| ERC-1400 | `canTransfer(...)` result consumed by the pure assessor if supplied (needs from/value — out of the receiver-only live spike) |

### Request

```jsonc
POST /v1/forecast-payment
{
  "counterparty": "0xIssuerOrDex...",   // paid in USDC (scored as usual)
  "amount": "100.00", "asset": "USDC", "chain": "base",
  "payer": "0xAgentWallet...",          // REQUIRED for the wedge: who RECEIVES the security
  "acquires": {                          // NEW, optional
    "token": "0xTokenizedStock...",      // the security's contract
    "standard": "erc3643",               // optional hint
    "chain": "base"
  }
}
```

### Verdict fold

`assess_transfer_readiness(probe)` → grade, folded by `apply_rwa_readiness`:

- **`blocked`** — a definitive failure signal (not verified / frozen / paused /
  `canTransfer` rejected). Escalates **GO → HOLD** with a plain-language reason.
- **`ready`** — receiver verified/whitelisted; leaves the verdict unchanged.
- **`unknown`** — not a recognized permissioned token, or could not determine (the
  **common** case — a plain ERC-20 wrapper is unrestricted). No-op.

Surfaced under `signals.rwa_transfer_readiness`.

## Design rules (inherited from `readiness.py` + `blockscout.py`)

1. **FAIL OPEN.** Unreachable RPC, plain ERC-20, or junk data → `unknown`, verdict
   proceeds on Blackwall's own signals. Absence of a restriction interface reads as
   *"no gate"*, **never** as *"blocked"* (the `decode_bool("0x") → None` rule is the
   structural guarantee: an absent/reverted method is unknown, not `false`).
2. **HARD BOUNDARY — settlement-safety, not compliance.** `blocked` can only push a
   would-be GO to **HOLD (REVIEW)**. It **never** produces or clears a STOP and never
   touches `hard_stop`. A token contract's own freeze/whitelist is **not** a sanctions
   source — OFAC (`sanctions.py`) stays the STOP authority.
3. **Conservative-only / monotonic toward caution.** Escalates only on `blocked`,
   only from GO; never upgrades a HOLD/STOP to GO.

### Why HOLD, not STOP

A confirmed will-revert transfer *sounds* like a hard block, but we deliberately
HOLD:

- We **can't tell an atomic swap** (a revert only wastes gas) **from a non-atomic
  settle** (you pay and get nothing) from the request alone.
- A **mis-read interface** (a bogus `identityRegistry()` return, an RPC hiccup) must
  not hard-block a legit buy.

HOLD hands the decision to the human / spending-cap layer to confirm eligibility —
the right altitude for an agent flow. A strict operator can escalate on the exposed
grade themselves.

## Enabling it

Off by default (network on the hot path). Opt in:

```sh
BLACKWALL_RWA_READINESS=1 BLACKWALL_RWA_RPC_URL=https://mainnet.base.org \
  python blackwall.py --store rep.db
```

Only fires when a request carries `acquires`; adds a few `eth_call` view reads per
such verdict. With no RPC url it degrades to `unknown` (fail-open).

## Audit findings (fixed)

An adversarial pass on the first cut found two real issues, both fixed with regression
tests:

- **Non-canonical bool → false block (medium).** `decode_bool` originally treated any
  nonzero word as `True`, so a token whose `paused()`/`isFrozen()` returned a non-bool
  word (e.g. a uint timestamp) could manufacture a false `blocked` (GO→HOLD). Now
  **strict**: only the canonical ABI words `0`/`1` decode to a bool; anything else →
  `None` (unknown, fail-open). It can never fabricate a rejection.
- **RPC amplification (low-med).** The probe issued 4 sequential `eth_call`s *even for a
  plain ERC-20*. It now **short-circuits**: it detects a permissioned standard by its
  receiver gate first (≤2 calls) and only probes `isFrozen`/`paused` once the token is
  confirmed permissioned. A plain token is a **2-call no-op**; "unknown = not a
  recognized permissioned token" is now exact.

## Limitations (audited & accepted)

- **Live-path amplification / latency.** A confirmed permissioned-token verdict issues
  up to **4 sequential `eth_call`s** (plain tokens: 2), each bounded by `timeout`. On a
  public deploy this is upstream-RPC amplification per request carrying `acquires` —
  mitigated by the opt-in default and the server rate-limiter (`BLACKWALL_RATE_LIMIT`),
  same posture as the Blockscout enrichment. Put a rate-limiter/gateway in front.
- **Malformed `acquires.token` is a silent no-op.** `validate_request` requires only a
  non-empty string; a symbol (`"AAPLx"`) or a typo'd address is not a contract address,
  so the source returns `unknown` (no signal) rather than erroring — fail-open by design.
- **Receiver-only probe.** We read gates that need just the receiving wallet
  (`isVerified`/`isWhitelisted`/`isFrozen`/`paused`). Full ERC-1400 `canTransfer`
  (needs from/value/data) is out of the live spike; the *pure* assessor consumes a
  `can_receive`/`reason_code` if a caller supplies one.
- **We screen restrictions, not backing.** This does **not** verify NAV/peg, that the
  issuer is a registered transfer agent, or 1:1 redeemability — that's a separate
  reputation/attestation extension (a natural fit for `seller_audit.py`).
- **Live-path is a spike.** The pure logic (`assess_transfer_readiness`,
  `apply_rwa_readiness`, ABI encode/decode) is fully unit-tested; the network source
  is exercised via an injected `eth_call` transport (no Base RPC in this env).
- **USDC-medium only.** This gates *buying* an RWA **with stablecoins**. Paying *with*
  a tokenized stock as the medium still needs per-asset decimals + EIP-712 domain
  wiring (see the `LIMITATIONS` notes in `payload_sim.py` / `calldata.py`).

## Tests

`test_rwa_readiness.py` (39 tests, mutation-noted): assessor boundaries + blocking
dominates positive + fail-open; conservative-only fold (GO→HOLD, never upgrade,
never touch STOP); ABI encode/decode (incl. the `"0x" → None` rule); the live source
across ERC-3643 / allowlist / frozen / paused / plain-ERC-20 / no-payer /
transport-raises; end-to-end through `forecast`; `validate_request` acceptance.
