# Demand-first: the one customer conversation

STRATEGY.md's #1 action. The bottleneck is not more product — it is **one real
person who needs this and will say so.** This doc exists to get to that one
conversation, led with the on-chain demo (undeniable proof it works), not a deck.

The discipline: the goal of outreach is **learning whether the pain is real**,
not closing a sale. One good conversation that says "no, here's why" is worth
more than ten polite listens. Do not pitch features. Lead with the proof, ask
about their pain, shut up.

## The one-line

> Traceipt turns an x402 payment + a sanctions/policy check into an **independent,
> on-chain-verifiable compliance receipt** — the audit artifact a facilitator's
> own dashboard can't be, because we didn't move the money or make the verdict.

## The proof to lead with (already live)

A real Black_Wall risk verdict (STOP on the OFAC-sanctioned Tornado Cash address
`0x8589…FDA16`), bound to a paid x402 transaction, anchored on Base Sepolia,
independently verifiable by anyone:

**https://sepolia.basescan.org/tx/0x1a9b1db1992d157ce1e0da6dc30d854fd0eaa99a524a1862b7838ba960848010**

(calldata = `TRACEIPT-ANCHOR` + the Merkle root; inclusion proof verifies offline;
full chain in `FLYWHEEL.md`.)

## Who has the pain (hypotheses, ranked)

The acute question a buyer feels: **"when my regulator/enterprise customer/counter-
party asks me to *prove* my agent didn't pay a sanctioned party — and prove it
independently, not just show a vendor's log — what do I hand them?"**

1. **Agent platforms serving enterprise/regulated buyers.** They inherit their
   customers' audit demands (EU AI Act Art. 12, MiCA Art. 76). Examples to probe:
   the **Amazon Bedrock AgentCore Payments** ecosystem (compliance-forward,
   Base+Solana), fintech-facing agent tools. *Highest willingness to pay, slowest
   cycle.* The wedge vs. Coinbase CDP (which already screens): CDP's trail is
   **Coinbase-hosted**; a regulated buyer often needs a **portable, neutral,
   offline-verifiable** proof they hold themselves.
2. **High-value / money-moving x402 sellers.** Services where a wrong payment
   matters — e.g. **Yield.xyz's AgentKit** (agents moving into 3,300+ yields).
   Dispute + audit needs are concrete.
3. **x402 facilitators / routers** (TrustBench, AEON). Not end customers — a
   *partnership*: embed Traceipt compliance receipts as a value-add for their
   sellers. They have distribution; we have the depth they lack.

## Where to find them

- **x402 Foundation** (Linux Foundation) — Discord / GitHub discussions; the
  compliance gap is openly acknowledged there.
- **The Bazaar** `/discovery/resources` and **[x402.org/ecosystem](https://www.x402.org/ecosystem)**
  — actual live sellers, filterable.
- **[awesome-x402](https://github.com/xpaysh/awesome-x402)** /
  **[awesome-agentic-commerce](https://github.com/Merit-Systems/awesome-agentic-commerce)** — the sellers + tooling, with contacts.
- **The MCP registry** — once our verifier is listed, inbound from agents who
  search "verify payment receipt."

## Cold message (ready to adapt — keep it short)

> Subject: proof your agent didn't pay a sanctioned party
>
> Hi <name> — you're running real x402 payments through <thing>. Quick question:
> when a customer or auditor asks you to *prove* a payment was screened against
> sanctions before it settled — independently, not just from a vendor's log — what
> do you hand them today?
>
> We built the missing piece and it's live on-chain: a risk verdict bound to a
> paid x402 tx, anchored on Base, verifiable by anyone —
> https://sepolia.basescan.org/tx/0x1a9b1db1992d157ce1e0da6dc30d854fd0eaa99a524a1862b7838ba960848010
>
> Not selling anything yet — trying to learn if this is a real problem for you or
> a solution looking for one. 15 min?

## Honesty guardrails

- This is a **hypothesis list**, not validated demand. Every name here is a guess
  about who *might* feel the pain.
- **Distribution ≠ demand.** Getting a reply proves interest, not willingness to
  pay. The only real signal is someone saying "yes, I need this, here's my
  budget/timeline."
- Do not overclaim: no "standard," no "only," no "we own it" (see STRATEGY.md).
  Lead with the verifiable tx and let it speak.
- If three good-faith conversations in the top segment all say "not a priority,"
  that is a **finding** — it means the compliance-receipt thesis is early, and the
  honest move is to say so, not to keep building.
