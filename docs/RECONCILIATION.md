# Blackwall — two-surface reconciliation spec

**Purpose:** pin the relationship between the two Blackwall MCP surfaces so an
agent gets the SAME verdict regardless of which door it uses, and so the deep
payment engine actually powers the live, distributed product. Execute this in a
session scoped to **both** repos.

## The two surfaces (today)
- **`blackwalltier.com`** — the front-facing product. A **generalized pre-action
  risk check** (any high-stakes action: email, payment, SQL, delete, post, API).
  Live on Smithery (`bluetier-operations/blackwall`), has the ElizaOS plugin, the
  API-key SaaS, and the human-confirmation (HITL) flow. Response schema:
  `{recommendation, gate, confirmation, hard_blocks}`.
  *(Observed component names from another session — verify: a `forecast-app` TS
  `/api/v1/forecast` route, a `run_forecast.py` action, a `sigil-aa-gateway`.)*
- **`agent-egress-proxy`** — this repo. The deep **x402 payment verdict engine**:
  behavioral reputation, price-anomaly (per-class + peer-group), OFAC screening,
  x402 billing, configurable auto-release threshold. Response schema:
  `{verdict, score, reasons, signals, receipt_id, report_token}`.

## The decision (canonical)
- **`agent-egress-proxy` is CANONICAL for the PAYMENT verdict.** It has the depth;
  do not fork/re-implement payment logic into blackwalltier.
- **`blackwalltier` owns the broad surface**: non-payment actions (email/SQL/etc.),
  the API-key/billing layer, the HITL confirmation flow, and distribution
  (Smithery/Eliza). For NON-payment actions it uses its own logic — the payment
  engine has no opinion there.
- **For a PAYMENT action, blackwalltier DELEGATES to the payment engine** and
  translates the verdict into its own schema. One brain for payments, one front door.

```
agent action ──► blackwalltier (broad guardrail)
                     │
        payment? ────┼── no ──► blackwalltier's own action logic
                     │
                     └── yes ─► agent-egress-proxy payment engine (CANONICAL)
                                 → translate verdict → {recommendation,gate,...}
```

## Interface contract

### Request (blackwalltier payment action → engine `POST /v1/forecast-payment`)
| engine field | from blackwalltier |
|---|---|
| `counterparty` (required) | the payee / recipient wallet |
| `amount` (required, decimal string) | the payment amount |
| `asset`, `chain` (required) | e.g. `USDC`, `base` |
| `payer` (optional) | the agent's wallet (binds settlement confirmation) |
| `resource` (optional) | invoice/PO id (per-class price comparison) |
| `resource_class` (optional) | shared category (peer-group price comparison) |
| `context.expected_recipient` (optional) | the address the 402 named (mismatch STOP) |

### Response (engine verdict → blackwalltier schema)
| engine `verdict` | condition | `recommendation` | `gate` | `hard_blocks` | `confirmation` |
|---|---|---|---|---|---|
| `GO` | — | `GO` | `NONE` | `[]` | none |
| `HOLD` | — | `CAUTION` | `CONFIRM` | `[]` | **open** (poll_url → human) |
| `STOP` | hard stop: sanctioned / known-bad / recipient-mismatch (`score == 0`) | `DENY` | `BLOCK` | `[reason]` | none |
| `STOP` | price gouge (non-hard) | `DENY` | `BLOCK` | `[reason]` | none *(or `CONFIRM` if you want price-gouge to be human-overridable — pick one policy and keep it)* |

Detecting a **hard stop** on the engine side: `score == 0.0` AND a reason contains
`sanctions list` / `known-bad` / `recipient mismatch`. (Consider adding an explicit
`hard_stop: bool` to the engine response to make this unambiguous — small change in
`decide_payment`.)

### Audit passthrough (don't drop these)
- Pass the engine's `receipt_id` + `report_token` back through blackwalltier so the
  agent can later call **report_outcome** — that's what feeds the reputation moat.
  A payment guardrail that never records outcomes never learns.

## Verdict parity requirement (the point of all this)
The same inputs must yield the same decision through **both** paths. Enforce with a
**golden set**: a fixture of `(request → expected decision)` run against (a) the
engine directly and (b) the blackwalltier payment path; they must agree after the
mapping above. Include: a clean GO, a thin/HOLD, a sanctioned STOP, a price-gouge
STOP, a per-class in-line large payment, a peer-group outlier. CI on both repos.

## Integration options (pick one, both-repos session decides)
1. **HTTP call (recommended first step).** blackwalltier's payment branch calls the
   engine's `POST /v1/forecast-payment` over HTTP and maps the response. Fastest to
   parity; keeps one deployed engine. Needs: network path + the mapping adapter +
   auth between the two services.
2. **Embed as a library.** If they can share a runtime, import `blackwall.forecast()`
   directly. Lowest latency, no network — but couples the deployments and needs a
   Python runtime on the blackwalltier side.
3. **Shared service.** Stand up the engine as an internal service both the HTTP
   endpoint and the MCP server call. Cleanest long-term; most work now.

Recommendation: **start with (1)** to reach verdict parity quickly, then consider
(3) if latency/coupling warrants.

## Migration checklist (both-repos session)
- [ ] Confirm the canonical decision above with the owner.
- [ ] Add an explicit `hard_stop` flag to the engine response (removes reason-string
      sniffing).
- [ ] Build the **mapping adapter** in blackwalltier (engine verdict → `{recommendation,
      gate, hard_blocks, confirmation}`) per the table.
- [ ] Route blackwalltier's **payment** branch through the engine (option 1).
- [ ] Thread `receipt_id`/`report_token` through for outcome reporting.
- [ ] Land the **golden parity test** in both repos; wire CI.
- [ ] Retire any forked/duplicate payment logic in blackwalltier once parity is green.
- [ ] Update the Smithery listing's description to reflect the unified engine (do
      NOT create a second listing — see `docs/REGISTRIES.md`).

## Non-goals / do NOT
- Do **not** re-implement payment reputation/price/OFAC logic in blackwalltier —
  call the engine.
- Do **not** publish a second Smithery/Glama listing for `forecast_payment`.
- Do **not** change non-payment action handling — that stays blackwalltier's.
