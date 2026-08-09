# Blackwall — launch post (ready to ship)

**Verified-claims-only.** Everything here is true and checkable as of this writing.
Do NOT add adoption/usage numbers (there are none yet) — lead with the capability
proof. Re-fetch the live endpoint before posting.

## The proof points (all verifiable)
- Live on **Base mainnet**: `https://agent-egress-proxy.onrender.com`
- Real x402 settlement on Base: tx `0x9ddec827b762303c6f1f351530239f52901c86bb19df41ea6a02e8d276be9fd7`
- **Reproducible STOP** — anyone can POST an overpriced amount and watch it return STOP
- Live **OFAC screening** (`screening: ["sanctions-ofac"]` in the discovery descriptor)
- Deterministic, stdlib-only, **368 tests**

---

## Option A — single hook tweet
> Your AI agent is about to sign a payment. Should it?
>
> Blackwall returns **GO / HOLD / STOP** before the signature — on-chain
> counterparty reputation, price-anomaly, OFAC screening. Verdict-only, never
> custody.
>
> Live on Base. 🛡️ agent-egress-proxy.onrender.com

## Option B — the thread (recommended)

**1/**
> Agents are starting to pay for things autonomously over x402 — signing
> stablecoin payments in milliseconds, no human in the loop.
>
> I built the guardrail that goes in front of the signature.
>
> Blackwall: GO / HOLD / STOP before your agent signs. 🧵

**2/**
> It folds three checks into one pre-signature verdict:
> • behavioral counterparty reputation (chain-confirmed settlement/dispute history)
> • price-anomaly (is this amount off vs the counterparty's own history — and vs the market)
> • OFAC sanctions screening
>
> Verdict-only. Never touches funds.

**3/**
> It's live on Base mainnet and settles real x402 payments — here's a real
> settlement tx: 0x9ddec8…
>
> And you can reproduce the interesting part yourself: POST an overpriced amount
> and it returns STOP. A guardrail that actually fires. 🛡️

**4/**
> Two things I'm deliberately not claiming: it's not an adoption story yet (just
> shipped), and it's not legal advice or a full AML program — it's the pre-payment
> screening control that sits inside one.
>
> It IS deterministic, auditable, and stdlib-only. No model, no drift.

**5/**
> If you're building agents that pay — or a payout/AP system that releases funds
> autonomously — this drops in front of the signature.
>
> Endpoint + discovery: agent-egress-proxy.onrender.com/.well-known/x402
> MCP tool: forecast_payment
>
> Tell me what it gets wrong.

## Option C — the demo tweet (pair with a screenshot of the verdict JSON)
> Pointed Blackwall at a payment on Base mainnet: it caught a price gouge and
> returned STOP before signing — using the counterparty's own on-chain price
> history. Settlement is on-chain, the STOP is reproducible. 🛡️

**Hashtags (optional):** `#x402` `#AIagents` `#Base` `#agentpayments`

---

## Where to post (beyond your timeline)
- **X** — main thread + pin it.
- **Farcaster** — the Base/x402 builder crowd lives here; cross-post the thread.
- **x402 community** (Discord/Telegram) + **Coinbase x402 / Base ecosystem** channels — share the endpoint + a one-liner, builder-to-builder.
- Link back to the awesome-x402 listing (already merged).

## Pre-post checklist
- [ ] Re-fetch `GET /.well-known/x402` — confirm it's up and `screening` shows `["sanctions-ofac"]`.
- [ ] Confirm the settlement tx still resolves on BaseScan.
- [ ] Screenshot a live STOP verdict JSON for the demo tweet.
- [ ] No adoption/"trusted by" claims. Capability proof only.
