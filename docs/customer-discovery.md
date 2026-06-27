# Customer-discovery kit — Sprint 1 (willingness-to-pay, A3)

Turnkey kit for the founder-only sprint. Goal: get an honest read on **A3 — will
operators pay over the free facilitator baseline** — without leading the witness.
This is the gate the whole spec is conditional on.

## Who to talk to (target 8–12)

The buyer is whoever owns the loss when an agent makes a bad payment.

- **Agent builders shipping x402 payments** — teams wiring agents to pay for
  APIs/resources (find them in awesome-x402, x402 Discord/Telegram, MCP server
  authors who hold wallets).
- **Agent-framework / orchestrator maintainers** — they'd embed a risk check for
  all their users (highest-leverage design partners).
- **Wallet / facilitator teams** — partner *or* competitor; the conversation
  tells you which (feeds A2/A5).
- **Treasury/ops at companies running agents with real budgets** — they feel the
  loss most acutely.

Aim for people **actually making agentic payments today**, not the merely
interested — A1/A3 are about real volume and real pain.

## Interview script (~30 min, problem-first, non-leading)

**Rule:** do NOT pitch Blackwall until the last block. You're testing whether the
problem and willingness exist independent of your solution.

1. **Context (5m)** — "Walk me through how your agent pays for things today.
   Which facilitator? How many payments a day? Trending up?" *(feeds A1)*
2. **Pain, unprompted (10m)** — "Tell me about a time an agent paid for something
   it shouldn't have, or you worried it might. What happened? What did it cost
   you — money, time, trust?" "What do you do today to prevent that?" *Let them
   describe the problem in their words. If they have no story and no worry, that
   is a signal.*
3. **Current solution & gaps (5m)** — "Does your facilitator do anything on the
   risk side? Is that enough? What's missing?" *(feeds A2)*
4. **Willingness, before pricing (5m)** — "If something could tell your agent
   GO/HOLD/STOP on a payment before it signs — counterparty reputation, price
   anomaly — would you wire it in? Why / why not?" "What would have to be true for
   you to pay for it, given the facilitator check is free?"
5. **Price reaction (3m)** — only now: "It's sub-cent per check, or a funded
   session. Reaction?" Watch for "why pay at all" vs "that's nothing if it saves
   me X."
6. **Close (2m)** — "Who else should I talk to?" "Want early access?" *(a real
   yes = a stronger signal than any verbal answer)*

## What counts as signal vs. noise

- **Strongest:** unprompted pain story + asks for access + names a price/budget.
- **Real:** "yes I'd pay because [specific reason vs. free]."
- **Noise (discount it):** "cool idea," "sure, maybe," politeness. Enthusiasm
  without a felt problem or a commitment is a soft no.

## Kill / pivot thresholds (decide BEFORE interviewing — don't move the goalposts)

| Outcome across 8–12 interviews | Decision |
|--------------------------------|----------|
| ≥⅓ give a concrete willingness + a real pain story | **GO** — build toward revenue |
| Pain is real but "won't pay over free" dominates | **PIVOT** — reputation-as-data to facilitators, or a §7 product |
| No felt pain, low volume, "too early" | **SHELVE** — revisit when x402 volume grows (re-check A1) |
| Facilitators/x402-secure already cover it | **PIVOT or KILL** — niche taken (confirm via A2 desk) |

## Fake-door pricing test (run in parallel)

A one-page landing for `forecast_payment`: the GO/HOLD/STOP value prop, the
sub-cent + session pricing, and a single **"Get an API key"** / **"Connect MCP"**
CTA that captures email (no product behind it yet).

- **Drive traffic** from the awesome-x402 listing (already built), x402
  community channels, and a short build-in-public post.
- **Metric that matters:** CTA conversion among *agent-builder* visitors, and how
  many reply when you follow up asking what they'd pay.
- **Threshold:** a handful of qualified, specific "I'd pay for this" replies beats
  a thousand vanity signups. Zero qualified replies after real traffic = corroborates
  a no.

## Output of this sprint

A short memo: interview notes, the willingness tally vs. the thresholds above, the
fake-door numbers, and a one-line **GO / PIVOT / SHELVE / KILL** call with the
evidence behind it. That memo is the HANDOFF §4 willingness-to-pay gate, finally
closed.
