# Talking points — the hard questions + a 2-minute demo

For when someone in the x402 Slack (or a call) engages. The rule from
`OUTREACH.md` still holds: **the goal is to learn whether the pain is real, not
to close.** Answer straight, stay curious, and turn every answer into a question
back. Lead with the honest weakness when there is one — this crowd smells spin.

---

## The hard questions (honest, defensible answers)

**"Who's using this? Who's paying?"**
> Nobody yet — it's days old. I have a live, mainnet-verifiable implementation and
> I'm here to find out if this is a real need before I build more. That's exactly
> why I'm asking you: does this problem resonate, or am I early?

*(Never fake traction. "Pre-revenue, validating demand" is respected; a made-up
logo is fatal.)*

**"Why anchor on-chain instead of hash-linked retention (à la draft-hopley)?"**
> Both give tamper-evidence. On-chain gives trustless *existence-by-time*: you can
> prove a verdict existed at a block without trusting the issuer's storage or that
> they didn't backdate a retention log. Hopley's S3 Object-Lock is great for
> retention but you still trust the retainer. Different trust models — and they
> compose (you can retain *and* anchor). Cost is gas per anchor, which batching
> amortizes.

**"How is this different from Vauban (STARK / Starknet)?"**
> Vauban is a crypto-maximalist receipt *primitive* — STARK proofs, post-quantum,
> on Starknet — and it deliberately keeps compliance *out* of the receipt.
> Traceipt is compliance-*first* (it binds the sanctions/policy verdict), on Base
> (where the x402/USDC volume is), with simpler Merkle+RPC proofs, and it's live on
> mainnet today. Vauban is more formalized (IETF drafts); we're shipped and
> compliance-bound. Genuinely different bets — I'd like to understand where they
> converge.

**"Isn't the facilitator's own log (CDP, etc.) enough?"**
> For the facilitator, sure. But a regulated buyer often needs a proof *they hold*,
> portable and verifiable without trusting the facilitator — or even trusting me.
> That's the whole point: verify against the chain + a published key, no vendor in
> the trust path. The moment a platform owns the proof, it isn't neutral anymore.

**"What stops someone forging a receipt?"**
> Three independent locks: an Ed25519 signature over the canonical envelope (no key,
> no forgery), a Merkle inclusion proof (can't fake membership), and the root
> published on-chain (can't backdate). For payment receipts, settlement is verified
> on-chain (RPC + a Blockscout cross-check) *before* signing — the receipt only
> exists if the money actually moved, to the right address, for the right amount.

**"Is the sanctions screening real? Do you have Chainalysis?"**
> Screening is consumer-side — Traceipt binds and anchors whatever verdict a real
> provider produces; it supports live Chainalysis/TRM feeds via API keys. The
> public demos use an offline fixture seeded with real OFAC addresses so anyone can
> reproduce them. **Traceipt is not trying to be the sanctions oracle** — it's the
> neutral *proof* that a screen happened and what it decided. That boundary is
> deliberate.

**"What's the business model?"**
> The $0.01 micropayment per receipt is frictionless onboarding, not the revenue
> engine — micropayments don't scale to real money. Revenue is B2B: ~$500–5k/mo per
> company for the API + retention + audit exports + SLA. Pre-revenue today; I'd
> rather validate the pain than pitch a price.

**"Nobody's asking my agents to prove screening yet — why would I need this?"**
> Honestly, today it's preventive, not a live fire — "before your regulator or
> enterprise customer asks," tied to EU AI Act Art. 12 / MiCA recordkeeping. If
> it's not a real pain for you yet, that's a completely valid answer and useful for
> me to hear. Is it on your radar at all, or not close?

**"Isn't this just Certificate Transparency / Sigstore for payments?"**
> Same *shape* — a Merkle log with inclusion proofs — deliberately, applied to
> compliance verdicts on agent payments. Boring, standards-based, and verifiable is
> the point. If it feels familiar, good.

**"Why post-quantum now?"**
> Receipts are 5–10-year audit artifacts. "Harvest now, forge later": one signed
> today must resist a quantum attacker in 2035. The Merkle anchor is already
> hash-based (quantum-sound); the Ed25519 signature isn't, so I add an ML-DSA-65
> signature as a hybrid hedge. Not urgent — cheap, and it matches the retention
> promise.

**"Can I self-host? Is it open?"**
> Yes — MIT-licensed and self-hostable, which matters for regulated buyers who
> can't put this on someone else's SaaS. Verification needs nothing from me at all.

**"Only Base?"**
> Base mainnet + Sepolia today (where the x402/USDC volume is). The mechanics —
> Merkle root in tx calldata, EIP-3009 settlement — generalize to any EVM chain;
> it's not chain-locked.

**The question to ask back (always):**
> "When *you* think about proving an agent payment was screened/authorized —
> is that a real problem you or your customers hit, or not yet? What would you
> actually need to hand an auditor?"

---

## The 2-minute demo (Loom script)

Screen-record `traceipt.xyz/verify`. Keep it tight; the whole point is *they can
do it themselves*.

- **0:00–0:15 — Hook.** "When an auditor asks you to prove an agent payment was
  sanctions-screened *before* it settled — independently, not from a vendor's
  dashboard — what do you hand them? Here's an answer you can check yourself, in
  your browser."
- **0:15–0:45 — The STOP.** Open the page. "This runs client-side — it calls my
  server zero times." Click **"Load the OFAC Tornado Cash STOP."** Point at the
  JSON: "A real payment where the screened address is the OFAC-sanctioned Tornado
  Cash contract — verdict: STOP."
- **0:45–1:15 — Verify it.** Click **Verify**. Walk the three green checks:
  "*verdict binding* — the STOP verdict hashes to the anchored leaf; *inclusion
  proof* — the Merkle path recomputes the root; *on-chain anchor* — that root is
  in a Base **mainnet** transaction's calldata. All checked against Base, in the
  browser."
- **1:15–1:40 — The clean case.** Click **"Load the live mainnet example"** →
  **Verify** → the GO. "Same verification on a clean payment."
- **1:40–2:00 — Close + ask.** "Verdict bound to the paid tx, Merkle root on-chain,
  Ed25519 + a post-quantum signature, verifiable offline for years — no vendor in
  the trust path. I'm looking for people who feel the 'prove it was screened' pain.
  If that's you, I'd love 15 minutes."

**Recording tips:** no intro fluff, start on the page. Actually click and let the
checks turn green on camera — the live verification *is* the pitch. Post it as a
reply if the thread gets traction, or send it 1:1 when someone says "tell me more."
