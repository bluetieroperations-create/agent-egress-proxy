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

22 tests. Mutation-verified: calling through on STOP, letting approval override a
STOP, failing open on an unreadable body, dropping the Permit2 allowance from the
claim, and returning `None` instead of raising each fail a test by name.
