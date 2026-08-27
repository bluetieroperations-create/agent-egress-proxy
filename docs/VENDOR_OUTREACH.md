# Vendor outreach — draft for you to send

Send from your own address. Do not attach the dataset. One finding, one method,
one question. If nobody replies, that is the answer and it cost an afternoon.

## Targets

| Company | Why them | Who to find |
|---|---|---|
| **JFrog** | Ships an MCP registry product; needs to know which catalog entries are junk | Product lead, MCP/AI catalog |
| **Snyk** | Sells supply-chain scanning; MCP is the adjacent surface | Security research / product |
| **Checkmarx** | Published MCP security guidance | Research lead |
| **UpGuard** | Published "Six MCP Security Incidents" | Research lead |
| **Bishop Fox** | Published MCP supply-chain research | Research lead |

## The email

> **Subject:** 24 MCP servers advertise tools they don't serve — data offer
>
> Hi [name],
>
> I probed all 13,901 remotely-reachable servers in the official MCP registry on
> 27 Aug and recorded every tool definition — 127,403 of them, including
> descriptions and input schemas.
>
> One result you may care about: 24 servers from 21 unrelated publishers, on 24
> different hosts, serve only `echo`/`add`/`server_time` while advertising real
> capabilities. Two of them advertise safety functions — "rug check, honeypot
> sell-sim, drainer scan" and "pre-trade safety verdicts" — and implement none of
> it. An agent told a drainer-scan tool exists may act as though it ran.
>
> Also: 2,631 listed servers (18.9%) don't answer at all, and 47 complete the
> handshake and expose nothing.
>
> None of this is in the registry metadata. It only shows up if you call every
> server and hash what comes back.
>
> I'm taking this reading monthly. The part I think is worth more than any single
> snapshot is the diff: which servers change their tool definitions **without
> changing their version** — the rug-pull signature, where a tool keeps its name
> and its description (the text the model obeys) gets rewritten.
>
> Is that a feed [company] would want? Happy to send the current reading so you
> can check the numbers yourself.
>
> [name]

## Why this email is shaped this way

- **Leads with a specific verified number**, not a pitch.
- **Names the limitation** — intent is not claimed, only the mismatch. Security
  researchers check things; overclaiming ends the conversation.
- **Offers the data free** to verify. The product is the feed, not the file.
- **One question at the end.** Not a meeting request — this stays behind a
  computer, per the standing constraint.

## What counts as a result

- **Reply asking for the data** — real interest, send it.
- **Reply saying "we already do this"** — valuable; ask what they use, then stop.
- **Silence from all five** — the honest signal that there is no buyer here.
  Stop before spending months, exactly the mistake made 19 times before this.

## What NOT to do

- Do not name individual publishers publicly as bad actors. Intent is unproven,
  some are likely abandoned scaffolding, and being wrong about one costs the
  credibility of the whole finding.
- Do not send the retracted "53 brands" claim. It was checked and was false.
