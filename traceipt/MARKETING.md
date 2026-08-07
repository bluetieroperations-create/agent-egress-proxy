# Traceipt — marketing messaging & honesty guardrails

The messaging we can stand behind, and the claims we must NOT make. The point of
this file is to keep the pitch grounded in what's *demonstrably true* (the proof,
the mechanism, the real buyer) and out of the overclaim traps (fabricated demand,
"regulation requires this"). Market the proof, not the hype.

---

## Positioning line

> **An invoice is a claim. A Traceipt is proof.**

## One-sentence value prop

> Traceipt turns an AI agent's payment into a signed, on-chain-verified receipt
> that anyone can check — and no one can forge, alter, or backdate — without
> trusting the issuer.

## The three proof-points (hero)

- **Proves it happened** — the payment is verified on-chain *before* it's ever
  signed, not just asserted.
- **Anyone can check it** — offline, forever, even if we vanish. No "call us to
  confirm."
- **Can't be faked** — change one digit and the seal breaks (it's a live demo, not
  a slogan).

## What it does that an invoice can't

| | Invoice / PDF | Traceipt receipt |
|---|---|---|
| Proves the money actually moved | asserts it | verified on-chain before signing |
| Third party can verify without trusting the issuer | call them and hope | anyone, offline, forever |
| Can't be forged, altered, or backdated | trivial to fake | signed + hash-chained + anchored |
| Machine-native (issued/read by bots at fractions of a cent) | human artifact | built for agents |

The compression: **an invoice says "trust me, you paid." Traceipt proves it — to
someone who trusts no one.**

## Who it's for (the honest target)

**Not the agent.** Agents don't care about nice receipts. The buyer is whoever is
**accountable** for what the agent spends:

- companies running fleets of spending agents that need an audit trail;
- regulated entities that must keep records;
- anyone in a **dispute** — "prove you paid" / "prove you didn't";
- services (like Black_Wall) that need verified outcomes as trustworthy data.

They buy in exactly one situation: **when being trusted isn't enough and they have
to prove it.**

---

## Honesty guardrails — what we must NOT say

The tech is real and demonstrable; the traction is not there yet. Do not paper over
that gap.

- ❌ **No named customers / "trusted by" / logos** we don't have.
- ❌ **No "validated demand" / "everyone needs this."** Demand is a *hypothesis*,
  not a proven fact — there are no external paying customers yet. The one designed
  user (Black_Wall) is us paying ourselves = proof-of-concept, not validation.
