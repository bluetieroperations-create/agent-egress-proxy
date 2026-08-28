# Finding the kill signal ourselves — no email required

The question was: why wait for Blockaid to tell us whether the payment-sanity gap
is real? Several competitors expose **free tools** on the MCP registry. Call them.

## The test

Ask the closest competitor the exact question Blackwall exists to answer:

> *"About to pay 0.40 USDC to an x402 API whose settled median is 0.01.
> Is this payment safe?"*

`lionx402` advertises `lion_declare_need` as a **FREE router**: *"Tell LION what
you need in plain language. Returns the one paid path to call next."* So it is
their own answer to "which of our products fits this question."

## The answer

It routed to **`company-research` ($0.03)** — firmographics and web research on a
company — plus a free "deep research sample". It offered nothing about price,
settled history, or the payment itself.

**It did not recognise the question.** Its `lion_wallet_screen` is address
screening — PASS/WARN/BLOCK on *who* you are paying. Nothing in its catalogue
reasons about *how much*.

`trust-score.api.klymax402.com` returned **HTTP 402** — paid only. Its published
description is "a composite trust score 0-100 for a domain, endpoint or wallet":
again reputation of the counterparty, not sanity of the amount.

## What this establishes, and what it does not

**Establishes:** the closest competitor's own routing layer has no concept of
overpayment-against-settled-history. The distinction between *"is this
counterparty bad?"* and *"is this amount wrong?"* is real, and it is not covered
by the product that overlaps Blackwall most.

**Does not establish:** that their paid tools lack it entirely. `lion_declare_need`
may be a crude keyword router that fails to represent what `lion_compliance_bundle`
actually returns. One probe of a free front door is evidence, not proof.

**The cheap way to make it proof:** `lion_wallet_screen` costs **$0.001** and
`lion_company_research` **$0.03**. Paying a few cents through the funded-signer
client in `clients/x402_pay.py` would settle it conclusively. That is the next
test, and it costs less than a stamp.

## Why this matters more than the outreach

The Blockaid and Turnkey emails ask other people for an answer we can measure
ourselves. This corpus contains **13,901 probed servers with 127,403 tool
definitions**, of which hundreds sell risk, trust, compliance or screening. Every
one is a competitor whose exact capability can be tested by calling it — free
where they expose a free tier, for cents where they do not.

The outreach still has value (distribution, partnership, being told we are wrong
by someone senior). But it is no longer the only path to knowing whether the gap
is real, and it should not block anything.
