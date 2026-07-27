# CLAUDE.md

Guidance for AI assistants working in this repository.

## What this project is

`agent-egress-proxy` is a single-file, **stdlib-only** localhost HTTP/HTTPS
(CONNECT) forward proxy that acts as a **network-layer egress control for AI
agents**. Point an agent's `HTTP_PROXY` / `HTTPS_PROXY` at it and you get a
complete audit log of every destination it reaches (`observe` mode) plus the
ability to block anything not on an allowlist (`enforce` mode).

It is security-critical code. Treat every change to the parsing / allowlist /
decision logic as a security change.

## Repository layout

Flat, four files. There is no package, no build system, no CI config.

| File | Role |
|---|---|
| `egress_proxy.py` | The entire implementation (~690 lines): pure security functions, `EgressProxy` server, CLI. |
| `test_egress_proxy.py` | Full test suite (51 tests): pure-function unit tests + real-socket integration tests. |
| `allowlist.txt.example` | Template allowlist; copy to `allowlist.txt` and edit. Note `allowlist.txt` is **not** in `.gitignore` — don't commit one containing real hostnames. |
| `README.md` | User-facing docs: quick start, CLI table, log format, security properties, honest limits. |

`egress.log`, `*.log`, `__pycache__/`, `.env*` are gitignored.

## Commands

```sh
# Run the tests (the only check that exists — always run before committing)
python -m unittest test_egress_proxy.py -v

# Observe mode: log every destination, block nothing
python egress_proxy.py --mode observe --port 8888

# Enforce mode: block anything not on the allowlist
python egress_proxy.py --mode enforce --allowlist allowlist.txt --port 8888
```

No dependencies to install. Python 3.8+ (the code uses `from __future__ import
annotations` deliberately so it stays 3.8-compatible — do not introduce 3.9+ only
syntax such as builtin generics in annotations, `match`, or `X | Y` unions at
runtime).

CLI flags all have env fallbacks: `EGRESS_PROXY_PORT`, `EGRESS_PROXY_MODE`,
`EGRESS_PROXY_ALLOWLIST`, `EGRESS_PROXY_LOG`.

## Architecture

`egress_proxy.py` is deliberately organized in three banded sections, in this
order. Keep new code in the band where it belongs.

### 1. Security-boundary pure functions

These three functions **are** the security boundary. They are pure (no I/O, no
sockets) precisely so they can be exhaustively unit-tested:

- `parse_connect_target(request_line) -> (host, port) | None` — parses
  `CONNECT host:port HTTP/1.1`. Rejects: non-CONNECT verbs, CRLF/control chars
  in the host (request-smuggling guard), non-digit or out-of-range ports,
  empty hosts, hosts > `MAX_HOST_LEN` (255), and bare unbracketed IPv6.
- `host_allowed(host, allowlist) -> bool` — **dot-boundary** match:
  `host == entry or host.endswith("." + entry)`, case-folded, trailing dot
  stripped. Empty allowlist → `False` (fail-closed).
- `decide(host, mode, allowlist) -> "forward" | "block"` — `observe` always
  forwards; **every other mode value, including unknown/`None`, is treated as
  enforce (fail-closed)**.

`load_allowlist(path)` sits just below: one host per line, `#` comments only at
column 0, blank lines skipped, case-folded, trailing dots stripped; a missing or
unreadable file returns an empty set (which means block-all in enforce mode).

### 2. `EgressProxy` server

- `serve_forever()` — binds **`127.0.0.1` only** (never `0.0.0.0`), 1s accept
  timeout so Ctrl-C is responsive, thread-per-connection gated by a
  `threading.Semaphore(MAX_CONCURRENT)`; over the cap clients get `503`.
  When constructed with `port=0` it writes the OS-assigned port back to
  `self.port` (tests rely on this).
- `_handle()` → `_read_headers()` (capped at `MAX_HEADER_BYTES`, 16 KB, with
  `HEADER_READ_TIMEOUT`) → dispatch to `_handle_connect()` or `_handle_plain()`.
- `_handle_connect()` — the fully-correct path. Parse → `decide` → on block send
  `403` and return **without ever calling `create_connection`** → on forward,
  connect upstream, send `200 Connection Established`, then `_tunnel()`.
- `_tunnel()` — non-blocking bidirectional `select()` relay with
  `IDLE_TUNNEL_TIMEOUT`; returns `(bytes_up, bytes_down)`; always closes
  upstream in `finally`.
- `_handle_plain()` / `_relay_plain()` — best-effort absolute-form plain HTTP
  (`GET http://host/path`). It is **gated and logged exactly like CONNECT**, but
  the relay itself is one-shot: `Connection: close`, no keep-alive, no chunked
  request-body streaming past the first read, no HTTP/2.
- `_log()` — writes one JSONL object per event under `self._log_lock` and echoes
  a human one-liner to stdout.

### 3. CLI

`main(argv=None)` — argparse with env fallbacks, loads the allowlist, pins
`host="127.0.0.1"`, runs `serve_forever()`, handles `KeyboardInterrupt`.

