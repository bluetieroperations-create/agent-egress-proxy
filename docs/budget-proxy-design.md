# Design: x402 Budget Proxy — the "third lock"

**Status:** Draft / RFC
**Author:** (brainstorm output)
**Target:** extension of `egress_proxy.py`
**Scope:** add a client-side, agent-agnostic **spend guardrail** on top of the
existing network-layer egress control.

---

## 1. Why

`egress_proxy.py` already sits in the exact place a budget guardrail needs to
be: it is a localhost forward proxy that every proxy-respecting agent
(`HTTP_PROXY` / `HTTPS_PROXY`) routes its outbound through, with **no change to
the agent's code**. Today it answers one question per destination:

> *Is this agent allowed to **reach** `host:port`?*  (network lock)

The README already frames this as one of "two locks" — a **network layer**
(where an agent may *reach*) paired with an **action layer** (whether an agent
may *do* a thing). The rise of [x402][x402] micropayments adds a third question
that neither lock answers:

> *Is this agent allowed to **spend** this much, right now, in total?*  (money lock)

x402 turns any HTTP 402 response into a machine-payable invoice: the server
replies `402 Payment Required` with a set of payment offers, the client signs a
stablecoin payment and **retries the same request** carrying an `X-PAYMENT`
header, and the server settles and returns the resource. An agent stuck in a
retry loop against a paid MCP tool or API can submit the same payment hundreds
of times in seconds and drain a wallet before a human notices.

The competitive landscape (see the brainstorm notes) is almost entirely
**wallet-native**: Coinbase Agentic Wallets, OpenZeppelin policy contracts,
AgentLeash, Fystack. Each requires the agent to adopt *their* wallet SDK. A
proxy is different: it is **agent-agnostic and code-change-free** — it gates the
payment *in flight*, before the signed payment leaves the machine, for any agent
that honors proxy env vars. That is the same wedge that makes the existing
egress proxy useful, extended from *destinations* to *dollars*.

This doc specifies that extension. It deliberately mirrors the existing code's
philosophy: **pure, unit-tested decision functions at the security boundary**,
**fail-closed** defaults, **stdlib-only**, and the **"no silent egress"**
invariant — extended here to **"no silent spend."**

[x402]: https://github.com/coinbase/x402

---

## 2. The hard constraint: CONNECT tunnels are opaque

This is the single most important fact shaping the design, and it must be stated
before any feature list.

For HTTPS the proxy uses the standard `CONNECT` method (`_handle_connect`,
`egress_proxy.py:315`). After it replies `200 Connection Established`, it becomes
a **blind byte relay** (`_tunnel`, `egress_proxy.py:363`): it copies encrypted
bytes between client and upstream and can see **only the destination
`host:port`** — never the HTTP status, headers, or body inside the TLS session.

But the entire x402 handshake — the `402` offer, the price, the `X-PAYMENT`
header on the retry — lives **inside** that TLS stream. **A blind CONNECT tunnel
cannot see a payment, let alone gate one.** Any design that claims to enforce a
per-payment budget over HTTPS without addressing this is lying.

There are exactly three ways to get the visibility, and we will support two of
them explicitly, with the trade-offs named:

| Approach | Visibility | Cost / risk | Verdict |
|---|---|---|---|
| **A. Blind tunnel (today)** | destination only | none | keep as default for non-x402 traffic |
| **B. Plain-HTTP path** | full HTTP | already implemented (`_handle_plain`, `egress_proxy.py:414`); but x402 is rarely plaintext | support, but insufficient alone |
| **C. Selective TLS termination (MITM)** for a named set of x402 hosts | full HTTP over HTTPS | requires a local CA the agent trusts; invasive; must never touch wallet keys | **the real production path**, opt-in, scoped |

**Chosen approach: C, scoped as narrowly as possible.** The proxy keeps blind
tunneling by default. Only for hosts on an explicit **`x402-hosts` list** does it
terminate TLS, inspect the HTTP exchange for the x402 handshake, apply the budget
policy, and re-originate the request upstream over a fresh TLS connection. Every
other destination stays a blind tunnel exactly as today — the MITM surface is
opt-in and minimized to the endpoints the operator actually pays.

This mirrors how `mitmproxy` works, but we are not shipping a general
interception platform — only the smallest slice needed to read a 402 offer and
gate the retry.

