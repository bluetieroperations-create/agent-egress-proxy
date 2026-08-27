# Where to point the recorder — verified, not guessed

Three questions: is anyone else doing this, where does it work in a bigger market,
and how does it make money. Incumbency checked FIRST this time.

## 1. Is anyone doing this? Yes — and that is the good news

The mechanism (archive a surface the publisher overwrites; sell the history) is a
proven, funded business model:

| Company | Surface they archive | What they sell |
|---|---|---|
| **Serif Health** ("Signal") | Hospital/payer machine-readable price files | Paid **historical look-backs** — which providers got reimbursement increases or decreases, unit-level price trends |
| **Turquoise Health** | Same files (measured 91% hospital posting rate) | Rate benchmarking |
| **Payerset / DoltHub** | Same | Datasets, free tier |
| **Policy Reporter / MMIT** | Payer medical & prior-auth policy PDFs | Change alerts to pharma market-access teams |
| **Genability (Arcadia)** | Utility tariff filings | Tariff history API |

This kills healthcare pricing as a target and simultaneously proves the model.
Nobody had to invent demand for "the history of a thing that gets overwritten."

## 2. Where to point it

The trap in rounds 1-3 was that rich markets have entrenched incumbents *because*
they are rich. The mechanism's edge is TIME, not access — so the target must be a
surface **new enough that the incumbent's head start is short too**.

### Checked and dead

- **Hospital price transparency** — Serif/Turquoise/Payerset/DoltHub. Taken.
- **FDA AI-enabled device list** (1,400+ devices, updated 2026-03-04) — MedTech
  Dive runs a tracker, IntuitionLabs runs a tracker, and there is a peer-reviewed
  taxonomy of all 1,016 authorizations. Taken.
- **Payer policy change tracking** — Policy Reporter and MMIT own it, at pharma
  budgets. Not winnable solo.

### Checked and PARTIALLY open — MCP server ecosystem

New surface: the MCP registry dates to late 2025 and grew **3,510 -> 18,966 active
servers between 2026-04-30 and 2026-07-28**. Buyers are enterprises deploying
agents, and the threat is documented, not hypothetical:

- **postmark-mcp** (Sept 2025) — first malicious MCP server found in the wild; a
  backdoor BCC'd every outgoing email to an attacker. ~1,500 active weekly installs
  leaking data.
- **SmartLoader** (Feb 2026) — spent **three months** building five fake GitHub
  accounts to establish reputation, then submitted a trojanized Oura Ring MCP
  server to a legitimate registry. Payload stole browser passwords, cloud session
  tokens, SSH keys and wallet files.

The named defensive control is verbatim our mechanism: *"track versions and watch
commit histories — if a community tool suddenly gets a new maintainer or a flurry
of odd changes, that's a red flag."* A three-month reputation build is invisible in
a snapshot and obvious in a series.

**HONEST KILL on the naive version.** The official registry is synced every four
hours and is **itself versioned** — one study observed 19,099 distinct server names
across **120 revisions** in 88.6 days. If the publisher keeps its own history, there
is no archival moat on registry metadata. Academics have also already run the
one-shot studies: an 89-day drift measurement, a scale security assessment of
internet-facing servers, and an ecosystem census. A commercial blog has published
"52% of MCP servers are dead."

**What survives that kill.** All of the above measure *metadata*. None of it is a
continuous record of **runtime behavior**, which cannot be downloaded — only probed:

1. **Reachability.** One security study found **193 of 464 confirmed servers (41.6%)
   were unreachable three days later.** The registry does not record that.
2. **Tool-definition drift.** The signature MCP attack is the rug pull: ship a benign
   tool, get adopted, then change the tool's description or schema. Nobody is
   recording, at scale and continuously, what each server's tools *said* on each day.
3. **Ownership/maintainer transitions**, joined to 1 and 2.

That is the same shape as `data/liveness.json` — probe, classify, store dated — and
it lands directly in this repo's existing domain (agent guardrails, supply chain).

## 3. How it monetizes

Four models, in increasing order of how long they take to stand up:

1. **Sell the feed to the vendors, not the enterprises.** JFrog, Checkmarx, UpGuard
   and Bishop Fox are all publishing MCP security content and building catalogs;
   the search result's own recommended enterprise control is to host "an internal
   registry of approved MCP connectors." They need the underlying signal. Selling
   data to a security vendor is a behind-a-computer sale and the fastest path.
2. **Alert subscription.** "Your agents depend on 14 MCP servers. Two changed tool
   definitions this week, one changed maintainer, one went dark." Recurring, and
   the value is entirely in having yesterday's copy.
3. **Point-in-time forensics.** After an incident: what did this server's tool
   definition say on the day we installed it? This is evidence, and evidence has no
   substitute — you either recorded it or you did not.
4. **Free census -> inbound.** Publishing a hard number is how Rapid Claw got
   attention with "52% are dead." Publish the census free; sell the feed.

## The rule this round establishes

Do not ask "is this data public?" — it always is. Ask **"does the publisher keep
its own history?"** If yes (MCP registry metadata, SEC EDGAR), there is no moat. If
no (overwritten price files, runtime probes, deleted pages), the archive is the
product.
