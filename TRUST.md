# BlackWall — Trust & Security

*Vendor: BlueTier Operations · Product: BlackWall agent egress proxy · Last updated: 2026-07-09*

BlackWall is a **network-layer egress control for AI agents**. It is a small,
localhost-only forward proxy that audit-logs every destination an agent reaches
and, in enforce mode, blocks anything not on an allowlist. This page describes —
in plain terms and with honest limits — what BlackWall does with your data, how
it is built, and why security teams can trust running it.

Everything on this page is verifiable against the source in this repository. We
link claims to the code so you don't have to take our word for it.

---

## The one thing to understand first

**BlackWall runs on your machine, not ours.** It binds `127.0.0.1` only
([`egress_proxy.py:595-596`](egress_proxy.py)), tunnels TLS without decrypting
it, and writes its log to a local file beside the process. There is **no cloud
service, no account, no phone-home, and no BlackWall-operated infrastructure in
the data path.**

This matters for how you should evaluate us. The usual vendor question — *"can we
trust you with our data in your cloud?"* — largely doesn't apply, because we
never receive your data. The relevant questions are instead:

1. Is the tool itself secure and correct? (See **Security properties** below.)
2. Can you trust the code we ship you? (See **Supply-chain integrity**.)

---

## What data BlackWall can and cannot see

| Data | Seen by BlackWall? | Notes |
|------|--------------------|-------|
| Destination **host and port** | **Yes** | This is the whole point — it's what gets logged and gated. |
| **Byte counts** (up/down) per connection | **Yes** | Volume only, not content. |
| Local **client address** (`127.0.0.1:<port>`) | **Yes** | Identifies the calling local process's socket. |
| **Timestamp** of each attempt | **Yes** | UTC, second granularity. |
| **Plaintext of HTTPS traffic** | **No** | TLS is *tunneled, not terminated* — BlackWall relays encrypted bytes and never holds keys or plaintext ([`_tunnel`, `egress_proxy.py:363-411`](egress_proxy.py)). It is an egress **gate**, not a MITM inspector. |
| Request/response **bodies** | **No** (HTTPS) | For plain HTTP the bytes pass through a one-shot relay but are **not stored** — only host/port/byte-counts are logged. |

### Where that data lives

- The log is a local **JSONL file** (`egress.log` by default,
  [`egress_proxy.py:669-673`](egress_proxy.py)), on **your** disk, under **your**
  control. You choose the path, retention, and rotation.
- **No telemetry.** BlackWall makes no outbound connection of its own. It only
  ever connects to the upstream host an agent explicitly requested, and only
  when policy allows it.
- **No dependencies.** BlackWall is Python **standard library only**
  (`import` list at [`egress_proxy.py:29-36`](egress_proxy.py)) — nothing is
  fetched from a package registry at install or run time.

A representative log line (this is the entirety of what is recorded per event):

```json
{"ts":"2026-06-24T22:22:27Z","client":"127.0.0.1:59878","method":"CONNECT","host":"api.example.com","port":443,"decision":"allow","bytes_up":12,"bytes_down":17}
```

---

## Security properties

Each property below is enforced in code and covered by the test suite
(`test_egress_proxy.py`).

### Fail-closed by design
- **Enforce mode blocks anything not explicitly allowed**, and *any unrecognized
  mode is treated as enforce* — the decision function fails closed
  ([`decide`, `egress_proxy.py:164-175`](egress_proxy.py)).
- **An empty or missing allowlist in enforce mode blocks everything**
  ([`host_allowed`, `egress_proxy.py:146-147`](egress_proxy.py)). A
  misconfiguration degrades to *deny*, never to *allow-all*.

### Allowlist matching resists the classic suffix bypass
- Matching is **dot-boundary**, case-insensitive, and trailing-dot-normalized
  ([`host_allowed`, `egress_proxy.py:137-161`](egress_proxy.py)). `example.com`
  matches `api.example.com` but **not** `evilexample.com` or
  `example.com.attacker.com`. This is deliberately *not* a naive
  `endswith()` — the test suite fails if anyone weakens it
  (`test_suffix_bypass_rejected`).

