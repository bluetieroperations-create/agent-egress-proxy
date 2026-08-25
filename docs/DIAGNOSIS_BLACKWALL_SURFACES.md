# Blackwall — full surface diagnosis

*Probed 2026-08-25. Every status below was measured, not inferred from docs.*

## The intended architecture

Per `docs/RECONCILIATION.md`, there are **two products** sharing the name, with a
deliberate split:

```
agent action ──► blackwalltier.com  (broad guardrail: email, SQL, delete, payment)
                     │                API-key SaaS · HITL confirmation · Smithery/Eliza
        payment? ────┼── no ──► blackwalltier's own action logic
                     │
                     └── yes ─► agent-egress-proxy engine  (CANONICAL for payments)
                                 POST /v1/forecast-payment → translate verdict
```

`agent-egress-proxy` (this repo) is canonical for the **payment** verdict.
`blackwalltier` owns the broad surface, billing, HITL, and distribution.

## Measured status of every surface

| # | Surface | Status | Detail |
|---|---|---|---|
| 1 | `blackwall-free.onrender.com` | **HEALTHY** | The engine. `/healthz` 200, `/.well-known/x402` 200, `/openapi.json` 200, `/v1/discovery` 200, `/v1/forecast-payment` 200 with full verdict. Running `main` @ `754e38e`, fresh corpus (239 settlements for bitrefill), rate limit ON (120/60s, burst 30). |
| 2 | `agent-egress-proxy.onrender.com` | **DEAD** | 404 on every path, byte-identical to a never-existed Render name. This service was renamed to `blackwall-free`; the old hostname was released. |
| 3 | `blackwalltier.com` | **HEALTHY** | Next.js app. `/api/health` 200 `{"status":"ok","ts":...}`. `/api/v1/forecast` is live and **key-gated** → 401 `missing_api_key`. |
| 4 | `mcp.blackwalltier.com` | **ALIVE, BROKEN** | JSON-RPC works (`blackwall-remote-mcp` 0.1.0, exposes `forecast_payment`), but every call returns `"Black_Wall oracle error: HTTP 404. No verdict — do not treat as GO."` |
| 5 | `x402.blackwalltier.com` | **ALIVE (static)** | Marketing page. Serves HTML on every path including `/.well-known/x402` and `/stats`; `POST /v1/forecast-payment` → 405. Not an API. |
| 6 | `check.blackwalltier.com/demo` | **ALIVE, BROKEN** | Page loads, but its fetch target is `agent-egress-proxy.onrender.com/v1/forecast-payment` (#2, dead). |

## Root cause of the public breakage

The MCP client builds its URL as `{baseUrl}/api/v1/forecast`
(`lib/forecast.mjs:54`), where

```js
const baseUrl = (opts.baseUrl ?? process.env.BLACKWALL_BASE_URL ?? DEFAULT_BASE_URL)
const DEFAULT_BASE_URL = 'https://blackwalltier.com'   // lib/observe.mjs:13
```

Identified by **error-code elimination**, since the host's env is not visible from here:

| baseUrl | resulting call | observed |
|---|---|---|
| `blackwalltier.com` (the default) | `/api/v1/forecast` | **401** `missing_api_key` |
| `agent-egress-proxy.onrender.com` (dead) | `/api/v1/forecast` | **404** |

The MCP server returns **404**. It therefore has `BLACKWALL_BASE_URL` set to the
dead host — it is **not** falling back to its default. Unsetting that variable, or
pointing it at `https://blackwalltier.com`, is the likely fix (an API key must also
be configured, since the default target is key-gated).

**Repointing the MCP at the engine would NOT work.** The paths differ:

```
MCP requests        {baseUrl}/api/v1/forecast
engine serves       /v1/forecast-payment          (200)
engine on /api/v1/forecast                        (404)
```

The MCP is designed to talk to the blackwalltier product, not the engine directly.
That matches the reconciliation spec: blackwalltier is the front door, and it is
blackwalltier's job to delegate payment actions to the engine.

**Unverified, and the most likely second break:** whether `blackwalltier.com`'s
payment delegation also points at the dead host. It cannot be tested from outside
because `/api/v1/forecast` is key-gated. If it does, fixing the MCP alone will
surface the same 404 one tier down.

## Identity problem: the listings describe the wrong product

| Listing | Describes | Links to |
|---|---|---|
| awesome-x402 `BLACK_WALL` | the **payment oracle** — GO/HOLD/STOP, OFAC, price-anomaly, Ed25519 receipts | `x402.blackwalltier.com` (static), `mcp.blackwalltier.com` (broken), `blackwall-mcp` repo (the *generalized* product) |
| npm `blackwall-mcp` v1.4.1 | "pre-action risk check … before any irreversible action (send email…)" | the generalized product |
| MCP registry `com.blackwalltier/blackwall` v1.0.2 | "Pre-action risk gate … (money, SQL, delete)" | the generalized product |

The public listing sells this repo's capabilities and points at the other product's
doors. Anyone arriving from awesome-x402 gets the broken MCP.

`smithery.yaml` already warns about this: a Smithery listing exists for the
blackwalltier product, and publishing a second listing for the engine would split
the identity further.

## Other findings

- **Billing is OFF on the engine.** `POST /v1/session` → 404, consistent with
  `BLACKWALL_PAY_TO` unset. The endpoint is public and free.
- **The engine's discovery descriptor uses relative paths** (`/v1/forecast-payment`),
  so it carries no stale absolute hostname. Correct.
- **New competitor not in `COMPETITIVE.md`:** Warden (`warden402.xyz`) —
  "pre-execution security for agents on Base … block/review/clear before an agent
  signs", free MCP via `npx warden402-mcp`. Same surface as this engine.
- **`blackwalltier.com` intermittently failed TLS** during probing (2 of ~12
  requests, `SSL_ERROR_SYSCALL`). Could be edge rate-limiting of a repeated
  automated caller rather than instability; not enough samples to say.

## What cannot be determined from here

1. `mcp.blackwalltier.com`'s actual env vars — inferred, not read.
2. Where `mcp.blackwalltier.com` and `blackwalltier.com` are hosted.
3. Whether `blackwalltier.com` delegates payments to the engine, and to which URL.
4. Whether a valid API key exists for the MCP → blackwalltier hop.

All four need dashboard access to those services. None are in this repo.
