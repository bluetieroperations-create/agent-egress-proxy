# egress-proxy

A localhost-only HTTP/HTTPS (CONNECT) **forward proxy** that acts as a
**network-layer egress control** for AI agents — and any process that honors the
standard `HTTP_PROXY` / `HTTPS_PROXY` environment variables.

Point an agent's outbound through it and you get a complete **audit log of every
destination it reaches** (observe mode), and the ability to **block anything not
on an allowlist** (enforce mode) — so an injected/compromised agent can't quietly
exfiltrate. It's the **network layer** that pairs with an **action layer**
guardrail (which decides whether an agent should *do* a thing): this decides
where it can *reach*. Two locks.

- **Observe mode** — logs every **forwarded** destination (`host:port`) an agent
  reaches **and every rejected/blocked attempt** (parse-fail, oversize, block).
- **Enforce mode** — additionally **blocks** any host not on a dot-boundary allowlist.

> **Every accepted connection produces at least one log line.** A *rejected*
> attempt (malformed/oversize CONNECT, or an enforce-mode block) **egresses
> nothing** — the proxy never connects upstream — but it is **still logged**, so
> the observe log is a complete attempt record you can build the allowlist from.

Python **stdlib only** (no pip deps). Python 3.8+.

It binds **`127.0.0.1` only** — it is *not* an open relay; only local processes
(your agent / process) can use it.

---

## Why this works

Most HTTP clients honor the standard `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`
environment variables out of the box — Python `requests`, Node's `undici`
(via `EnvHttpProxyAgent`), `curl`, Go, and more. Point those env vars at this
proxy and all proxy-respecting outbound traffic is logged (and, in enforce
mode, gated) — **with no change to the agent's code**.

---

## Quick start

### 1. Run in OBSERVE mode (log everything, block nothing)

```sh
python egress_proxy.py --mode observe --port 8888
```

Then wire your agent / process to it (in the same shell that launches it):

**PowerShell**
```powershell
$env:HTTP_PROXY  = "http://127.0.0.1:8888"
$env:HTTPS_PROXY = "http://127.0.0.1:8888"
# start your agent here
```

**bash / sh**
```sh
export HTTP_PROXY="http://127.0.0.1:8888"
export HTTPS_PROXY="http://127.0.0.1:8888"
```

> Note: even for HTTPS the proxy URL is `http://127.0.0.1:8888` — that is the
> address of the *proxy*, not the upstream. `requests` issues a `CONNECT` to it.

Let the agent run. Every destination lands in `egress.log` (JSONL).

### 2. Build the allowlist from the log

Each log line has a `host` field. Collect the legitimate ones into an
allowlist file (one host per line). Start from `allowlist.txt.example`.

```sh
cp allowlist.txt.example allowlist.txt   # then edit
```

### 3. Switch to ENFORCE mode (block non-allowlisted hosts)

```sh
python egress_proxy.py --mode enforce --allowlist allowlist.txt --port 8888
```

Now any host not matching the allowlist gets a `403 Forbidden` and is logged
with `"decision":"block"`.

> In enforce mode, an **empty or missing allowlist means block-all**
> (fail-closed). The proxy prints a loud warning if you do this.

---

## CLI

```
python egress_proxy.py [--port N] [--mode observe|enforce]
                       [--allowlist FILE] [--log FILE]
```

| Flag          | Default                | Meaning                                        |
|---------------|------------------------|------------------------------------------------|
| `--port`      | `8888`                 | Listen port (bind is always `127.0.0.1`).      |
| `--mode`      | `observe`              | `observe` = log only; `enforce` = gate.        |
| `--allowlist` | (none)                 | Allowlist file path.                           |
| `--log`       | `egress.log` (beside script) | JSONL log file.                          |

Env fallbacks: `EGRESS_PROXY_PORT`, `EGRESS_PROXY_MODE`,
`EGRESS_PROXY_ALLOWLIST`, `EGRESS_PROXY_LOG`.

Stop with **Ctrl-C** (clean listener shutdown).

---

## Allowlist matching semantics

Dot-boundary, case-insensitive, trailing-dot-stripped. An entry `example.com`:

| Host                          | Allowed? |
|-------------------------------|----------|
| `example.com`                 | yes      |
| `api.example.com`             | yes (subdomain) |
| `EXAMPLE.com` / `example.com.`| yes (case / trailing dot) |
| `evilexample.com`             | **NO** (classic suffix bypass) |
| `example.com.attacker.com`    | **NO**   |
| anything, empty allowlist     | **NO** (fail-closed) |

