# BlackWall — Independent Security Claims Audit

*Product: BlackWall agent egress proxy (`egress_proxy.py`) · Audit date: 2026-07-09*
*Method: live-traffic verification · Harness: [`audit_claims.py`](audit_claims.py)*

This report verifies that the security properties asserted in
[`TRUST.md`](TRUST.md) and [`THREAT_MODEL.md`](THREAT_MODEL.md) are **true in
running code**, not just described in prose. It is intended as a
buyer/reviewer-facing artifact and as a repeatable regression check.

## Method

Rather than trust the product's own unit tests, the audit stands up the **real
proxy** (`EgressProxy`) on an ephemeral port, points a **raw-socket client** at
it, and drives **live traffic** through it. Forwarding claims are checked against
a **real local upstream server** — we confirm bytes actually traverse the tunnel
(or that no upstream connection is made when a request is blocked/rejected). The
resulting JSONL log is parsed to confirm the audit-trail invariants.

Reproduce:

```sh
python audit_claims.py        # exits non-zero if any claim fails
```

The project's own TDD suite is complementary and also passes:

```sh
python -m unittest test_egress_proxy.py -v   # 51 tests, OK
```

## Result

**22 / 22 claims verified under live traffic.** Every assertion below was
observed as behavior of the running process, with the driving request and the
resulting log line captured.

| # | Claim (from TRUST.md / THREAT_MODEL.md) | How it was exercised | Result |
|---|------------------------------------------|----------------------|--------|
| 1 | Allowlisted CONNECT establishes a tunnel | Real `CONNECT` to allowlisted upstream → `200 Connection Established` | ✅ |
| 2 | Real bytes traverse the tunnel (forwarding actually works) | Wrote through tunnel, upstream banner returned; upstream hit count incremented | ✅ |
| 3 | **T1** Non-allowlisted CONNECT is blocked | `CONNECT evil.com` → `403 Forbidden` | ✅ |
| 4 | **T2** Suffix-bypass blocked | `evilexample.com` vs allow `example.com` → `403` | ✅ |
| 5 | **T2** Attacker-suffix blocked | `example.com.attacker.com` → `403` | ✅ |
| 6 | **T2** Legitimate subdomain allowed | `api.example.com` passes policy (reaches upstream-connect, not `403`) | ✅ |
| 7 | **T3** Control-char / injection in host rejected | Host with `\x00` → `400` `reject-parse` | ✅ |
| 8 | **T4** Out-of-range port rejected | port `70000` → `400` | ✅ |
| 9 | **T4** Port 0 rejected | port `0` → `400` | ✅ |
| 10 | **T4** Garbage request rejected | non-CONNECT junk → `400` | ✅ |
| 11 | **T5** Oversize header rejected | 17 KB header → `400` `reject-oversize` | ✅ |
| 12 | **T5** Oversize attempt makes **no** upstream connection | upstream hit count unchanged across the oversize probe | ✅ |
| 13 | **T8** Block decisions are logged | `block` records present in log | ✅ |
| 14 | **T8** Parse rejects logged (never silent) | `reject-parse` records present | ✅ |
| 15 | **T8** Oversize rejects logged (never silent) | `reject-oversize` record present | ✅ |
| 16 | **T8** Forwarded conn logged at open **and** teardown | both `allow` and `allow-close` (byte tally) present | ✅ |
| 17 | **T8** Blocked destination recorded by name | `evil.com`, `evilexample.com`, `example.com.attacker.com` all in log | ✅ |
| 18 | Fail-closed: enforce + empty allowlist blocks all | empty allowlist → `403` to a would-be-reachable host | ✅ |
| 19 | Observe mode forwards regardless of allowlist | observe + empty allowlist → `200` (non-gating) | ✅ |
| 20 | Observe forwards are logged (`observe-forward`) | `observe-forward` + `-close` records present | ✅ |
| 21 | **T7** Live listener bound to `127.0.0.1` (not `0.0.0.0`) | inspected the bound socket's address | ✅ |
| 22 | Supply chain: zero third-party imports | parsed every `import` — all stdlib | ✅ |

### Notable positive observations

- **No silent egress, confirmed dynamically.** For every blocked or rejected
  probe, the upstream server's connection counter did **not** increase — the
  proxy provably never dialed out — yet a log line was still written each time.
- **Byte-accurate teardown record.** The allowlisted tunnel logged `allow` at
  open (`up=0 down=0`) then `allow-close` with the real tally (`up=20 down=11`)
  after 20 bytes in / 11 bytes back — the "record destination before bytes, tally
  on teardown" invariant, observed live.
- **Fail-closed is real, not just documented.** Enforce mode with an empty
  allowlist printed its loud warning and returned `403` even to a host that was
  actually reachable.

## Scope and honesty

This audit verifies the **in-scope** security properties. It does **not**
contradict the documented non-goals in [`THREAT_MODEL.md` §6](THREAT_MODEL.md) —
those remain the operator's responsibility and were not "tested away":

- **Raw-socket bypass (N1):** out of scope by design; requires OS-level
  default-deny egress. Not exercised here because it is not a proxy-layer
  property.
- **Local privileged tampering (N2):** out of scope; the audit assumes an intact
  host and log file.
- **Content DLP (N3):** by design, not attempted — BlackWall gates by
  destination, not payload.

## Conclusion

Every security property BlackWall claims to a customer is **demonstrably true in
the running code**, and the claims that matter most for an egress control —
fail-closed gating, dot-boundary allowlisting, injection/oversize rejection, and
a complete no-silent-egress audit trail — were confirmed by observing the proxy's
actual network behavior and log output, not merely by reading its source.

The harness (`audit_claims.py`) is committed alongside this report so the audit
can be re-run on every change and included in CI.
