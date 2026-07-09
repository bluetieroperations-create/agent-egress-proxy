# BlackWall — Compliance & Vendor Security Self-Assessment

*Vendor: BlueTier Operations · Product: BlackWall agent egress proxy · Last updated: 2026-07-09*

This document is written for **procurement and vendor-risk teams**. It gives our
current security posture, a self-assessment you can map onto a SIG/CAIQ-style
questionnaire, a light framework mapping, and our certification roadmap. It is
paired with our [Trust page](TRUST.md), [Threat model](THREAT_MODEL.md), and
[independent claims audit](AUDIT.md).

---

## 1. Posture in one paragraph

BlackWall is a **self-hosted, localhost-only** egress-control proxy. It runs
entirely on the customer's own host, has **no cloud service, no vendor-operated
infrastructure in the data path, no account system, and no telemetry**. As a
result, **BlackWall does not store, process, or transmit customer data to
BlueTier** — the classic "can we trust you with our hosted data" question largely
does not apply. The relevant questions are (a) is the tool itself secure and
correct, and (b) can you trust the code we ship. Both are addressed below and
backed by an automated, re-runnable audit.

---

## 2. Vendor security self-assessment (SIG/CAIQ-style)

> **How to use:** these answers can be pasted into most vendor questionnaires.
> "N/A — self-hosted" means the control lives with the customer's environment
> because we operate no service on their behalf.

### Data handling & privacy
| Question | Answer |
|----------|--------|
| Do you store customer data? | **No.** BlackWall runs on the customer host; its only output (a JSONL log of destinations) is written to the customer's local disk under their control. |
| Do you transmit customer data to your infrastructure? | **No.** No telemetry, no phone-home, no update check. The only outbound connection BlackWall makes is the upstream the agent requested and policy permitted. |
| What data does the product process? | Destination `host:port`, per-connection byte counts, local client socket address, timestamp. **Not** payload/plaintext. |
| Do you see the content of tunneled traffic? | **No.** TLS is tunneled, not terminated; the proxy holds no keys and never sees plaintext. |
| Data residency? | Entirely within the customer's environment. We hold nothing. |
| PII processed by vendor? | **None** — we operate no service that receives it. |
| Subprocessors? | **None** in the data path. |

### Product / application security
| Question | Answer |
|----------|--------|
| Encryption in transit | End-to-end TLS between agent and upstream is preserved and unmodified; BlackWall relays ciphertext. |
| Input validation | Strict CONNECT/HTTP parsing; CR/LF and control chars rejected (injection guard); ports bounded `1..65535`; hosts capped at 255 chars. |
| Access control model | Binds `127.0.0.1` only — only local processes can use it; it is not network-reachable and not an open relay. |
| Denial-of-service protections | 16 KB header cap + read timeout (slowloris/oversize); semaphore-bounded concurrency (200) with `503` shedding; idle-tunnel timeout. |
| Fail-safe behavior | Fail-closed: unknown mode → enforce; empty allowlist in enforce → block-all. |
| Audit logging | Every accepted connection and every rejected/blocked attempt is logged; forwarded destination recorded before bytes flow. |
| Third-party dependencies | **Zero** — Python standard library only. No package supply chain. |
| Known vulnerabilities / CVEs | None known. Disclosure process in [`SECURITY.md`](SECURITY.md). |

### Development & supply chain
| Question | Answer |
|----------|--------|
| Source availability | Full source in this repository; no binaries, no build step, no minification. |
| Change control | Changes gated by CI ([`verify`](.github/workflows/verify.yml)): unit tests + live-traffic security audit must pass. |
| Automated security testing | Yes — [`audit_claims.py`](audit_claims.py) drives the real proxy and verifies each documented security property on every push. |
| Dependency scanning | N/A — zero dependencies (nothing to scan). |
| Vulnerability disclosure policy | Yes — [`SECURITY.md`](SECURITY.md). |
| Reproducibility | What is in the repository is what runs; verifiable by inspection. |