### Input parsing rejects injection and smuggling
- The CONNECT request line is strictly parsed; **CR/LF and control characters in
  the host are rejected** (request-smuggling / header-injection guard), ports
  must be `1..65535`, hosts are capped at 255 chars, and ambiguous bare IPv6 is
  rejected ([`parse_connect_target`, `egress_proxy.py:57-134`](egress_proxy.py)).

### Denial-of-service guards
- **Header reads are capped at 16 KB with a 10s timeout** (oversize / slowloris
  guard) ([`_read_headers`, `egress_proxy.py:251-272`](egress_proxy.py)).
- **Concurrency is bounded by a semaphore (200)**; excess connections receive
  `503` instead of exhausting the host
  ([`egress_proxy.py:622-629`](egress_proxy.py)).
- **Timeouts** apply to header read, upstream connect, and idle tunnels
  ([`egress_proxy.py:41-45`](egress_proxy.py)); sockets are always closed in
  `finally` blocks.

### Not an open relay
- Bind is **`127.0.0.1` only, never `0.0.0.0`**
  ([`egress_proxy.py:595-596`](egress_proxy.py)) — only local processes can use
  the proxy.

### Complete, tamper-evident attempt record
- **Every accepted connection produces at least one log line**, and a blocked or
  rejected attempt **egresses nothing but is still logged**
  ([`_handle_connect`, `egress_proxy.py:315-361`](egress_proxy.py)). The
  destination is recorded *before* any upstream bytes flow, and the byte tally is
  written on teardown in a `finally`, so an exception mid-stream can never
  suppress a destination log line.

---

## Supply-chain integrity

Because you run our code, the honest trust question is *"how do I know this is the
code you wrote?"* Our commitments:

- **Zero third-party dependencies** — standard library only, so there is no
  transitive package supply chain to compromise.
- **Auditable in one sitting** — the entire product is a single ~690-line Python
  file plus its tests. The security boundary is three small pure functions
  (`parse_connect_target`, `host_allowed`, `decide`) that you can read and verify
  in minutes.
- **Reproducible & inspectable** — no build step, no minification, no binary
  blobs. What is in the repository is what runs.
- *(Roadmap)* Signed releases and build provenance attestation so you can verify
  a downloaded artifact matches this source.

---

## Honest limits

We would rather tell you the boundaries than have you discover them.

- **Not airtight against a fully-compromised agent.** BlackWall governs the
  normal proxy-respecting path (processes that honor `HTTP(S)_PROXY`). An agent
  that opens **raw sockets** and ignores the proxy env vars bypasses it entirely.
  Closing that hole requires **OS-level default-deny egress** (a host firewall
  rule or network namespace that only permits outbound to `127.0.0.1:<proxy>`).
  BlackWall is the *control point*; the OS firewall is what *forces traffic
  through it*. Use both for real containment.
- **Plain-HTTP forwarding is best-effort.** It is gated and logged like CONNECT,
  but the relay is one-shot (no keep-alive, no chunked-body streaming beyond the
  first read, no HTTP/2). The **HTTPS/CONNECT path is the fully-correct one**, and
  agents almost always use HTTPS.
- **TLS is tunneled, not inspected.** BlackWall sees the destination `host:port`
  (all it needs to log and gate) but never the plaintext. It is an egress gate,
  not a content inspector.
- **Log fidelity for IDN hosts.** In observe mode the logged `host` is the raw
  CONNECT target as sent (pre-IDNA). Treat unfamiliar or non-ASCII log entries
  with suspicion when building an allowlist.

---

## Reporting a vulnerability

Found a security issue? Please contact **security@blackwalltier.com** (or open a
private security advisory on the repository). We aim to acknowledge within two
business days. Please do not disclose publicly until we've had a chance to
respond.

---

## Compliance posture

BlackWall is early-stage software distributed as source. We favor **verifiable
security properties and transparency over compliance theater**, and we publish
this page and our [threat model](THREAT_MODEL.md) as our primary trust artifacts.
Formal attestations (e.g. a SOC 2 report covering our development and release
practices) will be pursued as customer requirements dictate. If your procurement
process needs a specific questionnaire (SIG, CAIQ) or framework mapping, contact
us and we'll work through it.
