# Blackwall × AWS Bedrock AgentCore Payments

**AgentCore decides whether you can afford it. Blackwall decides whether you
should pay them.**

## The gap

AgentCore Payments is GA, speaks x402 and MPP, and connects to Coinbase and
Stripe/Privy. A payment session constrains exactly two things:

```
limits.maxSpendAmount   {value, currency}
expiryTimeInMinutes
```

There is no payee allowlist, no counterparty screening, no merchant restriction
and no price check. `ProcessPayment` validates the request, checks the budget and
signs — and the merchant's `payTo` is forwarded **verbatim** into the signature.

So the budget is enforced and the counterparty is never examined. Two exposures
follow directly:

| exposure | why the session cap cannot see it |
|---|---|
| **who you pay** | `payTo` is never evaluated at all |
| **the `upto` Permit2 allowance** | an allowance is not a spend, so a $1.00 budget coexists with an approval over the whole balance — and AWS documents granting an **unlimited** one as a normal option |

## What this does

Gates the `ProcessPayment` call **before** it signs:

- **GO** → calls through
- **HOLD** → asks a human; refuses by default if nobody answers
- **STOP** → withholds the call, so **no proof is ever generated**

Withholding is the only durable control. Once AgentCore returns
`PROOF_GENERATED`, the signed payload is in the agent's hands.

## Layout

Same shape as `integrations/wallets/`: a dependency-free core plus thin shims.

| file | needs |
|---|---|
| `agentcore_guard.py` | **stdlib only** — claim extraction, decision, gate |
| `strands_plugin.py` | a Strands `PaymentManager` (duck-typed) |
| `langgraph_middleware.py` | an AgentCore payments middleware to wrap |

Nothing here imports boto3, Strands or LangGraph. You pass in the
`process_payment` you already call; this decides **whether** to call it.

## Use

```python
from agentcore_guard import AgentCoreGuard, in_process_decider, FAIL_CLOSED

guard = AgentCoreGuard(in_process_decider(reputation_source),
                       on_unreachable=FAIL_CLOSED)

result = guard.process(process_payment_body, my_process_payment)
if result.processed:
    proof = result.response["paymentOutput"]
else:
    print(result.decision.verdict, result.decision.reasons)
```

Strands:

```python
from strands_plugin import BlackwallPaymentsPlugin, PaymentBlocked

plugin = BlackwallPaymentsPlugin(manager, on_hold=ask_the_human)
try:
    plugin.process_payment(**body)
except PaymentBlocked as e:
    ...            # raises rather than returning None, so a missed check
                   # cannot be mistaken for a failed payment and retried
```

LangGraph — wraps the real middleware and delegates everything it does not gate:

```python
from langgraph_middleware import BlackwallPaymentsMiddleware
guarded = BlackwallPaymentsMiddleware(AgentCorePaymentsMiddleware(config))
agent = create_agent(model=..., middleware=[guarded])
```

## Behaviour worth knowing

- **`mode=OBSERVE`** always calls through but still reports the verdict — measure
  before arming.
- **`on_unreachable`** governs both an unreachable Blackwall *and* a request body
  we cannot parse. An unreadable request is an unknown, not an approval.
- **Human approval covers HOLD, never STOP.** A HOLD is a question; a STOP is an
  answer.
- **Both payment types** are read: `CRYPTO_X402` from
  `paymentInput.cryptoX402.payload`, and `MPP` from the raw
  `WWW-Authenticate: Payment` challenge in `paymentInput.mpp`.
- **`upto`** uses `maxAmountRequired` as the amount — the ceiling is the exposure
  the wallet approves against — and carries `permit2AllowanceLimit` through under
  AWS's own spelling.

## Tests

Run from this directory:

```sh
python3 -m unittest discover -p 'test_*.py'
```

26 tests. Mutation-verified: calling through on STOP, letting approval override a
STOP, failing open on an unreadable body, dropping the Permit2 allowance from the
claim, returning `None` instead of raising, guessing a payload when the body is
ambiguous, and dropping the MPP currency fallback each fail a test by name.

## Two findings from auditing this adapter

**Ambiguous bodies are refused, not guessed.** AgentCore selects the protocol by
`paymentType`. With it absent and BOTH `cryptoX402` and `mpp` present, which one
AWS signs is unspecified — so picking one risks scoring a different payee from the
one that gets signed. An attacker shaping the body would put a clean payee in the
payload we score and a hostile one in the payload that gets signed.

**MPP names its token in `currency`, often as a symbol.** `x402_challenge` refuses
to put a symbol in the `asset` address slot (correct — address comparisons would
silently fail), but `forecast` requires an asset, so every MPP payment was
unscoreable and blocked. Fail-closed was safe and useless there: it blocked the
legitimate ones too. The symbol now goes in the claim, never into the parser's
accepts entry.


## Try it

```
python integrations/agentcore/demo.py
```

Four ProcessPayment requests, all inside a $1.00 session cap, all approved by
AgentCore's own two constraints. Every verdict is a live HTTP call to the public
Blackwall service — nothing recorded, nothing staged. Change a number and re-run.

| request | AgentCore | Blackwall |
|---|---|---|
| established merchant, $0.05 | approves | **GO** — 239 settlements, price in line |
| merchant nobody has paid | approves | **HOLD** — no history is a question, not a denial |
| $0.05 payment, **unlimited** Permit2 allowance | approves | **STOP** — signature withheld |
| $0.05 payment, allowance 1000× the quote | approves | **GO** — recorded, not gated |

The last row matters as much as the third. Headroom is how the metered scheme is
meant to be used, so a tight ratio would flag correct behaviour. The note is in
the response if you want it; the payment still goes through.
