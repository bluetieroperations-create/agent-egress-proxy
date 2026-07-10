# BlackWall — Compliance & Trust Hand-off

*Prepared: 2026-07-09 · Owner: BlueTier Operations · Contact: bluetier.operations@gmail.com*

This document hands off the trust/compliance work done on BlackWall so anyone can
pick it up without prior context. It covers **what exists, what's left, and what
to do next.**

---

## 1. Goal

Get BlackWall (BlueTier's agent egress-control proxy) into a state where it can
be **trusted and, eventually, third-party certified** — done the cost-smart way
for a small vendor: **free, verifiable trust artifacts first; paid attestations
(SOC 2 / ISO) pursued reactively when a specific customer requires one.**

Key framing that drove every decision: **BlackWall is self-hosted software that
runs on the customer's machine** (localhost-only, no cloud, no data collection).
So the usual SaaS question — "can we trust you with our hosted data?" — doesn't
apply. The trust questions that *do* apply are **"is the tool secure and correct"**
and **"can we trust the code you ship"**, and the work below answers both.

---

## 2. Current state — what exists

All work is on branch **`claude/black-wall-compliance-cert-qdzrq0`** (pushed to
origin; not yet merged, no PR opened). Latest commit: `fecad41`.

### Trust & security documentation
| File | Purpose |
|------|---------|
| `TRUST.md` | Buyer-facing trust page — what data BlackWall can/can't see, where it lives, honest limits. |
| `THREAT_MODEL.md` | Adversary model (A1–A6), 9 in-scope threats with code-linked mitigations, 5 explicit non-goals, residual-risk table. |
| `SECURITY.md` | Vulnerability-disclosure policy; in-scope/out-of-scope; response targets. |
| `COMPLIANCE.md` | Vendor security self-assessment (SIG/CAIQ-style), NIST/CIS/ISO mapping, **phased costed certification roadmap**. |

### Verification (claims are proven, not just asserted)
| File | Purpose |
|------|---------|
| `audit_claims.py` | Independent live-traffic harness — stands up the real proxy, drives raw-socket traffic, confirms each security claim end-to-end. **Run: `python audit_claims.py`** |
| `AUDIT.md` | The audit report — methodology + results table (**22/22 claims verified**). |
| `test_egress_proxy.py` | Project's own TDD suite (**51 tests**) over the security-boundary functions. *(pre-existing)* |

### Supply-chain integrity
| File | Purpose |
|------|---------|
| `scripts/gen_sbom.py` | Stdlib-only CycloneDX SBOM generator (hashes shipped source). |
| `sbom.json` | Committed SBOM (zero third-party dependencies). |
| `.github/workflows/verify.yml` | CI: runs unit tests + live audit on every push/PR (Python 3.8/3.11/3.12). |
| `.github/workflows/release.yml` | On `vX.Y.Z` tag: gate on tests+audit → SBOM + checksums → **build-provenance attestation** (keyless Sigstore) → GitHub Release. |

### Shareable collateral
- **HTML trust page (artifact):** https://claude.ai/code/artifact/b02a120d-e0f4-4aa5-8cb7-24f1ee6737cd
  Private until shared from the artifact page's share menu. Hand-built (not an
  auto-render of `TRUST.md`) — if `TRUST.md` changes, the page must be re-synced.

### Verify everything locally (no setup, zero dependencies)
```sh
python -m unittest test_egress_proxy.py -v   # 51 tests, OK
python audit_claims.py                        # 22/22 claims verified
python scripts/gen_sbom.py --version 0.1.0    # regenerate SBOM
```

---

## 3. Open items — needs a human

