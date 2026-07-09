# BlackWall — Threat Model

*Product: BlackWall agent egress proxy (`egress_proxy.py`) · Last updated: 2026-07-09*

This document states what BlackWall is designed to defend against, what it is
explicitly **not** designed to defend against, and how each defense is realized
in code. It is written for security reviewers and enterprise buyers who need to
reason about the tool before deploying it. Every mitigation links to the source.

---

## 1. What BlackWall is

A localhost-only HTTP/HTTPS (CONNECT) **forward proxy** that acts as a
**network-layer egress control** for AI agents and any process honoring
`HTTP_PROXY` / `HTTPS_PROXY`. It runs on the same host as the agent, logs every
destination reached, and — in enforce mode — blocks any host not on a
dot-boundary allowlist.

It pairs with an **action-layer** guardrail (which decides whether an agent
should *do* a thing). BlackWall decides where an agent can *reach*. Two locks.

---

## 2. Assets we protect

| Asset | Why it matters |
|-------|----------------|
| **Egress destination integrity** | Preventing a compromised/injected agent from reaching an attacker-controlled host to exfiltrate data or receive commands. |
| **The audit record** | A complete, trustworthy log of every destination attempted — the evidence a responder relies on. |
| **The host running the proxy** | The proxy must not become a resource-exhaustion or injection vector against the machine it protects. |
| **Confidentiality of tunneled traffic** | BlackWall must not itself become a plaintext-exposure point. |

---

## 3. Trust boundaries

```
   ┌─────────────────────────── customer host ───────────────────────────┐
   │                                                                      │
   │   [ AI agent / process ]                                             │
   │        │  HTTP(S)_PROXY = http://127.0.0.1:8888                      │
   │        ▼                                                             │
   │   ╔═══════════════════════╗          logs         ┌──────────────┐  │
   │   ║  BlackWall proxy      ║ ───────────────────▶  │ egress.log   │  │
   │   ║  (127.0.0.1 only)     ║                        │ (local JSONL)│  │
   │   ╚═══════════════════════╝                        └──────────────┘  │
   │        │  forward (allow / observe) only                             │
   └────────┼─────────────────────────────────────────────────────────────┘
            ▼
      [ upstream host:port ]   ◀── the ONLY outbound BlackWall ever makes
```

- **Untrusted input crosses the boundary at the client socket**: the request line
  and headers sent by the (potentially compromised) local agent.
- **The allowlist and CLI flags are trusted configuration**, supplied by the
  operator, not the agent.
- **BlackWall itself makes no outbound connection except the one the agent
  requested and policy permitted** — no telemetry, no update check.

---

## 4. Adversary model

| Adversary | Capability assumed | In scope? |
|-----------|--------------------|-----------|
| **A1 — Injected/prompt-poisoned agent** | Controls what URLs/hosts the agent *requests* through the proxy, but still uses the normal proxy path. | **Yes — primary.** |
| **A2 — Malicious payload crafting raw requests** | Sends hostile CONNECT lines / headers to the proxy (injection, smuggling, oversize, slowloris). | **Yes.** |
| **A3 — Resource-exhaustion attacker** | Opens many/absorbing connections to starve the host. | **Yes.** |
| **A4 — Fully-compromised agent with code execution** | Can open **raw sockets** and ignore `HTTP(S)_PROXY`. | **No — out of scope by design (see §6).** |
| **A5 — Local attacker already root on the host** | Can read the log, kill the proxy, rewrite the allowlist. | **No — outside the trust boundary.** |
| **A6 — Network MITM between proxy and upstream** | Intercepts the tunneled connection. | **Partially — TLS end-to-end integrity is the agent's/upstream's responsibility; BlackWall does not weaken it.** |

---

## 5. Threats and mitigations (in scope)

### T1 — Data exfiltration to a non-approved destination *(A1)*
**Mitigation.** Enforce mode forwards **only** allowlisted hosts; everything else
gets `403` and is logged as `block`, having egressed nothing
([`_handle_connect`, `egress_proxy.py:325-329`](egress_proxy.py);
[`decide`, `egress_proxy.py:164-175`](egress_proxy.py)).
**Fail-closed:** unknown mode → enforce; empty allowlist → block-all
([`host_allowed`, `egress_proxy.py:146-147`](egress_proxy.py)).

### T2 — Allowlist suffix-bypass *(A1)*
An attacker registers `evilexample.com` hoping it matches an `example.com` entry.
**Mitigation.** Dot-boundary matching: host must equal the entry or end with
`"." + entry`. `evilexample.com` and `example.com.attacker.com` are rejected
([`host_allowed`, `egress_proxy.py:137-161`](egress_proxy.py)). Regression-locked
by `test_suffix_bypass_rejected` / `test_attacker_suffix_rejected`.

### T3 — Request smuggling / header injection via the CONNECT line *(A2)*
**Mitigation.** After trimming a single trailing newline, any remaining CR/LF in
the request line is rejected; control chars, spaces, and DEL in the host token
are forbidden; the line must be exactly `VERB TARGET VERSION`
([`parse_connect_target`, `egress_proxy.py:57-134`](egress_proxy.py)). The same
host guard is applied on the plain-HTTP path
([`egress_proxy.py:438-443`](egress_proxy.py)).