- ❌ **No "regulation requires this"** (see the next section — under scrutiny, it
  doesn't yet).
- ✅ Market the **proof and the mechanism** (100% real, live-demonstrable) and the
  **accountable buyer**. Everything else waits for real traction.

---

## Regulatory positioning — what we can and can't say

There is a genuine, worldwide *direction of travel* toward provable records of what
autonomous systems do and spend. But examined closely, **no regulation today
specifically requires cryptographically-verifiable receipts for autonomous agent
micropayments.** Traceipt is *ahead* of specific regulation, not compelled by it.
Treat regulation as a **directional tailwind, never a current mandate.**

### The landscape (mid-2026)

**AI-specific record-keeping laws** — early; only two are in force.
- 🇪🇺 **EU AI Act** (Art. 12/19): automatic logging + traceability for high-risk AI.
  In force, phasing in.
- 🇰🇷 **South Korea Basic AI Act**: documentation / risk-assessment duties; in force
  **Jan 22, 2026**.
- 🇧🇷 Brazil (PL 2338) — passed Senate, **not yet law**. 🇨🇦 Canada (AIDA) — **died**
  (Bill C-27, Jan 2025). 🇺🇸 US — no federal AI act; NIST AI RMF is voluntary.
- These govern *AI-system logging*, not payment receipts. Traceipt is *relevant*
  (an agent's payments are part of its operational record) but unnamed.

**Crypto / financial record-keeping** — global and enforced, but scoped away from us.
- **FATF "Travel Rule" (Rec. 16)**: payer/payee info must travel with a transfer and
  be retained/retrievable for auditors. Adopted widely.
- 🇪🇺 **MiCA + Transfer of Funds Regulation**: final CASP deadline **July 1, 2026**;
  TFR = **zero threshold**. 🇺🇸 **FinCEN/BSA**: records above **$3,000** + OFAC.
  🇬🇧 FCA, 🇸🇬 MAS, 🇭🇰/🇯🇵/🇦🇪 licensing — all carry retained-record duties.
- **The catch:** these target *regulated intermediaries (VASPs/CASPs) above dollar
  thresholds*. Cent-level, agent-to-API, self-hosted-wallet payments mostly fall
  **below thresholds and outside the intermediary net** — so the Travel Rule does
  not cleanly mandate Traceipt for its core use case.

**US market structure — CLARITY Act (H.R.3633)** — passed House, advanced by
Senate Banking (May 2026), **not law** (stalled, delegated rulemaking, uncertain
path). Two things, both *favorable and non-obligating for us*:
- It **explicitly exempts software developers / wallet / front-end providers**
  from intermediary registration. Traceipt is that exempt tool category — **zero
  compliance burden on us.**
- It would require **intermediaries** to run pre-trade risk programs
  (AML / **sanctions** / fraud) with recordkeeping, and directs the SEC to
  **allow blockchain for books-and-records** — a *customer-side* need for exactly
  the sanctions-screening evidence Traceipt produces, pointed at exactly the
  on-chain-anchored form we already use.
- ❗ Same catch, sharper: not law; and whether cent-level USDC agent payments are
  "trading activity through a DeFi trading protocol" is **unresolved** — they may
  fall outside CLARITY's framework entirely.

**General corporate audit** — SOX, tax, GAAP, SEC/FINRA WORM records: global,
old, boring, universal. "An agent spent our money — prove it for the audit" lands
here most directly, though a spreadsheet satisfies most auditors today.

### Can-say / can't-say

- ✅ **CAN:** "As agents begin spending real money, the records will have to be
  provable — and every major regime (EU AI Act, Korea, FATF/MiCA, corporate audit)
  is moving that way." (directional, true)
- ✅ **CAN:** "Traceipt gives you an audit-ready, independently-verifiable trail
  for machine payments — ahead of where the rules are heading."
- ❌ **CAN'T:** "The EU AI Act / MiCA *requires* signed receipts for agent
  payments." (they don't — intermediary/threshold-scoped or AI-system-scoped)
- ❌ **CAN'T:** cite a specific article as *mandating* Traceipt. Cite them as
  *context/tailwind* only.
- ✅ **CAN:** "If US market structure (CLARITY) passes, we're the *exempt* tool
  that produces the sanctions-screening record an intermediary's risk program
  needs — and it steers recordkeeping toward the on-chain form we already use."
- ❌ **CAN'T:** "CLARITY requires Traceipt." (not law; delegated; scope-uncertain;
  and it *exempts* software tools like us rather than mandating them)

### The risk this names

The demand driver is **timing**: most agent spending today is tiny and experimental,
where nobody bothers to prove anything, and the AI record-keeping laws are nascent
(two countries). Traceipt's regulatory thesis is *directionally* sound but *early* —
lead with proof/verifiability value, let regulation be the tailwind.

---

## The wedge: consume to prove (not ingest to compete)

We will never out-analyze Chainalysis or TRM, and we do not try. The defensible
role is **neutral notary**, not risk vendor: we *consume* the free, public
sanctions-screening APIs those vendors publish (which are designed to be called),
blend them into one canonical verdict, and then **anchor and bind** that verdict
into a compliance-bound receipt.

That produces something a risk vendor structurally cannot offer about its own
output without a conflict of interest: **independent, tamper-evident proof that a
specific policy check ran before a machine payment settled.** The more engines a
verdict can cite (Chainalysis, TRM, Black_Wall's on-chain heuristics), the more
neutral and credible the anchor — so consuming competitors is a *strength* here,
not a compromise.

Two honesty properties make the claim real, not marketing:

- **The decision is re-derivable, not trusted.** `verify_verdict` recomputes
  GO / STOP / REVIEW from the cited screens, so a mislabeled verdict is caught.
- **A failed screen never clears an address.** A provider that could not run
  (no key, error, rate-limited) is recorded as `checked: false` and downgrades
  the verdict to REVIEW — it can never masquerade as a clean result.

Runnable proof (real Ed25519 + real Merkle proof, no key or network required —
the offline fixture flags Chainalysis's documentation sample):

```
python tools/screen_and_anchor.py
```

It screens an address, builds the verdict, anchors it (Merkle root + offline-
verifiable inclusion proof), binds it into a receipt, and shows that swapping the
bound verdict for any other breaks verification. Set `CHAINALYSIS_API_KEY` /
`TRM_API_KEY` to fold in the real feeds — the verdict shape and everything
downstream is identical.

**Honesty guardrail:** the offline fixture's listed address is a Chainalysis
*documentation sample*, not a claim about any live wallet. Say "we make a risk
engine's answer provable," never "we screen better than Chainalysis."

---

*Sources for the regulatory section:*
[OneTrust — AI regulation 2026](https://www.onetrust.com/blog/where-ai-regulation-is-heading-in-2026-a-global-outlook/) ·
[Nemko — South Korea AI](https://digital.nemko.com/regulations/ai-regulation-in-south-korea) ·
[Sumsub — AI laws worldwide](https://sumsub.com/blog/comprehensive-guide-to-ai-laws-and-regulations-worldwide/) ·
[Sumsub — FATF Travel Rule](https://sumsub.com/blog/what-is-the-fatf-travel-rule/) ·
[Global Law Experts — MiCA deadline](https://globallawexperts.com/mica-compliance-deadline-crypto-businesses-serving/) ·
[AMLBot — crypto AML 2026](https://blog.amlbot.com/aml-crypto-regulations-compliance-guide-for-businesses/)