### Non-negotiable boundary: the proxy never holds keys and never signs

The proxy's power is strictly **allow / block / hold**. It **never** possesses
the wallet private key, **never** constructs or signs an `X-PAYMENT` payload, and
**never** initiates a payment the agent did not. It can only *prevent* a payment
the agent is trying to make, or *pass it through*. This keeps the trust model
identical to the existing proxy (it can stop traffic; it can't forge it) and
means a compromise of the proxy cannot itself move funds.

---

## 3. What x402 looks like on the wire (the part we parse)

The proxy only needs to understand three moments in the flow. (Field names
follow the x402 v1 scheme; the parser is written defensively so unknown/extra
fields are ignored and a malformed offer fails closed.)

1. **The offer.** Server → client:
   ```
   HTTP/1.1 402 Payment Required
   Content-Type: application/json

   { "x402Version": 1,
     "accepts": [
       { "scheme": "exact",
         "network": "base",
         "maxAmountRequired": "20000",      // atomic units (USDC, 6 decimals) = $0.02
         "asset": "0x...USDC",
         "payTo": "0x...seller",
         "resource": "https://api.example.com/tool/search",
         "description": "search call",
         "maxTimeoutSeconds": 60 } ] }
   ```
   The proxy reads `accepts[]` and, for the offer the client will use,
   extracts **(amount, asset, network, payTo, resource)**. `maxAmountRequired`
   is the ceiling the client is authorizing — that is the number the budget is
   checked against.

2. **The paid retry.** Client → server, same request URL, plus:
   ```
   X-PAYMENT: <base64(JSON payment payload)>
   ```
   The payload is the signed authorization. The proxy decodes the base64 to
   confirm **which offer** is being paid and the **amount** it authorizes, then
   matches it to the pending offer it saw in step 1. **This is the gate point.**

3. **The settlement receipt.** Server → client on success:
   ```
   HTTP/1.1 200 OK
   X-PAYMENT-RESPONSE: <base64(settlement result, tx hash, ...)>
   ```
   The proxy reads this to record **settled** spend (as opposed to merely
   *authorized*), which is what the ledger and analytics should count.

The proxy must **never** mutate the payment payload — it forwards `X-PAYMENT`
byte-for-byte or blocks the request. Editing it would invalidate the signature
anyway; the rule exists to make the "never forge" boundary structural.

---

## 4. Policy model

A budget policy is a small declarative file, loaded the way `load_allowlist`
loads the allowlist (`egress_proxy.py:181`) — comments, blank lines, fail-closed
on parse error. Proposed format is TOML-ish/JSON (stdlib `json`; no new deps):

```json
{
  "asset": "USDC",
  "unit_decimals": 6,

  "limits": {
    "per_call_max":      "0.10",   // reject any single payment above this
    "per_session_total": "2.00",   // cumulative cap for this proxy run / session
    "per_day_total":     "10.00",  // rolling 24h cap (persisted ledger)
    "prompt_above":      "0.50"    // hold + ask a human above this (but <= per_call_max)
  },

  "per_host": {
    "api.example.com": { "per_call_max": "0.25", "per_day_total": "5.00" }
  },

  "per_tool": {
    // keyed by resource path or MCP tool name parsed from the offer
    "/tool/search":  { "per_call_max": "0.02" },
    "/tool/deepgen": { "per_call_max": "0.40", "prompt_above": "0.20" }
  },

  "on_over_budget": "block",       // block | prompt | log-only(observe)
  "unknown_offer":  "block"        // fail-closed: an offer we can't parse is blocked
}
```

Design rules, consistent with the existing code:

- **Amounts are decimal strings, compared as integers in atomic units.** No
  floats anywhere in the decision path (float drift on money is a bug). Parse
  `"0.10"` × `10^decimals` → integer once, at load time.
- **Most specific wins:** `per_tool` overrides `per_host` overrides top-level
  `limits`, evaluated as a max-of-constraints (every applicable limit must pass).
- **Missing limit = unlimited for that dimension**, *except* the top-level
  `per_call_max`, which — like an empty allowlist in enforce mode — defaults to
  **block-all** when the mode is `enforce-budget` and no limit is set. Loud
  warning on startup, exactly like the empty-allowlist warning
  (`egress_proxy.py:603`).
- **Asset/network mismatch is a block, not a conversion.** The proxy does not do
  FX. If an offer is priced in an asset the policy doesn't name, that is an
  `unknown_offer` → fail closed. (v2 may add an oracle; v1 refuses.)

---

## 5. The decision function (pure, testable — the new security boundary)

Following the existing pattern (`parse_connect_target` / `host_allowed` /
`decide` are pure and unit-tested first), the budget logic is factored into pure
functions with **no I/O and no sockets**, so they can be TDD'd in isolation:

```python
def parse_x402_offer(body_bytes, headers) -> Offer | None:
    """402 body/headers -> normalized Offer(amount_atomic, asset, network,
    pay_to, resource) or None to REJECT (unparseable => fail closed)."""

def extract_payment_amount(x_payment_header) -> (amount_atomic, asset) | None:
    """Decode base64 X-PAYMENT -> the amount it authorizes, or None."""

def budget_decide(offer, ledger_snapshot, policy) -> Decision:
    """The core gate. Returns one of:
         ALLOW            - under all applicable limits
         BLOCK(reason)    - exceeds a hard limit, or unknown/mismatched offer
         PROMPT(reason)   - within hard limits but above a `prompt_above` band
       Pure: (offer, current cumulative spend, policy) -> decision.
       No mutation; the caller commits to the ledger only after ALLOW."""
```

`Decision` mirrors the string-return simplicity of the current `decide`
(`egress_proxy.py:164`) but carries a reason for the log. `budget_decide` is
where the `evilblackwalltier.com`-style edge cases of money live — off-by-one on
atomic units, the boundary at exactly `per_call_max`, an offer whose amount is
`0`, a negative/overflowing amount string — and, like `host_allowed`, it is the
function that gets the densest test table.

### Enforcement state machine (per client connection, per resource)

```
        see 402 offer            see retry w/ X-PAYMENT
  IDLE ───────────────► PENDING ─────────────────────────► gate
                          │                                   │
                          │ (offer for resource R,            ├─ ALLOW  → forward retry,
                          │  amount A, cached briefly)         │          on 200 record SETTLED(A)
                          │                                   ├─ BLOCK  → drop retry, synth 402/403
                          └── expires after N s / mismatch    └─ PROMPT → hold, ask human (§6)
```

- The pending offer is keyed by `(client, host, resource)` and **short-lived**
  (e.g. `maxTimeoutSeconds` from the offer, capped). This prevents a stale offer
  being reused to authorize a later, different charge.
- **Authorized vs. settled:** the gate checks against *authorized* amount at
  retry time; the ledger increments *settled* spend only when the `200` +
  `X-PAYMENT-RESPONSE` confirms the charge went through. A blocked or failed
  payment never counts against the budget. (Mirrors the current split between
  logging the *destination* at forward-commit and the *byte tally* at teardown,
  `egress_proxy.py:356`–`361`.)

---

## 6. Human-in-the-loop ("prompt above $X")

`PROMPT` needs an out-of-band approval channel because the agent's HTTP request
is blocked waiting on a yes/no a human must give. v1 keeps this deliberately
minimal and stdlib-only:

- **Hold:** the proxy parks the retry (does not forward, does not reject yet) up
  to a bounded `approval_timeout` (default 60s, ≤ the offer's `maxTimeoutSeconds`).
- **Ask:** write a structured `approval-request` line to a well-known path
  (`approvals.jsonl`) **and** to stderr: destination, resource, amount,
  cumulative session spend. A tiny companion CLI (`approve <id>` / `deny <id>`)
  or a local unix-socket/HTTP control endpoint on `127.0.0.1` flips the state.
- **Default on timeout = deny** (fail-closed). A human who isn't watching is not
  a rubber stamp.

This is the one genuinely new UX surface. It's kept out of the hot path: normal
sub-threshold payments never touch it.

---

## 7. Logging & analytics hook ("no silent spend")

Extend the existing JSONL record (`_log`, `egress_proxy.py:224`) rather than
inventing a second log. New optional fields, written only for x402 events:

```json
{ "ts":"...", "client":"...", "method":"POST", "host":"api.example.com",
  "port":443, "decision":"pay-allow",
  "x402": { "resource":"/tool/search", "amount":"0.02", "asset":"USDC",
            "network":"base", "pay_to":"0x...", "phase":"authorized",
            "session_total":"0.44", "tx":null } }
```

Decisions extend the existing vocabulary (`observe-forward`, `allow`, `block`,
`reject-*`) with: `pay-allow`, `pay-block-overbudget`, `pay-block-unknown`,
`pay-hold`, `pay-settled`. The invariant carries over verbatim: **every payment
attempt — allowed, blocked, or held — produces at least one log line, even
though a blocked payment egresses nothing.**

This log *is* the data exhaust for the analytics idea (idea #3 in the
brainstorm): per-agent (`client` / user-agent), per-endpoint, per-asset spend,
settled-vs-authorized, refusal rate. No separate collection path needed — the
dashboard reads the same JSONL. The proxy is the one place that sees **both the
client identity and the x402 headers**, which neither public block-explorers nor
server-side paywalls can see from the buyer's side.

---

## 8. New modes & CLI

Add a mode alongside `observe` / `enforce`; keep them composable (network lock
and money lock are independent):

```
python egress_proxy.py \
    --mode enforce \                     # existing network allowlist enforcement
    --allowlist allowlist.txt \
    --budget budget.json \               # NEW: enable the money lock
    --budget-mode enforce \              # observe | enforce (default observe)
    --x402-hosts x402-hosts.txt \        # NEW: hosts to TLS-terminate for x402
    --tls-ca ca.pem --tls-key ca-key.pem # NEW: local CA for selective MITM
```

- `--budget-mode observe`: parse and **log** payments, enforce nothing (the
  allowlist-building workflow's analog — run it first, watch real spend, then set
  limits). This is the safe default and the recommended on-ramp.
- `--budget-mode enforce`: apply `budget_decide`.
- Absent `--budget`, behavior is **byte-for-byte identical to today** — the
  money lock is purely additive and off by default.
- Absent `--x402-hosts` / `--tls-ca`, x402 enforcement is only possible on the
  plain-HTTP path (§2-B); HTTPS stays a blind tunnel and the proxy logs a
  one-time notice that HTTPS x402 needs termination to be gated.

---

## 9. Failure modes & fail-closed table

| Situation | `budget-mode observe` | `budget-mode enforce` |
|---|---|---|
| 402 offer unparseable | log `pay-block-unknown`, forward | **block** |
| amount in unknown asset | log, forward | **block** (no FX) |
| X-PAYMENT can't be matched to a pending offer | log, forward | **block** (no unpriced payment) |
| over a hard limit | log, forward | **block** |
| in `prompt` band, human times out | log, forward | **deny** (fail-closed) |
| TLS termination fails for an x402 host | log, fall back to blind tunnel + warn | **block** (can't gate what we can't see) |
| ledger file unwritable | warn to stderr, continue (like `_log` today) | **block** new spend (can't account => can't authorize) |
| agent bypasses proxy via raw socket | *out of scope* — same honest limit as the network lock (see README): needs OS-level egress control | same |

The last row is the same honesty the README already states for the network lock:
this governs the **proxy-respecting** path. It is a spend *guardrail*, not a
custody *vault*. Both locks should be paired with OS-level default-deny egress
for a fully-compromised-agent threat model.

---

## 10. Security considerations

- **No key custody, ever** (§2). The proxy allows/blocks/holds; it never signs.
- **Selective MITM only.** TLS is terminated **only** for explicitly listed
  x402 hosts. Every other destination is an untouched blind tunnel — the proxy
  cannot read your bank, your model API, or anything you didn't opt in. The
  local CA should be a dedicated, short-lived cert scoped to the agent's trust
  store, never a system-wide root.
- **Replay / double-spend within the proxy:** the pending-offer cache is
  single-use and expires; a retry that doesn't match a live pending offer is
  blocked in enforce mode. The proxy does not (and cannot) prevent on-chain
  replay — that's the wallet/facilitator's job — but it won't let one 402 offer
  authorize two charges through it.
- **Ledger integrity:** the day/session ledger is local state an attacker on the
  box could edit to raise the ceiling. This is acceptable for the client-side
  guardrail threat model (the attacker who owns the box owns the wallet too), but
  should be documented, not hidden.
- **Injection guards carry over:** hosts parsed on the x402 path reuse the same
  `_FORBIDDEN_HOST_CHARS` / length checks as `parse_connect_target`.

---

## 11. Backward compatibility & phased rollout

The extension is strictly additive. Ship it in slices, each independently
useful and testable:

- **Phase 0 — accounting only (no MITM).** `--budget-mode observe` on the
  **plain-HTTP path** and on CONNECT *metadata* (destination + byte volume).
  Parses 402/`X-PAYMENT` where visible, logs spend, enforces nothing. Zero risk,
  immediately feeds the analytics story. *Lands first.*
- **Phase 1 — enforcement on visible traffic.** `budget_decide` wired into the
  plain-HTTP path and the pending-offer state machine. Real caps, real blocks,
  for plaintext and already-terminated traffic.
- **Phase 2 — selective TLS termination.** The `--x402-hosts` + local-CA path
  that makes HTTPS x402 gate-able. The heaviest, most invasive slice; opt-in.
- **Phase 3 — human-in-the-loop approvals** (§6) and the analytics dashboard on
  top of the JSONL exhaust (idea #3).

---

## 12. Testing plan

Mirror `test_egress_proxy.py`: **pure functions get an exhaustive TDD table
before any socket code.**

- `parse_x402_offer`: valid single/multi-offer, missing fields, wrong types,
  extra unknown fields (must ignore), non-JSON body, empty `accepts`, huge body
  (reuse the header-cap discipline).
- `extract_payment_amount`: valid base64/JSON, truncated base64, amount as
  string vs number, negative, zero, overflow, wrong asset.
- `budget_decide`: the money edge-case table — exactly at `per_call_max`
  (boundary), one atomic unit over, session total that would be crossed by this
  call, per-tool overriding per-host overriding global, unknown asset, missing
  limits (unlimited vs. fail-closed default), `prompt` band boundaries.
- Atomic-unit conversion: `"0.10"` → `100000` and back; no float ever appears.
- State machine: offer→retry match, offer expiry, mismatch, settled-vs-authorized
  ledger increment, blocked payment does **not** increment.
- End-to-end (like the existing socket tests): a stub upstream that speaks 402
  then 200, asserting the proxy blocks over-budget and forwards under-budget, and
  that the log lines/ invariants hold.

---

## 13. Open questions

1. **Selective MITM vs. an explicit x402-aware sidecar.** Instead of terminating
   TLS, we could expose a small local endpoint the agent's x402 client library is
   pointed at for settlement calls (opt-in integration, no MITM). Cleaner trust
   model, weaker "no code change" promise. Which matters more to the target user
   — zero-integration, or zero-MITM? (Could offer both.)
2. **Ledger persistence & multi-process.** One proxy process = one session is
   simple. A shared daily cap across concurrent agents needs shared, locked state
   (sqlite? a file lock?). How multi-agent is the real deployment?
3. **Network/asset scope for v1.** Base + USDC only, or pluggable from day one?
4. **Approval channel.** stderr + CLI is the stdlib-only MVP; is a local web
   approval UI worth the dependency later?
5. **Interaction with the network allowlist.** Should a paid host be *required*
   to also be on the network allowlist (defense in depth), or are the two locks
   fully orthogonal? Proposed: orthogonal by default, with an opt-in
   "paid-implies-allowed" convenience flag.

---

## Appendix: how this maps to the existing code

| Existing | New analog |
|---|---|
| `parse_connect_target` (parse+reject, fail-closed) | `parse_x402_offer`, `extract_payment_amount` |
| `host_allowed` (dot-boundary edge cases) | `budget_decide` (atomic-unit / boundary edge cases) |
| `decide(host, mode, allowlist)` → forward/block | `budget_decide(offer, ledger, policy)` → allow/block/prompt |
| `load_allowlist` (comments, fail-closed) | `load_policy` (json, fail-closed, decimals→atomic once) |
| "no silent egress" (log every attempt) | "no silent spend" (log every payment attempt) |
| empty allowlist + enforce ⇒ block-all | no `per_call_max` + enforce-budget ⇒ block-all |
| observe vs enforce | budget-mode observe vs enforce (independent, composable) |
| HONEST LIMIT: raw-socket bypass needs OS egress control | same limit applies to the money lock |
