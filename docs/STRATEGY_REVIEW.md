# Strategy review — Gemini's "aggressive pillars" vs. ground truth

A Google AI Mode brief proposed 4 strategic pillars + a playbook for Black_Wall.
This captures it, grades it against the actual codebase, ranks the moves, and
sketches **how to build** each. Gemini doesn't know the code — several premises
are wrong; the grades below are the correction.

## What Gemini proposed
1. **Co-signing, not inspection** — turn Black_Wall from an opt-in tool an agent
   can bypass into a **mandatory co-signer** (MPC / ERC-4337); the wallet can't
   broadcast without a Black_Wall authorization signature.
2. **Dual-sided "Shield & Verify"** — buyers pay sub-cent per `forecast_payment()`
   (Shield); sellers/merchants pay to be audited & **whitelisted** so their risk
   score is instantly zero (Verify) → "Verisign/Visa for the machine economy".
3. **Decentralized "Threat Mesh"** — crowdsource threat intel; a failure signature
   detected anywhere is hashed and broadcast to a global registry; all nodes update.
4. **Under-100ms latency** — strip "slow LLM" risk analysis into WASM/edge SLMs;
   security eval < 20ms.
   Playbook: **Immediate** ElizaOS+LangChain plugin · **Short** EIP-712/EIP-3009
   payload simulation · **Long** on-chain risk registry (network effect).

## Grades (ground truth)
- ✅ **Pillar 1 (co-signing)** — the real moat; = the Tier-2 "AA guard module".
  But see the tradeoff below — it is NOT free.
- ✅ **Pillar 2 Verify (seller audit)** — good, net-new — **IF earned by audit,
  not paid-to-whitelist** (see risk).
- ⚠️ **Pillar 3 (threat mesh)** — **fights the data moat.** The roadmap's moat is
  the *private* reputation corpus; broadcasting it to a public decentralized
  registry gives it away. Real kernel (a shared blocklist), buzzword framing,
  premature (no traffic yet). **Defer.**
- ❌ **Pillar 4 premise is FALSE for the payment engine.** `agent-egress-proxy` is
  already **deterministic, stdlib-only, no LLM — sub-millisecond.** "Strip the slow
  LLM" applies to the *broad blackwalltier guardrail* (which may use an LLM), not
  this engine. Gemini is conflating the two products → another reason the
  RECONCILIATION matters.

## The tradeoff Gemini omits (co-signing)
Co-signing **sacrifices "verdict-only, never custody."** That was deliberate:
Black_Wall drops in front of *any* rail with no money-transmitter liability, and if
it's down, payments still flow. As a **mandatory co-signer** it: needs signing
infra + key management, sits in the transaction path (a hard availability
dependency — Black_Wall down ⇒ payments halt), and takes on liability. Powerful,
but a real bet — design fail-open vs fail-closed explicitly.

## Ranked roadmap (lowest-regret first)
1. **EIP-712/EIP-3009 payload simulation** — real capability gap, no positioning
   downside. *Build first.*
2. **Seller-side audit-and-verify tier** — earned, never paid.
3. **LangChain plugin** — ElizaOS already proved the pattern.
4. **AA co-signing** — the moat; deliberate design, eyes open on the tradeoff.
5. **Threat mesh** — defer (moat conflict).

---

# How to build each

## 1. EIP-712 / EIP-3009 payload simulation  *(build first)*  — **Phase 1 SHIPPED**
**Gap:** today the verdict trusts the CLAIMS in the request body
(`counterparty/amount/asset/chain`). A compromised/malicious agent (or a MITM) can
ask us to score "pay $5 to X" while actually signing "pay $5000 to Y". We never see
the real signed authorization.

**Phase 1 is built** (`payload_sim.py`, `test_payload_sim.py`): the request may
carry the agent's actual signed X-PAYMENT in the body field `payment_authorization`
(distinct from the fee header); `check_payment_authorization()` decodes it and
asserts `to/value(atomic)/asset/network` match the claim and a nonce is present.
Any mismatch is a hard STOP folded into `decide_payment` (`payload_mismatch_reasons`
→ `hard_stop`); expired/not-yet-valid is an advisory warning. Wired through
`forecast()` + the MCP schema; USDC (6dp) assumed. Phases 2–3 below remain.

**The seam already exists.** `x402.py` has `decode_payment_header()` (base64→JSON),
`_authorization()` (pulls the EIP-3009 `{from,to,value,validAfter,validBefore,
nonce}` type-safely, never raises), `addresses_equal()`, and `payment_satisfies()`
(which already cross-checks to/value/asset — but against Blackwall's *own fee* req,
not the payment being judged).

**Phase 1 — claim/authorization cross-check (stdlib, no crypto, high value):**
- Accept the agent's actual signed X-PAYMENT (the one it's about to send the
  counterparty) alongside the forecast request (new optional field, e.g.
  `payment_authorization`).
