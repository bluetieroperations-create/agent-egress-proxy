# Blackwall ecosystem inventory

**Confidence legend:**
`[VERIFIED]` = read directly this session · `[OBSERVED]` = pieced together from
screenshots the user shared (unverified — I can't see these repos) · `[UNKNOWN]` =
name seen, purpose unclear.

> **Scope caveat:** this session can only read `agent-egress-proxy`. Everything
> else is reconstructed from screenshots and must be confirmed by a session scoped
> to those repos. Treat non-`[VERIFIED]` rows as leads, not facts.

---

## The surfaces

### 1. `agent-egress-proxy`  `[VERIFIED]`
The deep **x402 payment verdict engine** (and, originally, an egress proxy — the
main README still documents `egress_proxy.py`).
- **What:** GO / HOLD / STOP pre-signature payment verdict from behavioral
  reputation + price-anomaly (per-class + peer-group) + OFAC screening + x402
  billing + configurable auto-release threshold + AP payout gate.
- **Deploy:** Render (`agent-egress-proxy.onrender.com`), Base mainnet, OFAC on,
  373 tests. MCP: `forecast_payment` (stdio).
- **Response schema:** `{verdict, hard_stop, score, reasons, signals, receipt_id, report_token}`.
- **Role (per RECONCILIATION.md):** the **canonical PAYMENT engine**.
- Key files: `blackwall.py`, `x402.py`, `ledger.py`, `reputation_store.py`,
  `reputation_onchain.py`, `settlement_watch.py`, `sanctions.py`, `readiness.py`,
  `ap_gate.py`, `discovery.py`, `mcp_server.py`, `clients/x402_pay.py`.

### 2. `blackwalltier.com` (+ backend)  `[OBSERVED]`
The **front-facing hosted product** — a **generalized pre-action risk check** (any
high-stakes action: email, payment, SQL, delete, post public content, external
API), not payment-only.
- **On Smithery:** `bluetier-operations/blackwall`, published 2026-05-23, 99.86%
  uptime, 69/100. One tool: "pre-action risk check."
- **API-key SaaS:** `bw_live_xxx` keys (free key at `blackwalltier.com/dashboard/keys`).
- **Response schema:** `{recommendation, gate, confirmation, hard_blocks}` + a
  human-in-the-loop confirmation flow (`poll_url`).
- **Role:** broad action guardrail / distribution front door.
- **Backend components (all `[OBSERVED]`/`[UNKNOWN]`):**
  - `forecast-app` — a TS app with an `/api/v1/forecast` route.
  - `run_forecast.py` — a Python forecast action.
  - `sigil-aa-gateway` — an **account-abstraction (AA) gateway**? Has a
    "deterministic corridor gate" + an LLM precision check; a reported flaw where
    `decode()` strips the "corridor signal" so the two decide on non-comparable
    inputs. **Purpose unconfirmed — investigate first.**

### 3. `bluetieroperations-create/blackwall-eliza-guardrail`  `[OBSERVED]`
**ElizaOS plugin** (npm `blackwall-eliza-guardrail`). Wraps every ElizaOS action
handler with a `forecast()` check; STOP-rated actions abort before running.
- Uses `BLACKWALL_API_KEY` → calls **blackwalltier.com** (not agent-egress-proxy).
- Modes: `observe` (default, log-only) / `enforce`. v0.3.0 human-confirmation flow
  (poll_url, hard_blocks, strictest-wins, fail-closed).
- Public repo; dirs `.githooks`, `.github/workflows`, `src`, `test-eliza-real`;
  `.gitleaks.toml`.
- **Role:** ElizaOS distribution adapter.

### 4. `bluetieroperations-create/blackwall-openclaw-plugin`  `[OBSERVED]`/`[UNKNOWN]`
A **Blackwall plugin for "OpenClaw"** (an agent framework?). Has a "verify nemoclaw
dockerfile" scheduled workflow (auto-disabling after ~60 days of repo inactivity —
so it's been quiet).
- **Purpose, "OpenClaw", and "nemoclaw" all unconfirmed — investigate.**
- **Role:** presumably another distribution adapter (like the Eliza plugin), for a
  different framework.

### 5. `Traceipt`  `[VERIFIED — in this repo]`
**The post-payment RECEIPT half of the trust lifecycle** (Black_Wall is the
pre-payment VERDICT half). Live: `traceipt.xyz` / `api.traceipt.xyz`. Lives in
**this same repo** (`agent-egress-proxy`) under `traceipt/` on branch
`claude/x402-product-ideas-6adgah` — Python, stdlib + `cryptography`, ~110 tests.
- **What:** issues **Ed25519-signed, offline-verifiable receipts** for x402
  payments — settlement verified **on-chain before signing**, binding the payment
  to payer/payee/what-was-bought. Verifiable against published JWKS, no callback.
  The audit/accounting layer (invoices, auditor trail, EU AI Act Art. 12 / MiCA).
- **Modules:** `signing.py` (Ed25519), `settlement.py` (on-chain verify),
  `ledger.py` (tamper-evident per-seller hash chain), `merkle.py` (RFC 6962
  anchoring), `invoice.py` (VAT A4 PDF + QR), `x402_gate.py` (payment gate),
  `service.py` (API incl. `POST /attest` anchoring-as-a-service — the
  recurring-revenue endpoint), `publisher.py` (on-chain).
- **Role — resolves Gemini pillars #4/#5:** it IS the honest "on-chain risk
  registry" (attest PROOFS via Merkle anchoring, never the private corpus).
- **Two-way integration with Black_Wall (the flywheel):**
  1. **Traceipt receipts → Black_Wall reputation** — a receipt is an on-chain-
     verified, payer-bound settlement = exactly the chain-confirmed outcome
     `ledger.py`/reputation wants (fraud-resistant reputation source).
  2. **Black_Wall verdicts → Traceipt `/attest`** — anchor a signed verdict digest
     into Traceipt's Merkle batches → trustless, time-stamped proof of the verdict.

### 6. awesome-x402 listing  `[VERIFIED via screenshots]`- **PR #679 MERGED** into `xpaysh/awesome-x402` `main` (by maintainer Sri Akula).
  Blackwall is live in the ecosystem index. (Earlier PR #667 via the fork
  `bluetieroperations-create/awesome-x402`.)

---

## Data flow (as best reconstructed)
```
ElizaOS agents ─► blackwall-eliza-guardrail ─┐
"OpenClaw" agents ─► blackwall-openclaw-plugin ─┤─► blackwalltier.com (broad guardrail, API keys, HITL)
MCP clients ─► Smithery (bluetier-operations/blackwall) ─┘        │  {recommendation,gate,confirmation,hard_blocks}
                                                                  │
                                          (payment branch SHOULD delegate to) ▼
                                          agent-egress-proxy  (canonical payment engine)
                                          {verdict,hard_stop,score,reasons,signals,receipt_id}
```
> The dashed "should delegate" link is the **reconciliation that isn't done yet**
> (docs/RECONCILIATION.md). Today blackwalltier's payment logic and
> agent-egress-proxy's engine are **separate**, with different schemas.

## The core problem this inventory exposes
**Schema + engine divergence across surfaces.** blackwalltier
(`recommendation/gate/...`) and agent-egress-proxy (`verdict/hard_stop/...`) are
different brains with different depth. The plugins call blackwalltier; the deep
payment work lives in agent-egress-proxy. Until reconciled, users of the
distributed product don't get the deep payment engine.

## Open questions (for a properly-scoped session)
1. What is `sigil-aa-gateway` actually — the AA guard, the "corridor gate", or the
   payment router? What's the `decode()`/corridor bug?
2. What is `blackwall-openclaw-plugin` / "OpenClaw" / "nemoclaw"?
3. Does `forecast-app`/`run_forecast.py` reimplement payment logic, or call an
   engine? Which one is canonical there?
4. Verdict parity: do all surfaces return the same decision for the same payment?
5. Which repos are active vs stale (openclaw looks ~60 days idle)?

## Recommended next step
Open **one session scoped to all `bluetieroperations-create/*` Blackwall repos**
(+ read access to the blackwalltier backend) and:
- Confirm each row above (turn `[OBSERVED]` → `[VERIFIED]`).
- Fill the `[UNKNOWN]`s (sigil, openclaw, nemoclaw).
- Then execute `docs/RECONCILIATION.md` to make agent-egress-proxy the payment
  engine behind blackwalltier.

*This inventory is a map, not ground truth — verify before acting on any
non-`[VERIFIED]` row.*
