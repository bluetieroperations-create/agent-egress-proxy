# Blockaid — the message (contact form / LinkedIn; no published email)

Checked blockaid.io: no email address published anywhere on the site. Mechanisms
are **Contact Us**, **Request a Demo**, **Report an Issue** (security disclosures
only — not this), plus LinkedIn/X/Telegram.

People: **Ido Ben-Natan** (co-founder/CEO, ex-Israel PM's Office cyber R&D),
**Raz Niv** (co-founder/CTO), **Josh Itzkovitz** (partnerships). Contact-scraper
sites list a masked `i******@blockaid.io` — **deliberately not used.** Guessing an
address from a scraped pattern is how a first contact bounces or lands wrong.

## Why Blockaid is not a competitor

They own the malice question and own it well — MetaMask, Coinbase, Uniswap, and the
Privy integration. Competing there would be foolish. They are the most credible
partner *and* the most credible acquirer for the payment-sanity layer.

## The message — lead with the gift, not the ask

> **Subject:** MCP registry measurement + a gap next to transaction security
>
> Hi — two things, the first of which is just data you might want.
>
> On 27 August I probed all 13,901 remotely-reachable servers in the official MCP
> registry and recorded every tool definition: 127,403, with descriptions and input
> schemas. Findings: 2,631 listed servers (18.9%) don't answer at all, and 24
> servers from 21 unrelated publishers on 24 different hosts serve only
> echo/add/server_time while advertising real capability — two of them advertising
> safety functions ("rug check, honeypot sell-sim, drainer scan") they don't
> implement. I make no claim about intent; the verifiable part is the gap between
> what the registry says and what the server serves. Happy to send the dataset, no
> strings — it's relevant to anyone whose users' agents call these tools.
>
> Second: I've built a payment-verdict engine that sits next to what you do rather
> than over it. You answer "is this transaction malicious?" — drainer contracts,
> known-bad addresses, simulation. I answer "is this payment sane?": is the amount
> far off what this payee has historically settled for, do the signed EIP-3009
> authorisation and the stated claim match, is the payee's payer set a wash farm,
> has the endpoint stopped serving what it advertises. A clean contract paying a
> fair-looking address at 40x the going rate isn't malicious — it clears a malice
> check and is still a bad payment.
>
> That distinction seems to matter more as agents pay autonomously and nobody
> reviews the transaction. Worth a conversation, or is this already inside your
> roadmap?
>
> — [name], bluetier.operations@gmail.com

## Why it's shaped this way

- **Opens with something given, not asked for.** The MCP dataset is real, verifiable
  and relevant to them; it earns the second paragraph.
- **Concedes their position explicitly.** "Next to what you do rather than over it."
  Any hint of competing with a MetaMask/Coinbase/Uniswap vendor ends the thread.
- **The 40x sentence is the entire argument**, and it is falsifiable — if their
  simulation already flags gross overpayment against a payee's own history, they
  will say so, and that is the answer.
- **Ends by inviting the kill.** "Or is this already inside your roadmap?" makes a
  one-line "we do that" easy, which is the outcome worth knowing fastest.

## Honest caveats

- Blockaid is well-funded with marquee logos. A Gmail from an unknown sender needs
  the data to carry it, which is why the measurement leads.
- Sending the MCP dataset to a security vendor is genuinely useful to them and
  costs nothing — but it is also giving away the current reading. That is the
  intended trade: the reading is the introduction, the *series* is the asset.
- Do NOT use "Report an Issue" — that is a vulnerability-disclosure channel and
  this is not a vulnerability report.
