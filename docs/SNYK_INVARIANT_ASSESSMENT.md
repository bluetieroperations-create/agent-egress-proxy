# What we can learn or take from Snyk / Invariant Labs

## 1. Public data we can ingest: essentially none

`mcp-scan` — now rebranded **Agent Scan** and sending to **Snyk's** servers — is
**Apache-2.0 code with no bundled dataset**. No blocklist, no tool-hash database,
no known-malicious-server registry. Its own docs:

> "Agent Scan validates discovered components with local checks and the Agent
> Scan API. It sends the component information needed for analysis, including
> agent application details, MCP server configurations and signatures, tool names
> and descriptions, and skill content."

The scanner is open. **The intelligence is not** — it lives behind their API. There
is nothing to ingest.

## 2. What we CAN legitimately take

| Asset | Terms | Use |
|---|---|---|
| `mcp-scan` scanner code | **Apache-2.0** | Readable and reusable with attribution |
| `mcp-injection-experiments` PoCs | **license unstated — check before use** | Three working attacks, valuable as test fixtures |

The PoCs are `direct-poisoning.py` (leaks SSH keys via an `add` tool),
`shadowing.py` (redirects a `send_email` tool), and `whatsapp-takeover.py` —
which masks as a benign "random fact of the day" server **and then changes the
tool to a malicious one.**

That last one is a **literal rug pull** and is the natural test fixture for
`probe.drift`. Do not copy the code without confirming the license first.

## 3. What we learn — and two of these hurt

### (a) Tool pinning already exists. Our drift detection is not novel.

MCP-Scan ships **"Tool Pinning to detect and prevent MCP Rug Pull attacks,
verifying the integrity of installed tools by tracking changes via tool hashing."**

That is `probe.tools_digest` + `probe.drift`, shipped, in a product, with
distribution. We did not invent it and should stop implying we might have.

### (b) They are building the same dataset, from a better position.

> "Invariant Labs is collecting data for security research purposes (only about
> tool descriptions and how they change over time)."

**That is precisely our corpus.** The difference is the collection mechanism:

| | Them | Us |
|---|---|---|
| Source | Their users' installed configs | A registry-wide probe |
| Coverage | Only servers people actually install | All 13,901 reachable servers |
| Growth | Grows with adoption | Grows with our cron |

**Their data is more valuable than ours**, and it is worth saying plainly: a
server somebody installed is a server that can hurt somebody. A server nobody
installed cannot. Breadth is the weaker axis.

### (c) Snyk has fully absorbed the product.

It is Snyk-branded and Snyk-hosted now. Any partnership conversation is with
Snyk, not a startup.

## 4. What this changes

The MCP measurement work is **behind, not ahead**. Rug-pull detection is taken,
and taken by people with a data engine we cannot match on the axis that matters.

What survives is the finding from `COMPETITOR_COVERAGE.md`: across **913
counterparty-screening tools, none scores an amount against the payee's own
settled history.** That is not MCP work and not agent-security work — it is the
payment-verdict engine, and it remains unoccupied.

**Narrower than we thought, and more clearly ours.**

## 5. The one thing worth doing with this

The email to Marc Fischer already asked whether the measurement is redundant.
This is most of the answer, arrived at without a reply: **on rug pulls, yes.**

That makes the *unsent* messages more valuable, not less — they should now lead
with the amount finding, which is the part Snyk demonstrably does not cover,
rather than the MCP reading, which they cover better.