### T4 — Malformed / ambiguous targets *(A2)*
**Mitigation.** Non-CONNECT verbs, missing/garbage structure, non-numeric or
out-of-range ports (`1..65535`), oversize hosts (>255), and ambiguous
unbracketed IPv6 are all rejected with `400` and logged as `reject-parse`
([`egress_proxy.py:57-134`, `315-322`](egress_proxy.py)).

### T5 — Oversize headers / slowloris *(A2, A3)*
**Mitigation.** Header read is capped at **16 KB** and bounded by a **10s
timeout**; exceeding either yields `400` + `reject-oversize`, and no upstream
connection is made ([`_read_headers`, `egress_proxy.py:251-272`](egress_proxy.py);
[`egress_proxy.py:279-285`](egress_proxy.py)).

### T6 — Connection-flood resource exhaustion *(A3)*
**Mitigation.** Thread-per-connection is bounded by a **semaphore (200)**; excess
connections get `503` and are closed instead of being serviced
([`egress_proxy.py:622-629`, `637-641`](egress_proxy.py)). Idle tunnels are torn
down after **300s** ([`egress_proxy.py:44`, `374-384`](egress_proxy.py)).

### T7 — Proxy used as an open relay from the network *(A2/A3)*
**Mitigation.** Bind is **`127.0.0.1` only**, never `0.0.0.0`
([`egress_proxy.py:595-596`](egress_proxy.py)). Remote hosts cannot reach it.

### T8 — Silent / unlogged egress (evasion of the audit trail) *(A1)*
**Mitigation.** The destination is logged the instant forwarding is committed —
before any bytes flow — and the byte-count tally is written on teardown in a
`finally`, so an exception mid-tunnel cannot suppress the destination record
([`_handle_connect`, `egress_proxy.py:349-361`](egress_proxy.py)). Invariant:
**every accepted connection produces at least one log line; every rejected
attempt egresses nothing but is still logged.**

### T9 — Plaintext exposure by the proxy *(A6)*
**Mitigation.** TLS is **tunneled, not terminated**: BlackWall relays opaque
encrypted bytes and never holds keys or plaintext
([`_tunnel`, `egress_proxy.py:363-411`](egress_proxy.py)). It cannot leak
plaintext it never possesses, and it does not weaken the agent↔upstream TLS
session.

---

## 6. Explicitly out of scope (non-goals)

These are **stated limitations, not undiscovered gaps.** Deploy compensating
controls where they matter to you.

### N1 — Raw-socket bypass by a fully-compromised agent *(A4)*
BlackWall only governs processes that honor `HTTP(S)_PROXY`. An agent with code
execution can open raw sockets and skip the proxy entirely.
**Compensating control:** OS-level default-deny egress — a host firewall rule or
network namespace / container that permits outbound **only** to
`127.0.0.1:<proxy port>`. BlackWall is the control point; the OS is what *forces*
traffic through it. Real containment uses both.

### N2 — Local privileged attacker *(A5)*
Anyone already root/admin on the host can read `egress.log`, edit the allowlist,
or kill the proxy. Protecting the log and config is the host's responsibility
(file permissions, integrity monitoring, shipping logs off-box).

### N3 — Content inspection / DLP
BlackWall gates by **destination**, not by payload. It does not inspect,
classify, or redact traffic content. Pair it with a content-layer control if you
need that.

### N4 — Fully-correct plain-HTTP proxying
Plain HTTP is best-effort (one-shot relay, `Connection: close`, no keep-alive /
chunked-request streaming / HTTP/2) — but still **gated and logged**
([`_handle_plain`, `egress_proxy.py:414-461`](egress_proxy.py)). The
HTTPS/CONNECT path is the fully-correct one.

### N5 — IDN / punycode normalization
The observe-mode log records the raw pre-IDNA host token; the OS resolver may map
it to a different ASCII name. Treat non-ASCII log entries with suspicion when
building an allowlist ([README "Log-fidelity caveat"](README.md)).

---

## 7. Verification

The security boundary is three pure functions — `parse_connect_target`,
`host_allowed`, `decide` — tested TDD-first in `test_egress_proxy.py`. Run:

```sh
python -m unittest test_egress_proxy.py -v
```

The suite documents its own mutation sensitivity: weakening the dot-boundary
match, dropping case-folding, dropping the trailing-dot strip, or making the
empty allowlist permissive each causes a specific test to **fail** — so the
guarantees above are regression-locked, not aspirational.

---

## 8. Residual risk summary

| Risk | Status | Owner of compensating control |
|------|--------|-------------------------------|
| Raw-socket bypass (A4) | Accepted / out of scope | Operator — OS firewall / netns |
| Local root tampering (A5) | Accepted / out of scope | Operator — host hardening |
| No content DLP | By design | Operator — pair with action-layer control |
| Plain-HTTP relay edge cases | Known limitation | Prefer HTTPS; low real-world exposure |
| IDN log ambiguity | Known limitation | Operator — allowlist hygiene |

BlackWall's guarantee is bounded and precise: **for proxy-respecting traffic, no
destination reaches the network unlogged, and in enforce mode no non-allowlisted
destination reaches the network at all.** Everything beyond that boundary is
named above and left to compensating controls.
