# Blackwall — wallet signing guard (Turnkey · Privy)

Put the payment check at the moment of signing. These adapters gate a wallet
provider's server-side signing call with a Blackwall verdict: the wallet signs on
**GO**, asks a human on **HOLD**, and **withholds the signature on STOP** — so a bad
payment never leaves the wallet.

Two providers are included so you can pilot with whichever partners first; they
share one core (`wallet_guard.py`). The provider-specific file only maps that
provider's request shape into the check.

## The availability toggle (important)

Sitting in the signing path means every payment waits on Blackwall. When the verdict
engine is **unreachable**, you choose the behavior — and can flip it at runtime:

| `on_unreachable` | Blackwall down ⇒ | Use when |
|---|---|---|
| `FAIL_CLOSED` (default) | withhold the signature → **payments halt** | high-value/treasury; correctness over liveness |
| `FAIL_OPEN` | sign anyway → **advisory only** | agent spending wallets; liveness over the guarantee |

```python
guard.set_availability(W.FAIL_OPEN)   # toggle live
```

A **STOP verdict always withholds** — the toggle only governs the "can't reach
Blackwall" case.

**Customer-facing copy is built in** so your settings UI can render the choice in
plain language without re-writing it:

```python
W.describe_policy()                 # {question, default, options:[...], note}
W.describe_policy(W.FAIL_CLOSED)    # {label:"Pause payments", tagline, customer, best_for}
guard.describe_availability()       # copy for the guard's CURRENT setting
```

## Where the verdict comes from

```python
import wallet_guard as W
decide = W.in_process_decider()                       # blackwall.forecast, free/local
decide = W.http_decider("https://your-blackwall.example")   # a deployed service
guard  = W.WalletGuard(decide, mode=W.ENFORCE, on_unreachable=W.FAIL_CLOSED,
                       confirm=lambda d: ask_human(d.summary()))
```

It runs the full engine — reputation, price-anomaly, sanctions, **and** the
payload/calldata screen — so a transaction that *looks* fine but is actually an
unlimited-approval drainer is withheld too.

## Turnkey

```python
from turnkey_signer import TurnkeyGuard

# your existing Turnkey call (SDK or stamped HTTP) that actually signs:
def my_turnkey_sign(request): ...        # -> signature

tk = TurnkeyGuard(guard, sign_fn=my_turnkey_sign)

# attach the tx object you already hold (Turnkey's unsignedTransaction is RLP):
request = {"parameters": {"signWith": agent_addr},
           "transaction": {"to": token, "value": 0, "data": calldata, "chain": "base"}}
res = tk.sign(request)
if res.signed:  broadcast(res.signature)
else:           handle_block(res.decision.summary())
```

## Privy

```python
from privy_signer import PrivyGuard

def my_privy_rpc(request): ...           # your walletApi.rpc call -> signature
pv = PrivyGuard(guard, sign_fn=my_privy_rpc)

res = pv.sign({"method": "eth_signTransaction",
               "params": [{"to": token, "value": 0, "data": calldata}]})
```

For a signed x402 payment (typed data), pass `payment_authorization` so the
signature is cross-checked against the claim (payload-sim Phases 1–2).

## What each returns

`res` is a `SignResult`: `.signed` (bool), `.signature` (from your signer, or None
if withheld), `.decision` (`.verdict`, `.action`, `.reasons`, `.summary()`).

**For your end-user UI**, don't render the raw `reasons` (they include internal
signals). Use the built-in plain-English message instead:

```python
msg = W.customer_message(res.decision)
# {status:"Blocked", headline:"Blackwall stopped this payment to protect your funds.",
#  reason:"the recipient is on a sanctions list",
#  detail:"...Blocked... Reason: ..."}
```

It leads with the single clearest reason, in real amounts (not atomic units), and
never leaks internal stats. See also `docs/TRANSPARENCY.md` for a customer-facing
"what Blackwall checks, sees, and stores" note.

## Notes / scope

- The `sign_fn` is **yours** — the adapter decides *whether* to call it, not how to
  talk to the provider. That keeps it SDK-version-agnostic and testable.
- `claim_from_tx` decodes ERC-20 `transfer`/`transferFrom` to recover the real
  recipient + amount; a raw **RLP-encoded** tx isn't decoded here — pass the tx
  object (which you have before encoding), or add RLP decoding.
- Reference adapters: pilot with a provider, then generalize. Same core works for
  Dynamic, Fireblocks (co-signer callback), etc. — write a new `*_extract`.

## Tests

```sh
python -m unittest test_wallet_guard.py test_turnkey_signer.py test_privy_signer.py
```
