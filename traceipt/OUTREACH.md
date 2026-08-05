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

## The proof to lead with (live on MAINNET)

A compliance verdict bound to a paid x402 transaction, settled with **real USDC
on Base mainnet** via the Coinbase CDP facilitator, anchored on-chain, and
independently verifiable by anyone — the receipt checks itself against the chain,
no call to us required:

**https://basescan.org/tx/0xab1c79b60a3ca3386eabc654bf163711140ac17a969e1fa526be8314da38821f**

(calldata = `TRACEIPT-ANCHOR` + the Merkle root; inclusion proof verifies offline;
the immediate-seal proof survives even if our server forgets it; full chain in
`FLYWHEEL.md`.)

**The sanctions teeth** — shown on testnet so it costs nothing to demo — a real
Black_Wall **STOP** verdict on the OFAC-sanctioned Tornado Cash address, anchored
and independently verifiable:
https://sepolia.basescan.org/tx/0x1a9b1db1992d157ce1e0da6dc30d854fd0eaa99a524a1862b7838ba960848010

> Honesty: the mainnet tx above anchored a *verdict* on a real settled payment;
> the STOP-on-a-sanctioned-address path is the testnet link. Lead with mainnet
> (real money, undeniable) and cite the STOP as the capability — never imply the
> mainnet tx was itself a sanctions hit.

**Even stronger once the site is deployed:** point them at
**`traceipt.xyz/verify`** — they paste the receipt and watch it verify against
Base *in their own browser*. "Check it yourself" beats "trust our link."

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

- **x402 Foundation Slack** — `slack.x402.org` — the official community hub (NOT
  Discord, and the GitHub repos have Discussions disabled). Ask questions,
  discuss ideas, share what you're building; the compliance gap is openly
  acknowledged here. A GitHub *Issue* on `x402-foundation/x402` is a weaker
  fallback — Issues are for the project's own roadmap, so a product post reads as
  self-promo unless framed as a genuine ecosystem/standard question.
- **The Bazaar** `/discovery/resources` and **[x402.org/ecosystem](https://www.x402.org/ecosystem)**
  — actual live sellers, filterable.
- **[awesome-x402](https://github.com/xpaysh/awesome-x402)** /
  **[awesome-agentic-commerce](https://github.com/Merit-Systems/awesome-agentic-commerce)** — the sellers + tooling, with contacts.
- **The MCP registry** — once our verifier is listed, inbound from agents who
  search "verify payment receipt."

## Cold message (ready to adapt — keep it short)

> Subject: proof your agent didn't pay a sanctioned party — now live on mainnet
>
> Hi <name> — you're running real x402 payments through <thing>. One question:
> when a customer, auditor, or counterparty asks you to *prove* a payment was
> sanctions-screened **before** it settled — independently, not just from a
> vendor's dashboard — what do you hand them today?
>
> We built that missing piece, and it's now live on **Base mainnet with real
> USDC**: a compliance verdict bound to a paid x402 transaction, anchored
> on-chain. You don't have to trust us — verify it yourself in your browser:
> https://traceipt.xyz/verify (or the raw tx:
> https://basescan.org/tx/0xab1c79b60a3ca3386eabc654bf163711140ac17a969e1fa526be8314da38821f )
>
> The screening layer binds a STOP when an address is OFAC-listed (e.g. a Tornado
> Cash SDN) — happy to share that verdict + its on-chain anchor so you can check
> it end-to-end.
>
> I'm not selling anything yet — trying to learn whether this is a real problem
> for you or a solution looking for one. 15 minutes?

## Slack #general post (x402 Foundation Slack — the primary channel)

Post this in `#general`, then stop talking and watch who engages. Audited
(2026-08-05): the verify page works end-to-end against the live chain, CORS is
clean, and EVERY claim here is independently checkable — including the Tornado
Cash STOP, which is now anchored on mainnet with its verdict published, so the
verify page's "Load the OFAC Tornado Cash STOP" button proves it (verdict binding
+ inclusion + on-chain all pass).

> Hey all 👋 gut-check from someone building in the compliance corner of x402.
>
> The problem I keep hitting: when a customer or auditor asks an x402 operator to
> *prove* a payment was sanctions/policy-screened **before** it settled —
> independently, not from a vendor's dashboard — there's no neutral artifact to
> hand them.
>
> Built a take, live on **Base mainnet**: a screening verdict cryptographically
> bound to a paid x402 tx, Merkle root anchored on-chain. **Don't trust me —
> verify it yourself in your browser** → https://traceipt.xyz/verify. Two one-click
> examples, each checked against Base live: **"Load the live mainnet example"** (a
> clean GO) and **"Load the OFAC Tornado Cash STOP"** (a real sanctioned address →
> STOP, verdict + anchor).
>
> Raw txs if you prefer the chain directly: GO
> https://basescan.org/tx/0xab1c79b60a3ca3386eabc654bf163711140ac17a969e1fa526be8314da38821f
> · Tornado Cash STOP
> https://basescan.org/tx/0xe41c540c7e6f21a3042c52f50e5799fc32af2560d117752be36fe720d3327cd3
>
> Also added hybrid **post-quantum** signatures (ML-DSA-65 next to Ed25519), since
> these are long-retention audit artifacts.
>
> I know `draft-hopley` (compliance-screening receipt) and `draft-vauban`
> (STARK/Starknet receipts) are circling this. I'd like to understand how a live,
> on-chain-anchored, compliance-bound take relates to those — and whether "prove
> it was screened, independently" is a real pain here or premature. Not selling;
> would love 15 min with anyone who's felt it 🙏

_Done (2026-08-05): the OFAC Tornado Cash STOP is anchored on Base mainnet
(`att_1c4b627ea116e9fec0c4`, tx `0xe41c540c…`), its verdict published at
`traceipt.xyz/proofs/tornado-stop.json`, and the verify page's "Load the OFAC
Tornado Cash STOP" button proves it live — verdict binding + inclusion + on-chain
all PASS. The STOP is now as verifiable as the GO proof._

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