## Invariants — do not break these

These are the properties the tests exist to defend. Any change that weakens one
is a regression even if the suite still passes:

1. **Bind is localhost-only.** `127.0.0.1`, never `0.0.0.0` or `""`. This is not
   an open relay. (`test_W3_binds_localhost_only`)
2. **Fail-closed everywhere.** Empty/missing allowlist in enforce mode blocks
   everything. Unknown mode strings behave as enforce.
3. **A blocked host is never reached upstream.** The block branch must return
   before `socket.create_connection`. Integration tests use a `_Sentinel`
   loopback listener and assert **zero** connections. (`test_W1_...`, `test_W4_...`)
4. **No silent egress.** The destination is logged the *instant* forwarding is
   committed — before any bytes flow — and the byte tally is written in a
   `finally`. An exception inside `_tunnel` / `_relay_plain` must never suppress
   the destination log line. (`TestNoSilentEgress`)
5. **Every accepted connection produces at least one log line**, including
   rejected attempts (`reject-parse`, `reject-oversize`) which egress nothing.
   The observe log must be a *complete attempt record*, because that's what the
   allowlist is built from.
6. **Dot-boundary matching.** Never simplify `host_allowed` to a bare
   `host.endswith(entry)` — that lets `evilexample.com` match `example.com`.
7. **Stdlib only.** No pip dependencies, ever. No `requests`, no `aiohttp`.

## Log format

JSONL, one object per line, keys: `ts`, `client`, `method`, `host`, `port`,
`decision`, `bytes_up`, `bytes_down`.

`decision` values: `observe-forward`, `allow`, `block`, `reject-parse`,
`reject-oversize`. A `-close` suffix (e.g. `allow-close`) is the teardown record
carrying the byte tally. Only `observe-forward` / `allow` actually egressed.

If you add a decision value, update the README table and the tests together.

Known caveat worth preserving in docs: in observe mode the logged `host` is the
**raw CONNECT target as sent (pre-IDNA)**, so unicode/IDN hosts appear verbatim
while the resolver may normalize them differently.

## Testing conventions

Tests are written **TDD-first** and each test class carries a "Mutation notes"
docstring naming the specific mutation the tests kill (e.g. "drop the CRLF check
→ the injection test FAILS"). **Follow this convention** — when you add a test,
state what breaking change it catches.

Two tiers:

- **Pure-function tests** (`TestParseConnectTarget`, `TestHostAllowed`,
  `TestDecide`, `TestLoadAllowlist`) — fast, no sockets.
- **Wiring / integration tests** (`TestNoSilentEgress`, `TestServerWiring`) —
  boot the real `EgressProxy.serve_forever()` on an ephemeral `127.0.0.1` port
  and drive real client sockets through it. These cover the socket-layer wiring
  that pure-function tests would leave green. Helper `_Sentinel` is a throwaway
  loopback upstream that counts connections — that's how "never reached
  upstream" is proven rather than assumed.

Test names in `TestServerWiring` are prefixed `W1`, `W1b`, `W2`, `W3`, `W4` —
keep the scheme when adding wiring tests.

Gotchas when editing tests:

- Integration tests use `self._settle()` (a 0.4s sleep) to let daemon threads
  finish logging before asserting on the log file. Keep it if you add async
  assertions.
- `TestNoSilentEgress` monkeypatches `EgressProxy._tunnel` and **must restore it
  wrapped in `staticmethod(...)`** — restoring the bare function turns `_tunnel`
  into an instance method and silently poisons every later test.
- Tests print proxy startup lines to stdout; that's expected noise, not failure.

## Style conventions

- Python stdlib, `from __future__ import annotations`, `%`-style string
  formatting throughout (matches existing code — don't switch to f-strings).
- Heavy explanatory comments on security decisions, including *why* a check
  exists and what attack it stops. Match that density; a bare check with no
  rationale is out of place here.
- `# ===` banner comments separate the three major sections; `# ---` separates
  subsections. Keep new code inside the right band.
- Broad `except OSError` around every socket teardown, sockets always closed in
  `finally`.

## Documentation duties

`README.md` documents user-facing behavior in detail (CLI table, allowlist
semantics table, log-decision table, security properties, HONEST LIMITS). It is
part of the deliverable, not an afterthought — **any change to flags, decision
values, matching semantics, or limits must land in the README in the same
commit.**

The "HONEST LIMITS" section is intentional and must stay honest: this proxy only
governs processes that honor the proxy env vars. A fully-compromised agent
opening raw sockets bypasses it entirely; real containment needs OS-level
default-deny egress (host firewall / network namespace) to *force* traffic
through this control point. Do not soften that claim.

## Git workflow

- Default branch: `main`.
- Work on the branch you were assigned; commit with clear, descriptive messages
  (the existing history uses Conventional Commits, e.g.
  `feat: localhost egress-control proxy for AI agents`).
- Run `python -m unittest test_egress_proxy.py -v` before every commit — it is
  the only gate in this repo.
- Never commit `egress.log`, a real `allowlist.txt`, or anything with real
  hostnames from a live run.
