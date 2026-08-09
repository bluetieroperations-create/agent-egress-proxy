# Blackwall — LangChain payment guard

"Call Blackwall before you sign." A thin LangChain adapter that runs the Blackwall
pre-signature verdict (GO / HOLD / STOP) on a payment before your agent sends it —
from counterparty reputation, price-anomaly, OFAC sanctions, and (when you pass the
signed payload) a cryptographic cross-check that the signature really pays who and
what you think.

Two ways to use it:

- **A tool the agent calls** (`BlackwallPaymentGuardTool`) — the model asks for a
  verdict before paying.
- **A guardrail that enforces** (`BlackwallGuardrailCallback`) — intercepts your
  payment tool and blocks a STOP / human-confirms a HOLD *even if the model forgets
  to check*.

The decision logic lives in `blackwall_guard.py`, which has **no LangChain
dependency** and is stdlib-only; `langchain_blackwall.py` is the adapter.

## Install

This directory is a pip-installable package (`blackwall-langchain`):

```sh
pip install blackwall-langchain           # core only (talks to a deployed Blackwall over HTTP)
pip install "blackwall-langchain[langchain]"   # + the LangChain adapter
```

The **core** (`blackwall_guard`) is stdlib-only and needs no engine locally — it
calls a deployed Blackwall via `HttpEngine`. To run the engine **in-process**
instead (free, no network), also have `blackwall.py` importable (the repo root on
`PYTHONPATH`); `InProcessEngine` picks it up automatically.

### Publishing (maintainers)

```sh
cd integrations/langchain
python -m pip wheel . -w dist --no-deps        # or: python -m build
python -m twine upload dist/blackwall_langchain-*.whl   # needs your PyPI token
```

Test the build in isolation first: `pip install <wheel> --target /tmp/t` then import
`blackwall_guard` / `langchain_blackwall` with only `/tmp/t` on the path.

## Engines: where the verdict comes from

```python
import blackwall_guard as G

engine = G.InProcessEngine()                       # deterministic, free, no network
# or talk to a deployed Blackwall service:
engine = G.HttpEngine("https://your-blackwall.example")   # POST /v1/forecast-payment
```

`HttpEngine` raises `PaymentRequired` on an x402 `402` (the hosted endpoint is
paid); `InProcessEngine` runs the engine locally for free.

## Modes

- `G.ENFORCE` (default) — STOP blocks, HOLD asks a human, GO proceeds.
- `G.OBSERVE` — never blocks; surfaces the verdict so you can log/alert first.

Fail-safe: if the engine is unreachable, ENFORCE escalates to a human **CONFIRM**
(never a silent allow) unless you pass `fail_open=True`.

## 1) The agent-callable tool

```python
from langchain_blackwall import BlackwallPaymentGuardTool

guard = BlackwallPaymentGuardTool(engine=G.InProcessEngine(), mode=G.ENFORCE)

# bind it alongside your other tools; the model calls it before paying
print(guard.invoke({"counterparty": "0xF00...", "amount": "0.09",
                    "asset": "USDC", "chain": "base"}))
# -> "VERDICT: STOP (BLOCK). STOP -- do NOT sign this payment. Reasons: ..."
```

Pass the **signed payload** to get the payload-simulation cross-check too:

```python
guard.invoke({
    "counterparty": "0xF00...", "amount": "0.09", "asset": "USDC", "chain": "base",
    "payment_authorization": "<base64 X-PAYMENT you're about to send>",   # Phase 1/2
    # or a raw contract call to screen for drainer calldata:
    "transaction": {"to": "0x...", "data": "0x095ea7b3..."},              # Phase 3
})
```

## 2) The guardrail (enforcement)

Wrap your existing payment tool so the check runs first — no reliance on the model
remembering to call the guard:

```python
from langchain_blackwall import BlackwallGuardrailCallback
from blackwall_guard import PaymentBlocked

def extract(serialized, tool_input):
    # return the payment fields for YOUR payment tool(s); None to pass others through
    if serialized.get("name") != "send_usdc" or not isinstance(tool_input, dict):
        return None
    return {"counterparty": tool_input["to"], "amount": tool_input["amount"],
            "asset": "USDC", "chain": "base"}

guardrail = BlackwallGuardrailCallback(
    engine=G.InProcessEngine(), extract=extract, mode=G.ENFORCE,
    confirm=lambda decision: input(f"{decision.summary()}\nApprove? [y/N] ") == "y")

# attach to any run; a STOP raises PaymentBlocked and the payment tool never runs
try:
    agent.invoke(task, config={"callbacks": [guardrail]})
except PaymentBlocked as e:
    print("Blocked:", e.decision.summary())
```

## Tests

```sh
python -m unittest test_blackwall_guard.py          # core, stdlib only
python -m unittest test_langchain_blackwall.py      # adapter (skips w/o langchain-core)
```
