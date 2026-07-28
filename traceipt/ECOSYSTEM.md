# Traceipt in the Black_Wall ecosystem

Cross-session synthesis. Traceipt (this branch, `claude/x402-product-ideas-6adgah`)
and Black_Wall (branch `claude/blackwall-x402-integration-j3rdab`) are two products
built in the same repo, in separate sessions, that integrate with each other. This
doc records how they relate so the two sessions stay coordinated and don't drift.
It mirrors Black_Wall's own `docs/ECOSYSTEM.md`.

**Confidence legend:**
`[VERIFIED]` = read the actual code this session and checked the contract ·
`[FROM BLACKWALL DOCS]` = stated in the Black_Wall branch, not re-verified here ·
`[OBSERVED]` = pieced together from screenshots / unread repos.

---

## The one-line relationship

**Black_Wall decides before the payment; Traceipt records after it.** They are the
pre-payment VERDICT half and the post-payment RECEIPT half of the same trust
lifecycle for x402 agent payments.

```
Black_Wall (GO / HOLD / STOP)  ─►  agent pays x402  ─►  Traceipt (Ed25519 receipt)
       ▲                                                          │
       └──────────── reputation flywheel: receipts feed verdicts ─┘
```

- **Traceipt** (`traceipt/`): issues Ed25519-signed, on-chain-verified, offline-
  verifiable receipts for x402 settlements; Merkle-anchors them (RFC 6962); exposes
  anchoring-as-a-service at `POST /attest`. Live at `traceipt.xyz` /
  `api.traceipt.xyz` (testnet/dev-gate/mock demo today).
- **Black_Wall** (repo root of the other branch): pre-signature GO/HOLD/STOP payment
  verdict from behavioral reputation + price-anomaly + OFAC screening. Never touches
  funds. `[FROM BLACKWALL DOCS]` Render, Base mainnet, 373 tests, MCP server, listed
  in `awesome-x402` + Smithery.

The two branches share **only** the original `agent-egress-proxy` root commit and
have **no file overlap** — Traceipt lives under `traceipt/`, Black_Wall at the repo
root. They talk over HTTP, not by shared code.

---

## The two integrations (Black_Wall consumes Traceipt over HTTP)

Both were written on the Black_Wall branch *against Traceipt's real API*. Every
contract point below was re-checked against this branch's code this session and
matches — the flywheel would work live.

### A) Black_Wall verdicts → Traceipt `POST /attest`  `[VERIFIED]`
Black_Wall anchors a proof of each verdict into Traceipt's Merkle batches, so
"Black_Wall issued verdict X at time T" is provable to anyone — a PROOF, never the
private reputation corpus.

- Black_Wall module: `traceipt_attest.py` (stdlib) + `clients/traceipt_anchor.py`
  + `clients/x402_pay.py` (the funded signer; only place that needs `eth-account`).
- Sends `{hash: "sha256:<64hex>", type: "blackwall-verdict", ref}`.
  ✓ matches Traceipt `schema.validate_attestation_request` (only hash/type/ref;
  hash regex `^sha256:[0-9a-f]{64}$`; type ≤40; ref ≤300).
- Reads back `{attestation: {attestation_id, status}, proof_url}`.
  ✓ matches Traceipt `service.App.issue_attestation`.
- **Pays the 402 via x402 auto-pay with a spend cap** (`max_amount_atomic`, default
  $1.00) so a spoofed/compromised `/attest` challenge can't drain the signer.
  **Fail-open**: any anchoring error returns a benign dict, never blocks a verdict.
- **Consequence:** Black_Wall is a built-in *paying* user of Traceipt's
  recurring-revenue endpoint.

### B) Traceipt receipts → Black_Wall reputation  `[VERIFIED]`
A Traceipt receipt is an on-chain-verified, payer/payee-bound settlement — exactly
the confirmed outcome Black_Wall's reputation moat wants, but fraud-resistant.

- Black_Wall modules: `traceipt_pull.py` (fetch by id), `traceipt_verify.py`
  (pure-Python Ed25519 verify), `traceipt_ingest.py` (map → reputation transfers).
- `GET /receipts/{id}` returns the raw signed envelope. ✓ (Traceipt
  `service` line ~497 sends `ledger.get(id)` directly.)
- Signature verified against `GET /jwks.json` before anything touches reputation.
  **Fail-closed**: a receipt whose Ed25519 signature doesn't verify is dropped.
  The signing input canonicalization (`{payload, protected}`, `ensure_ascii=False`,
  `alg=EdDSA`, `typ=x402-receipt+json`) is byte-identical to Traceipt `signing.py`.
- Maps `settlement.{verified, amount_base_units, payee, payer, tx_hash}` + top-level
  `kind` + `issued_at`. ✓ all present in Traceipt's schema. Correctly skips
  `kind:"credit"` and anything not `verified:true`.

---

## What this changes about Traceipt's go-to-market

- Traceipt's **external** demand is still NOT validated (the earlier "three GitHub
  issues asked for this" claim was wrong — it traced to one AAR author). Don't
  reintroduce that claim.
- But Black_Wall is Traceipt's **first real integration and first paying user of
  `/attest`** — designed, built, and contract-verified, not hypothetical.
- Per Black_Wall's `docs/STRATEGY_REVIEW.md` `[FROM BLACKWALL DOCS]`, the Black_Wall
  roadmap **retired building a separate on-chain risk registry** — Traceipt's
  `/attest` *is* that registry. So Traceipt is not a competing side-project; it's
  infrastructure the Black_Wall roadmap depends on.
- Black_Wall is also a **distribution channel** (ElizaOS plugin, OpenClaw plugin,
  Smithery, awesome-x402 index) `[OBSERVED / FROM BLACKWALL DOCS]` — every surface
  where "…and it anchors a tamper-evident receipt via Traceipt" attaches naturally.

---

## The shared unblock (same on both sides)

Both products are **testnet / dev-gate / mock-settlement** today. The integration is
**proven in code, not in production revenue.** For Black_Wall to pay Traceipt real
USDC on Base — turning the verified-in-code flywheel into a real end-to-end demo —
Traceipt needs the production flip we already scoped:

1. a receiving wallet (`RECEIPTS_PAY_TO`, address only),
2. real settlement (`RECEIPTS_CHAIN=base`, `RECEIPTS_SETTLEMENT=rpc`, a reliable RPC),
3. the facilitator gate (`X402_GATE=facilitator`, `RECEIPTS_BIND_PAYER=1`),
4. durable persistence (paid disk + `RECEIPTS_KEY_PEM` secret).

See `DEPLOY.md` §2. Until then, receipts stay honestly marked
`verification_method:"mock"` and `/attest` runs on the dev gate.

---

## Open questions (owned by the Black_Wall session)

Unconfirmed pieces of the wider Black_Wall ecosystem, listed here only so this
session knows they're *not* Traceipt's to resolve: the `blackwalltier.com` hosted
backend, `sigil-aa-gateway`, the OpenClaw plugin, and schema reconciliation across
Black_Wall's surfaces (their `docs/RECONCILIATION.md`). Traceipt's contract with
Black_Wall is `/attest`, `/receipts/{id}`, and `/jwks.json` — stable regardless of
how those resolve.

*This is a map for coordination, not ground truth for anything marked
`[OBSERVED]` / `[FROM BLACKWALL DOCS]`. Re-verify before acting on those rows.*