---

## Log format (JSONL, one object per line)

```json
{"ts":"2026-06-24T22:22:27Z","client":"127.0.0.1:59878","method":"CONNECT","host":"localhost","port":59876,"decision":"allow","bytes_up":12,"bytes_down":17}
```

`decision` is one of:

| `decision`         | Meaning                                                        | Egressed? |
|--------------------|----------------------------------------------------------------|-----------|
| `observe-forward`  | observe mode, forwarded                                         | yes       |
| `allow`            | enforce mode, allowlisted + forwarded                          | yes       |
| `block`            | enforce mode, host not on allowlist → `403`                    | **no**    |
| `reject-parse`     | malformed request / CONNECT target → `400`                     | **no**    |
| `reject-oversize`  | header exceeded 16 KB cap or read timed out → `400`            | **no**    |

The `-close` suffix (e.g. `allow-close`) is the teardown record carrying the
`bytes_up` / `bytes_down` tally for a forwarded connection. A one-line human
summary is also echoed to stdout.

> **Log-fidelity caveat (allowlist building):** in observe mode the logged
> `host` is the **raw CONNECT target as the client sent it (pre-IDNA)**. A
> unicode-dot / IDN host is logged **verbatim** while the OS resolver may
> normalize it to a different ASCII (punycode) name — so two log entries that
> look different can resolve to the same place, and vice-versa. When building
> the allowlist **from the log, treat unfamiliar or non-ASCII host entries with
> suspicion**. (In enforce mode such hosts are blocked anyway unless an
> allowlist entry matches the raw token.)

---

## Tests

```sh
python -m unittest test_egress_proxy.py -v
```

The three pure functions (`parse_connect_target`, `host_allowed`, `decide`)
are the security boundary and are tested TDD-first. Notably, flipping
`host_allowed` to a naive `host.endswith(entry)` (dropping the `"."` boundary)
makes the suffix-bypass test FAIL — e.g. `evilexample.com` would wrongly match
the allowlist entry `example.com`.

---

## Security properties

- **Input parsing rejects injection / oversize:** the CONNECT request line is
  validated by `parse_connect_target` — CRLF / control chars in the host are
  rejected (request-smuggling guard), ports must be `1..65535`, hosts capped at
  255 chars, non-CONNECT verbs and garbage rejected. Header reads are capped at
  16 KB with a timeout (slowloris / oversize guard).
- **Allowlist is dot-boundary + fail-closed:** subdomains match, sibling-suffix
  attacks do not; an empty allowlist in enforce mode blocks everything.
- **Bind is localhost-only:** `127.0.0.1`, never `0.0.0.0` — not an open relay.
- **Bounded concurrency:** thread-per-connection capped by a semaphore
  (default 200); excess connections get `503` instead of exhausting the host.
  Socket timeouts on header read, upstream connect, and idle tunnel; sockets
  always closed in `finally`.

---

## HONEST LIMITS

- This governs the **normal `requests` / proxy-respecting path** and logs it.
  It is **NOT airtight** against a *fully-compromised* agent that opens **raw
  sockets** and ignores the `HTTP(S)_PROXY` env vars — that bypasses the proxy
  entirely. Closing that hole requires **OS-level default-deny egress**
  (host firewall rule allowing outbound only to `127.0.0.1:8888`, or a network
  namespace / container with no other route). This proxy is the *control point*;
  the OS firewall is what *forces traffic through it*. Use both for real
  containment.
- **Plain HTTP forwarding is best-effort:** absolute-form requests
  (`GET http://host/path`) are **gated and logged exactly like CONNECT** — a
  non-allowlisted host in enforce mode gets a `403` + a `decision:"block"` log
  line and is **never relayed upstream** — and an allowed request is one-shot
  relayed with `Connection: close`. The *relay* is best-effort: no keep-alive,
  no chunked-request-body streaming beyond the first read, no HTTP/2. The
  **HTTPS / CONNECT path is the fully-correct one**; agents almost always use
  HTTPS.
- TLS is **tunneled, not terminated** — the proxy sees the destination
  `host:port` (which is all it needs to log/gate) but never the plaintext. It
  is an egress *gate*, not a MITM inspector.
