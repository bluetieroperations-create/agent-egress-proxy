# Blackwall — what it checks, sees, and stores

A plain-language note for the people whose money is being protected and the wallet /
agent providers integrating Blackwall. No jargon.

## What Blackwall does

Before a payment is signed, Blackwall gives one answer — **go, wait, or stop** —
so a risky payment can be caught *before* it's irreversible. It's a check, not a
bank: in its default mode it only advises and **never holds your money.**

## What it checks

For the payment you're about to make, Blackwall looks at:

- **Who you're paying** — the recipient's real track record (past settlements and
  disputes), so a brand-new or previously-bad counterparty raises caution.
- **The price** — whether the amount is wildly out of line with what this recipient
  normally charges, or with comparable services.
- **Sanctions** — whether the recipient is on an official sanctions list.
- **The actual signed payment** — that what you're really signing matches what you
  think (same recipient, same amount, same coin), and that it isn't a disguised
  "give a stranger the keys to your wallet" instruction.

## What data it sees

To make that call, Blackwall needs the **details of the payment being judged**:
the recipient address, the amount, the coin, the chain, and — if you want the
strongest check — the transaction or signed payload so it can inspect the real
thing.

It does **not** need, ask for, or receive:

- your private keys or seed phrase,
- your identity, name, email, or KYC documents,
- your balances or your other transactions,
- anything about wallets other than the counterparty being paid.

## What it stores

The one thing that makes Blackwall better over time is **counterparty outcomes** —
records of how payments to a given recipient turned out (did they settle cleanly, or
get disputed). This is built from **public, on-chain-confirmed** activity and is
keyed to the *counterparty* (the payee), not to you. It's what lets the guard say
"this recipient has a clean history" or "this one has a pattern of disputes."

It is **not** a profile of you, and it isn't your payment history for sale.

## You can run it so *no* data leaves your walls

Blackwall's core is dependency-free and can run **in-process** — inside your own
infrastructure — so the payment details never leave your systems at all. There's
also a hosted option if you'd rather call an API. Same verdict either way; you
choose the trust boundary.

## Speed

The check adds **about a moment** — fast enough to sit in the signing flow. If a
payment pauses briefly, that's the check running (or, rarely, Blackwall being
momentarily unreachable — see the availability setting below).

## If Blackwall is ever briefly unreachable

You choose what happens, and can change it anytime:

- **Pause payments** *(safest, default)* — wait until the check is back.
- **Keep paying** — let payments through without the check during that short window.

Either way, **a payment already flagged as dangerous is always blocked.** This
setting only covers the rare moment the check can't be reached. (In the wallet
adapters this is the `FAIL_CLOSED` / `FAIL_OPEN` toggle, with built-in
`describe_policy()` copy for your settings screen.)

## In one line

Blackwall checks *the payment in front of it* against *the recipient's public track
record* — it doesn't profile you, never touches your keys or your money, and can run
entirely inside your own systems.