- Decode it and assert: `auth.to == counterparty`, `auth.value == amount`
  (atomic units), asset/domain (`extra.name/version`, chainId) == the claimed
  asset/chain. **Any mismatch → hard STOP** ("signed payment does not match the
  payment you asked me to score"). This is a new hard-stop reason, folds into the
  existing `hard_stop` flag.
- Also check `validBefore` isn't already past and `nonce` is present (reuse the
  existing replay guard `NonceLedger`).

**Phase 2 — signer recovery — SHIPPED (pure-Python, option (a)):**
- `ecrecover` the EIP-712 digest of the `transferWithAuthorization` struct →
  confirm the recovered signer == the stated `from`. Catches a signature that
  doesn't belong to the stated payer. **Built dependency-free** in `keccak.py`
  (Ethereum Keccak-256), `secp256k1.py` (ECDSA public-key recovery), `eip712.py`
  (typed-data hashing) — same posture as `cdp_auth.py`'s pure Ed25519.
- **Bonus — closes the Phase-1 asset/chain gap:** the EIP-712 digest's DOMAIN is
  built from the CLAIM (chainId from `chain`, verifyingContract from `asset`), so a
  valid recovery cryptographically binds the chain + asset — no longer self-declared
  metadata. Gated to assets with a trusted domain (Base/Base-Sepolia USDC);
  unknown-asset degrades to a warning. Anchored to published vectors (Keccak,
  privkey→address, the EIP-712 spec "Mail" domain separator).
- **LIVE-VALIDATED against eth-account.** A real Ethereum signing library was
  cross-checked (test-only, `skipUnless`-gated in `test_eip712.py` /
  `test_blackwall.py`): our EIP-712 digest is byte-identical to eth-account's, and
  our pure-Python `ecrecover` recovered the correct signer on **200/200 real random
  signatures** (both v=27/28); real foreign-signer / wrong-chain / forged signatures
  all hard-STOP through `forecast()`; `keccak256` matches `eth-utils.keccak`. The
  production path stays stdlib-only — eth-account is only a test cross-check.

**Phase 3 — contract-call malice — SHIPPED (`calldata.py`):**
- For payments that are *contract calls* (not plain transferWithAuthorization), the
  request carries a `transaction` {to, data, value}; we decode the 4-byte selector
  (keccak) + static ABI args and flag drainer patterns: UNLIMITED `approve` /
  `increaseAllowance` / EIP-2612 `permit`, `setApprovalForAll(operator,true)`, and
  `transfer`/`transferFrom` whose recipient (or, when the token is the claimed
  asset, amount) doesn't match the claim. CRITICAL → hard STOP; bounded approvals /
  unknown selectors → advisory warning. Selectors anchored to the published 4byte
  db. Folds into the verdict via the same `payload_mismatch_reasons` seam as Ph 1/2.
- Out of scope (documented in the module): target-contract bytecode simulation
  (this is the calldata lane, not full tx simulation) and spender-reputation
  screening.

**Verdict integration:** a payload mismatch is a `hard_stop` (non-negotiable),
distinct from a price/reputation judgment — maps cleanly through
`docs/RECONCILIATION.md`.

## 2. Seller-side audit-and-verify (earned, NOT paid) — **SHIPPED (`seller_audit.py`)**
**Risk to avoid:** "merchant pays → risk score = 0" is the credit-rating-agency
trap (pay us, we call you safe) and destroys the oracle's integrity.
**Built:** `run_audit()` scores a merchant from `readiness.py` signals + on-chain
settlement history + sanctions/known-bad screen + price-fairness vs peers; a bad
actor / unconfigured / disputed / gouging merchant FAILS. A pass earns a signed,
**expiring, revocable** attestation (`SellerRegistry`) granting a **bounded trust
FLOOR** (grade A 0.85 / B 0.72 — above GO_REPUTATION_MIN, never 1.0). Folds into
`decide_payment` via `verified_floor`: it clears the thin-*count* gate (the audit
substitutes for organic volume) but **not** the Sybil/distinct-payer gate, and
**never overrides a STOP** — live sanctions/anomaly/budget/recipient/payload gates
all still fire, plus a verdict-time stale-badge guard drops the floor if live
disputes have since risen past the audit bar. The **fee is the audit + re-audit**,
not a pass. For public verifiability, anchor the attestation via Traceipt `/attest`
(proofs, not the key).

## 3. LangChain plugin
Same thin-adapter pattern as `blackwall-eliza-guardrail`: a callback/tool that runs
`forecast_payment` (or the broad check) before a tool/payment executes; observe →
enforce modes; reuse the HITL confirmation. Lives in its own repo. Low effort.