### Organizational (to be completed by BlueTier)
> These are org-level controls that a formal attestation (e.g. SOC 2) would
> cover. Fill in as your practices mature — placeholders kept honest.
| Question | Answer |
|----------|--------|
| Access to source/build systems | *TODO: MFA/SSO on GitHub org; least-privilege; document.* |
| Employee onboarding/offboarding | *TODO: document.* |
| Endpoint security | *TODO: MDM/disk encryption on dev machines; document.* |
| Incident response plan | *TODO: document; reference SECURITY.md for product vulns.* |
| Background checks | *TODO: as applicable.* |

---

## 3. Framework mapping (indicative)

BlackWall is a control that *helps customers meet* their own obligations, and its
own posture maps cleanly onto recognized frameworks:

| Framework | Where BlackWall fits |
|-----------|----------------------|
| **NIST CSF 2.0** | *Protect (PR.DS/PR.AC)* egress restriction & least-connectivity; *Detect (DE.CM)* continuous destination monitoring/logging. |
| **NIST 800-53** | `SC-7` Boundary Protection; `AC-4` Information Flow Enforcement; `AU-2/AU-12` Audit events. |
| **CIS Controls v8** | Control 13 (Network Monitoring & Defense); Control 4 (Secure Configuration) via fail-closed defaults. |
| **OWASP** | Mitigates SSRF-style egress abuse and data-exfiltration paths for agentic apps. |
| **ISO/IEC 27001 Annex A** | `A.8.20/A.8.21` network security & segregation; `A.8.15` logging. |

*As a customer, BlackWall is evidence toward your egress-control and monitoring
requirements; deploying it with OS-level default-deny egress closes the
raw-socket gap (see threat model §6).*

---

## 4. Certification roadmap

Recommended sequence for a small, self-hosted security vendor. Cheapest,
highest-leverage first. (Ranges are ballpark for a small-headcount company;
startup-discount programs via AWS/GCP/Azure/YC often cut platform + audit costs
20–50%.)

| Phase | Deliverable | Approx. cost | When |
|-------|-------------|--------------|------|
| **0 — Now (free)** | Trust page, threat model, self-assessment, live audit, disclosure policy, CI verification | **$0** | ✅ Done in this repo |
| **0.5 — Free signals** | Publish a CSA CAIQ self-assessment to the STAR registry; keep a SIG Lite on file | **$0** | When first enterprise asks |
| **1 — Product assurance** | Independent third-party **penetration test / code audit** (most on-point for a security tool) | **~$4k–$15k** | Before/at first enterprise deal |
| **2 — Supply-chain integrity** | Signed releases + build **provenance attestation** (Sigstore / GitHub artifact attestations); publish an SBOM (trivial — stdlib only) | **~$0–$2k** | Alongside phase 1 |
| **3 — SOC 2 Type II** | AICPA attestation over dev/build/corp security (scope: **Security** criterion only). Note: covers *our* SDLC & org, not "hosted data" (we host none) | **~$15k lean / ~$30k with platform**; ~$15k–$25k/yr ongoing | When a US enterprise names it |
| **4 — ISO/IEC 27001** | ISMS certification (base management system) | **~$15k–$40k first year** | When selling internationally |
| **5 — ISO/IEC 42001** | AI Management System — the differentiator for an AI-governance product; cheapest layered on 27001 | **~$8k–$20k as 27001 add-on** | After 27001 |

**Who to talk to:**
- *Automation platforms (prep + auditor intros):* Vanta, Drata, Secureframe, Sprinto.
- *SOC 2 auditors (budget-friendly):* Johanson Group, Prescient Assurance, Insight Assurance; (full-service) A-LIGN, Schellman.
- *ISO 27001/42001 bodies:* Schellman, A-LIGN, BSI, Coalfire — do 27001+42001 in one integrated audit to save cost.

**Guiding principle:** favor **verifiable security properties and transparency**
(phases 0–2, mostly free) over compliance theater. Pursue formal attestations
(phases 3–5) *reactively*, driven by specific customer requirements, scoping each
honestly so you don't pay to attest controls that don't apply to a localhost-only
tool.

---

## 5. Requesting more

Need a specific questionnaire completed (SIG Full, CAIQ v4, a customer template),
a framework mapping, or a copy of the pen-test report once available? Contact
**security@blackwalltier.com**.
