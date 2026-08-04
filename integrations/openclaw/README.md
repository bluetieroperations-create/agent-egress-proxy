# Blackwall — OpenClaw x402 payment gate

"Call Blackwall before you sign", enforced at the **runtime** layer. This is an
OpenClaw/NemoClaw plugin that hooks `before_tool_call`, recognizes
payment-shaped tool calls, extracts the x402 claim, asks a Blackwall deployment
`POST /v1/forecast-payment` for a **GO / HOLD / STOP** verdict, and blocks
anything that isn't a GO — even if the agent (compromised, mistaken, or
prompt-injected) never asked for a check.

It is the mandatory backstop behind the advisory skill layer: a skill an agent
can skip is guidance; a hook it cannot skip is a gate. Same verdict contract as
the LangChain (`integrations/langchain/`) and wallet (`integrations/wallets/`)
guards: **GO → allow, HOLD → confirm, STOP → block**, with `confirm` degrading
to block-with-detail because this runtime has no interactive approval surface.

## What counts as a payment

`extractClaim()` (pure, unit-tested) recognizes three shapes in a tool call's
params, most explicit first:

1. **Flat fields** — `payTo`/`recipient`/`payee` + `amount`/`price` (+ optional
   `asset`, `chain`/`network`, `resource`).
2. **An x402 402-challenge** — `accepts: [{ payTo, maxAmountRequired, network,
   asset, resource }]` (atomic `maxAmountRequired`, converted exactly).
3. **A signed X-PAYMENT header** — base64 EIP-3009 authorization; decoded for
   the claim and **passed through as `payment_authorization`** so the server's
   payload-sim can cross-check the signature against the claim (catches
   "verdict said pay A, signature actually pays B").

A tool whose *name* looks payment-shaped (word-boundary tokens: `x402`, `pay`,
`payment`, `payto`, `usdc`, plus your `paymentTools` additions) but whose params
yield no claim is **unscorable**: blocked under enforce, warned under observe —
a payment that can't be scored must not be signed on a guess. Everything else
passes through untouched, with zero forecast calls.

## Defaults (deliberate)

| Config | Default | Why |
|---|---|---|
| `mode` | `enforce` | The hook fires only on payment-shaped calls; a payment backstop that defaults to advisory is not a backstop. |
| `failClosed` | `true` | An unreachable verdict service blocks the *payment*, never the conversation. |
| `baseUrl` | `https://blackwall-free.onrender.com` | Keyless free tier; self-host for real workloads. First call after idle may take ~60s (cold start). |

No API key, no credential story: only the payment claim
`{counterparty, amount, asset, chain}` leaves the sandbox — data that is about
to be public on-chain anyway — never tool payloads.

## Use

```ts
import createBlackwallX402Gate from "./index.ts";

export default createBlackwallX402Gate({
  // baseUrl: "https://your-blackwall.example.com",
  // payer: "0xYourAgentWallet",
  // paymentTools: ["treasury"],   // extend payment-tool name detection
});
```

Env equivalents: `BLACKWALL_X402_URL`, `BLACKWALL_X402_MODE`,
`BLACKWALL_X402_FAIL_CLOSED`, `BLACKWALL_PAYER`.

## Tests

```sh
npm install && npm test    # vitest; the OpenClaw SDK entry import is stubbed
```

38 tests pin the claim extraction (all three shapes + adversarial inputs,
including case-variant keys and scientific-notation amounts), the exact
atomic-unit conversion, the verdict mapping, the enforce/observe × fail-closed
matrix, the zero-overhead pass-through for non-payment calls, and the HTTP
client's response-size cap over real loopback sockets. The HTTP client is a
zero-dependency proxy-aware CONNECT tunnel (Node's fetch ignores
`HTTPS_PROXY`, which matters in proxy-only-egress sandboxes).

```sh
npm run eval:live          # opt-in live scorecard against blackwall-free
```

`eval-live.test.ts` is the redteam-style scenario sweep (network, excluded from
`npm test`): fair-price GO, gouged HOLD, cold-start HOLD, sanctions STOP, a
recipient-swap attack (claim pays A, signed X-PAYMENT pays B) killed by
payload-sim, and case-variant-key recognition — 6/6 verified against the live
free instance.

## Consumers

The NemoClaw community example `blackwall-x402-payment-gate`
(nemoclaw-community repo) vendors this plugin alongside an OpenShell network
policy and an agent skill; this directory is the canonical source.
