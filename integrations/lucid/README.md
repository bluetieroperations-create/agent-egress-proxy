# Blackwall × Lucid Agents

A **counterparty** gate for the [Lucid Agents Commerce SDK](https://github.com/daydreamsai/lucid-agents).
Lucid enforces *how much* an agent may spend. This answers *who it is about to pay*.

An **adapter, not a clone** — it stands exactly where Lucid's own budget policy
stands and asks a different question.

## The gap this fills

Lucid's buyer path composes fetch wrappers, and its policy layer inspects the
unpaid x402 requirement and reserves budget **before a signature is created**,
rejecting a disallowed challenge with `403 policy_violation`. That is the right
place to stand. Its budget tier constrains:

| Lucid's policy | |
|---|---|
| `allowedRecipients` | a **static allowlist** |
| `maxPaymentUsd` | per-request ceiling |
| `maxTotalUsd` / `windowMs` | time-bounded total |

So it enforces **how much**, and **who** only from a list a human typed. An
allowlist cannot tell you that a payee you are about to add is a wash-trading
Sybil ring, is OFAC-sanctioned, quotes 50× its category's settled median, has
been silent for 90 days, or advertises a `payTo` that is not a possible address
on any chain. None of those are a spend limit. (Same gap
`integrations/agentcore/` documents about AWS Bedrock AgentCore Payments.)

## Install

```
npm install && npm test
```

Then place it **inside** `wrapFetchWithPayment`, where Lucid's own policy goes:

```ts
import { wrapFetchWithBlackwall } from "blackwall-lucid-policy";
import { wrapBaseFetchWithPolicy } from "@lucid-agents/payments";
import { wrapFetchWithPayment } from "@x402/fetch";

const gated  = wrapFetchWithBlackwall(fetch, { baseUrl: BLACKWALL_URL, payer: MY_ADDRESS });
const policy = wrapBaseFetchWithPolicy(gated, { maxPaymentUsd: 0.05, maxTotalUsd: 1.0 });
const paid   = wrapFetchWithPayment(policy, client);
```

A non-GO verdict turns the 402 into a **403** — the same currency Lucid's policy
already speaks — so `wrapFetchWithPayment` sees a refusal rather than a payable
challenge and **no signature is ever created**. This never signs, holds a key, or
moves money.

## Two design points the wrapper position forces

Both would be bugs if handled the obvious way.

**1. Every `accepts[]` entry is scored, not the first.** At policy time the
client has not chosen which requirement to satisfy — that happens inside
`wrapFetchWithPayment` when the scheme is selected. Scoring `accepts[0]` and
letting the client pay `accepts[1]` means the gate scored a payment that never
happened. Since the choice is unknowable here, the only sound rule is *safe for
whichever it picks*: the combined decision is the **most conservative** across
all entries. Verified live — one clean entry plus one sanctioned entry returns
`STOP`, with the reasons from both merged.

> `integrations/openclaw` reads `accepts[0]`. That is correct **there**: it sits
> at a tool-call boundary where the payload is already chosen. The assumption
> holds there and not here.

**2. All three challenge carriers are read.** Requirements arrive in the JSON
body, in `WWW-Authenticate: X402 requirements="<b64>"`, or in a bare-base64
`payment-required:` header. Measured on 195 live x402 hosts: **the body alone
leaves 86 unreadable, and 80 of those serve a complete v2 challenge in
`payment-required` with `{}` as the body.** A body-only gate therefore fails
**open** on ~41% of the live ecosystem while looking healthy — it sees no
challenge and passes the request through. Body wins on disagreement, because the
body is what other x402 clients actually pay.

## Behaviour

| verdict | default |
|---|---|
| `GO` | the 402 passes through, Lucid pays it |
| `HOLD` | **403** unless an `onConfirm` handler approves |
| `STOP` / `hard_stop` | **403**, always |
| Blackwall unreachable | **503** (fail-closed) |
| challenge unreadable | passes through (fail-open) |
| `mode: "observe"` | never blocks — measure the false-block rate first |

`onConfirm` is the human-in-the-loop seam. A handler that **throws is not an
approval**, and `mode: "observe"` overrides everything so the gate can be
measured before it enforces.

**Fail-closed is the default and the tradeoff is real in both directions:**
closed means a Blackwall outage stops the agent paying; open means an outage
silently removes the gate. Closed is the default because an unscored payment is
irreversible and a stopped agent is not — but an operator running unattended may
legitimately choose `failClosed: false`.

## One decision site

`decide` — the one place `GO`/`HOLD`/`STOP` becomes allow/confirm/block — is
**imported** from `../openclaw/core.js`, not copied. Two copies of "what does a
verdict mean" is exactly the drift this repo keeps paying for.

That import is why `integrations/openclaw` was split into `core.ts` (no host
dependency) and `index.ts` (the OpenClaw adapter): one `import ... from
"openclaw/plugin-sdk/plugin-entry"` was holding the claim parsing and the
decision hostage, making the module unimportable from anywhere that is not an
OpenClaw plugin. That split is the shape every other integration here already
had — `blackwall_guard.py`, `wallet_guard.py`, `agentcore_guard.py` — and this
one did not.

## Tests

```
npm test           # 27 tests, no network
npm run eval:live  # hits the real Blackwall endpoint
```

11 mutations verified killed, including scoring only `accepts[0]`, body-only
parsing, dropping either header carrier, returning the 402 regardless,
consuming the response body, and treating a thrown `onConfirm` as consent.

## Limitations, stated rather than implied

- **The thin shim is unverified against a live Lucid install.** The composition
  order and the `403` convention come from Lucid's published buyer docs, and
  every test here drives the wrapper directly. Nothing in this repo has run it
  against `@lucid-agents/payments` itself.
- **Amounts assume 6 decimals** when converting atomic units, which is right for
  USDC and wrong for an asset that is not. The engine's own
  `KNOWN_DECIMALS_BY_CHAIN` is the authority; this wrapper does not consult it.
- **A2A and ERC-8004 are not touched.** Lucid also carries agent-to-agent
  messaging and on-chain identity. This gates the *payment* leg only. ERC-8004
  identity is the more interesting future input — it would give the verdict a
  durable agent identity to accumulate reputation against, rather than an
  address.