| # | Item | Why | Owner |
|---|------|-----|-------|
| 1 | **Confirm `bluetier.operations@gmail.com` is monitored** for security reports | It's now the disclosure address in TRUST.md, SECURITY.md, COMPLIANCE.md, and the trust page. Consider a dedicated `security@` alias later. | BlueTier |
| 2 | **Fill org-level TODOs in `COMPLIANCE.md` §2** (MFA/SSO, onboarding/offboarding, endpoint security, incident response, background checks) | These are the org controls a SOC 2 would formalize; placeholders are left honest. | BlueTier |
| 3 | **Decide on the branch**: merge via PR, or keep as-is | No PR was opened (none was required). CI runs on push already. | BlueTier |
| 4 | **Repo discoverability**: add GitHub topics + a keyword description | Not yet applied. Suggested topics and description in §5. | BlueTier |
| 5 | **Cut a first signed release** (`git tag v0.1.0 && git push --tags`) | Exercises release.yml → provenance + SBOM + checksums on a real Release. | BlueTier |

---

## 4. Certification roadmap (cheapest → most involved)

From `COMPLIANCE.md`. Ranges are ballpark for a small-headcount vendor;
startup-discount programs (AWS/GCP/Azure/YC) often cut platform+audit cost 20–50%.

| Phase | Deliverable | Cost | Status |
|-------|-------------|------|--------|
| 0 | Trust page, threat model, self-assessment, live audit, disclosure policy, CI | $0 | ✅ Done |
| 0.5 | Publish CSA CAIQ self-assessment to STAR registry; keep SIG Lite on file | $0 | ⬜ When first enterprise asks |
| 1 | **Independent pen test / code audit** (most on-point for a security tool) | ~$4k–$15k | ⬜ At first enterprise deal |
| 2 | Signed releases + provenance + SBOM | $0 | ✅ Done (release.yml) |
| 3 | **SOC 2 Type II** (Security criterion; covers *our* SDLC/org, not hosted data) | ~$15k lean / ~$30k w/ platform; ~$15k–$25k/yr | ⬜ When a US enterprise names it |
| 4 | **ISO/IEC 27001** (base ISMS) | ~$15k–$40k yr 1 | ⬜ For international deals |
| 5 | **ISO/IEC 42001** (AI management system — the differentiator) | ~$8k–$20k as 27001 add-on | ⬜ After 27001 |

**Who to contact when you start phase 1/3+:**
- *Automation platforms (prep + auditor intros):* Vanta, Drata, Secureframe, Sprinto — ask about startup discounts.
- *SOC 2 auditors (budget-friendly):* Johanson Group, Prescient Assurance, Insight Assurance; (full-service) A-LIGN, Schellman.
- *ISO 27001/42001 bodies:* Schellman, A-LIGN, BSI, Coalfire — do 27001+42001 in one integrated audit to save cost.

**Guiding principle:** don't buy a cert nobody's asking for. Lead with the free,
verifiable artifacts (done); pursue paid attestations only when a named deal
requires one, and scope each honestly for a localhost-only tool.

---

## 5. Positioning & discoverability (suggested, not yet applied)

**One-line descriptor:**
> Localhost-only egress-control proxy for AI agents — audit-logs and allowlists
> outbound traffic to stop data exfiltration from compromised or prompt-injected
> agents. Fail-closed, zero-dependency.

**Anchor identity:** *"agent egress control."*

**GitHub topics:** `ai-agents` · `agent-security` · `egress-control` ·
`egress-proxy` · `forward-proxy` · `allowlist` · `prompt-injection` ·
`data-exfiltration` · `zero-trust` · `llm-security` · `network-security` ·
`python` · `proxy` · `guardrails`

**Do NOT position as:** a WAF/inbound firewall, content-inspecting DLP (it gates
by destination, not payload), or an "AI-powered" tool (it's deterministic
plumbing *for* AI agents).

---

## 6. Key facts for whoever picks this up

- BlackWall = a single ~690-line Python file (`egress_proxy.py`), **stdlib only**,
  Python 3.8+. No build step. What's in the repo is what runs.
- The **security boundary is three pure functions**: `parse_connect_target`,
  `host_allowed`, `decide`. If you change these, the tests and audit must stay
  green — they're designed to fail if the guarantees weaken.
- **Two modes:** `observe` (log everything, block nothing) and `enforce` (block
  non-allowlisted hosts). Enforce is fail-closed; empty allowlist = block-all.
- Every claim in the trust docs is **linked to a source line** and **re-verified
  in CI** — keep it that way when editing: update the doc, the code, and the
  audit together.
