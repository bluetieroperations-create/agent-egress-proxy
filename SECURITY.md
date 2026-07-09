# Security Policy

BlackWall is a security control, so we hold its own security to a high bar and
welcome reports. This policy explains how to report a vulnerability and what to
expect.

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** — do not open a public
issue for a security bug.

- **Email:** bluetier.operations@gmail.com
- **Or:** open a private [GitHub Security Advisory](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
  on this repository ("Security" tab → "Report a vulnerability").

Please include: a description, affected version/commit, reproduction steps or a
proof of concept, and the impact you observed.

### Our commitment

| Stage | Target |
|-------|--------|
| Acknowledge receipt | within **2 business days** |
| Initial assessment / severity | within **5 business days** |
| Fix or mitigation plan | communicated after assessment |
| Coordinated disclosure | by mutual agreement, typically after a fix ships |

We will credit reporters who wish to be named. Please give us a reasonable
opportunity to remediate before any public disclosure.

## Scope

BlackWall's trust boundary is defined in [`THREAT_MODEL.md`](THREAT_MODEL.md).
Reports are most valuable when they concern **in-scope** properties.

### In scope

- Bypass of the enforce-mode allowlist by a proxy-respecting client (e.g. a
  host-matching flaw, a parser gap that lets a blocked destination through).
- Request-smuggling / header-injection through the CONNECT or plain-HTTP path.
- A path that **egresses to an upstream without producing a log line**
  (violation of the no-silent-egress invariant).
- Denial of service against the host reachable through the proxy interface
  (resource exhaustion not bounded by the documented guards).
- The proxy binding a non-loopback interface, or otherwise acting as an open
  relay.
- Plaintext exposure by the proxy of traffic it tunnels.

### Out of scope (documented non-goals)

These are stated limitations in [`THREAT_MODEL.md` §6](THREAT_MODEL.md), not
vulnerabilities:

- A **fully-compromised agent opening raw sockets** that ignore `HTTP(S)_PROXY`.
  Containment of that requires OS-level default-deny egress (host firewall /
  network namespace) — BlackWall is the control point, not the enforcement of
  last resort.
- A **local privileged (root/admin) attacker** reading the log, editing the
  allowlist, or killing the process.
- Absence of **content inspection / DLP** — BlackWall gates by destination, not
  payload, by design.
- Best-effort edge cases in **plain-HTTP** forwarding (no keep-alive / chunked
  request streaming / HTTP/2). Prefer HTTPS/CONNECT, which is the fully-correct
  path.

If you're unsure whether something is in scope, report it anyway and we'll
assess.

## Supported versions

BlackWall is distributed as source from this repository. Security fixes land on
the default branch; we recommend tracking it. Every change is gated by the
[`verify`](.github/workflows/verify.yml) CI job (unit tests + live-traffic audit)
before merge.

## Verifying the build

BlackWall has **zero third-party dependencies** (Python standard library only)
and no build step — what is in the repository is what runs. You can audit the
entire product (a single ~690-line file plus tests) yourself, and re-run our
claims audit at any time:

```sh
python -m unittest test_egress_proxy.py -v   # security-boundary unit tests
python audit_claims.py                        # live-traffic claims audit
```