## 4. AA co-signing (the moat) — **OFF-CHAIN HALF SHIPPED (`aa_cosigner.py`)**
**Built (off-chain, what Blackwall runs):** `user_op_hash()` computes the exact
ERC-4337 v0.7 `userOpHash` (cross-checked vs `eth_abi`); `decode_execute()` pulls
the inner `(to,value,data)` from a standard account `execute()` so we Phase-3 screen
the REAL on-chain call (a drainer inner-call is refused even on a GO verdict);
`cosign_user_op()` ECDSA-signs the userOpHash ONLY on GO / human-approved HOLD and
WITHHOLDS on STOP — with an explicit `FAIL_OPEN`/`FAIL_CLOSED` choice when the
engine is unreachable; `recover_cosigner()` is what the on-chain validator does
(cross-checked vs `eth-account`). **Not built (the real bet):** the deployed
ERC-7579 Solidity validator, HSM/MPC key infra, bundler path — prototype on testnet.
See `docs/AA_COSIGNING.md` for the reference validator + the hash-domain caveat.
Everything below still holds — this is a posture change, not a free upgrade.

### Original design notes
**Model:** Black_Wall as an **ERC-4337 guard/validator module** (or MPC co-signer):
the smart account's `validateUserOp` calls Black_Wall; it returns a signature only
when the verdict is GO (or a human approves a HOLD). The wallet **cannot** broadcast
without it.
**Decisions to make up front:**
- **Fail-open vs fail-closed** when Black_Wall is unreachable. Fail-closed = payments
  halt if we're down (safest, but a hard availability SLA + liability). Fail-open =
  degrades to advisory (defeats the "mandatory" point). Pick per customer.
- **Custody/liability:** you're now in the signing path — money-transmitter and
  availability posture change vs. today's verdict-only stance.
- **Key management / MPC** infra is new surface area.
This is the biggest bet; prototype as an ERC-7579 module on a testnet smart account
before committing.

## 5. Threat mesh — defer
Do NOT broadcast the reputation corpus to a public registry (moat suicide). The
*safe* kernel is a **private shared blocklist** (known-bad addresses/signatures)
that Black_Wall nodes you operate can pull — not a decentralized public mesh. Revisit
only post-traffic, and only for PROOFS/blocklists, never the dataset (same rule as
EAS attestations in ROADMAP.md).

---

## Traceipt CHANGES THE PICTURE — verdict + receipt = the full lifecycle
**Traceipt is real, live, and in this repo** (`traceipt/`, branch
`claude/x402-product-ideas-6adgah`; see `docs/ECOSYSTEM.md`). It issues Ed25519-
signed, on-chain-verified receipts for x402 payments — the **post-payment RECEIPT**
half to Black_Wall's **pre-payment VERDICT** half:

```
Black_Wall (decide: GO/HOLD/STOP)  ──►  agent pays x402  ──►  Traceipt (record: signed receipt)
        ▲                                                              │
        └──────────── reputation flywheel (receipts feed verdicts) ◄───┘
```

**This resolves the long-term pillars without new infra:**
- **Pillars #4/#5 (on-chain registry / network effect) = Traceipt.** Don't build a
  new registry. Anchor Black_Wall verdict digests via Traceipt's `POST /attest`
  (Merkle, RFC 6962) → trustless "Black_Wall attested this verdict at time T",
  PROOFS not corpus. Bonus: `/attest` is Traceipt's paid endpoint → revenue.
- **Pillar #2 (seller audit-verify)** — a "verified merchant" badge = a Traceipt-
  anchored audit attestation. The attestation layer already exists.

**The compounding moat (the real prize):**
- **Traceipt receipts → Black_Wall reputation.** A receipt is an on-chain-verified,
  payer/payee-bound settlement — precisely the chain-confirmed outcome
  `ledger.py`/reputation consumes, but *fraud-resistant* (bound to what was bought).
  Feeding Traceipt receipts into Black_Wall's reputation is a higher-quality moat
  input than raw chain scraping.

**Two concrete integration builds (both in-scope, span two branches):**
- **A) verdict → `/attest`** — after a verdict, POST its digest to Traceipt to
  anchor it. Small; delivers the "registry" pillar immediately.
- **B) receipts → reputation** — ingest Traceipt receipts as confirmed settlement
  outcomes into reputation. Higher value (compounds the moat). **SHIPPED:**
  `traceipt_ingest.py` maps receipts, `traceipt_verify.py` authenticates the
  Ed25519 envelope against the issuer JWKS (fail-closed), and `traceipt_pull.py`
  pulls signed receipts by id from live Traceipt (`GET /receipts/{id}` +
  `/jwks.json`), verifies, and folds the authenticated payments into a
  `ReputationStore`. Remaining: a live run against real Traceipt receipt volume.

Design against the real Traceipt code (`traceipt/service.py` `/attest` shape,
`traceipt/schema.py` receipt schema) before wiring.
