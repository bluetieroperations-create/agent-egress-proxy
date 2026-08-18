# Is there anything out there to score?

*Survey run 2026-08-18 against `data/directory.json` (harvested 2026-08-02).
Reproduce with `python3 directory_liveness.py`; raw results in `data/liveness.json`.*

`data/directory.json` is the ecosystem map `ecosystem_scan.py` derives from the Bazaar
crawl — 198 x402 payees ranked by an explainable trust score. It is a snapshot of what
was **advertised**. This survey asks the follow-up question that a map cannot answer:
of those payees, how many are still serving a 402 today, and of those, how many publish
payment requirements Blackwall can actually **read**?

That second clause is the one that matters. `forecast` scores a payment from the
challenge's `accepts[]`. An endpoint that is perfectly alive but advertises its
requirements in a format we do not parse is, for our purposes, indistinguishable from
one that is down.

## Result

| class | hosts | meaning |
|---|---:|---|
| `body_accepts` | 71 | `{"x402Version":…,"accepts":[…]}` in the body. **Scoreable today.** |
| `hdr_accepts` | 2 | `WWW-Authenticate: X402 requirements="<base64>"`. Scoreable only if we read the header — see the gap below. |
| `wellknown` | 23 | No inline challenge, but `/.well-known/x402.json` serves a catalog. |
| `opaque_402` | 86 | 402 with nothing machine-readable anywhere. Not scoreable. |
| `other` | 12 | Answers, but not a 402 — route moved, keyed, or retired. |
| `dead` | 1 | DNS/TLS/connection failure. |

**73 of 195 hosts (37%) are live and scoreable right now**, between them advertising
2,524 priced resources. The ecosystem is not a ghost town, and the engine is not
short of things to point at.

The `opaque_402` bucket is the surprise: 86 hosts — 44% — return a bare 402 with no
machine-readable requirements at all. `pro-api.coingecko.com` answers
`{"error":"Payment required","message":"Payment is required to access this resource"}`
and nothing else; `api.ipintel.ai` returns `{}`. These are advertising an x402 price in
the Bazaar catalog while serving a challenge no agent can act on without out-of-band
knowledge. Worth knowing before treating "is in the Bazaar" as "is transactable."

## Two measurement artifacts, both of which undercounted

Recorded because the first two passes of this survey got both wrong, and each error
made the ecosystem look deader than it is. `directory_liveness.py` guards both, and
`test_directory_liveness.py` mutation-tests the guards.

**GET-only probing.** A 405 is not a dead endpoint, it is a POST endpoint. Retrying the
non-scoreable hosts with `POST {}` recovered 14 live, scoreable hosts — including
`api.anchor-x402.com` (52 distinct payers) and `api.loyalspark.online` (50).

**Body-only challenge parsing.** The x402 v2 style carries requirements in a
`WWW-Authenticate` header. Reading only the body classified those hosts as "402 with no
`accepts[]`" — in the output, identical to a broken endpoint.

A third caveat needs no code: single-run results carry transient noise.
`hirescrape.com` failed its TLS handshake in one pass and returned a clean
`body_accepts` challenge in the next. Treat any single host's `dead` as provisional.

## Parser gap: we do not read `WWW-Authenticate`

`grep -rin www-authenticate` over this repo returns **zero hits**. Every consumer takes
`accepts` from the JSON body — `clients/x402_pay.py`, `discovery_crawl.py`,
`readiness.py`, `traceipt_attest.py`, `blackwall.py`, and the OpenClaw hook's
402-challenge recognizer.

So a v2 header-style endpoint is invisible to the crawler and unpayable by the client,
even though the verdict engine would score it correctly if simply handed the
requirements. It is not a verdict bug — nothing is mis-scored — it is a reach bug.

Measured impact is honestly small: 2 of 195 hosts. But one of them is `blockrun.ai`,
second on the lead list by distinct payers, with 27 advertised resources. And the
direction of travel matters more than today's count — this is the *newer* spec style,
so the number should be expected to grow, not shrink.

`directory_liveness.parse_challenge` reads both carriers and is the reference
implementation if we close this in the consumers.

## Lead list

Live, scoreable, ≥8 distinct on-chain payers, and on its own domain rather than a free
hosting subdomain. Distinct payers is the ranking key because it is the hardest signal
to fake — an operator can inflate settlement count against themselves, but not the
number of independent counterparties.

| distinct payers | settlements | resources | host | price |
|---:|---:|---:|---|---|
| 134 | 146 | 8 | `api.bitrefill.com` | 0.001 USDC, Base |
| 61 | 150 | 27 | `blockrun.ai` | 0.0085 USDC, Base *(header-style)* |
| 53 | 146 | 6 | `x402.asterpay.io` | 0.01 USDC, Base |
| 52 | 148 | 19 | `api.anchor-x402.com` | 0.005 USDC, Base |
| 50 | 138 | 29 | `api.loyalspark.online` | 0.01 USDC, Base |
| 48 | 109 | 10 | `company.payapi.market` | 0.001 USDC, Base |
| 34 | 150 | 16 | `api.nansen.ai` | 0.01 USDC, Base |
| 28 | 150 | 29 | `2s.io` | 0.0025 USDC, Base |
| 25 | 106 | 6 | `coingecko.use.x402atlas.com` | 0.005 USDC, Base |
| 18 | 145 | 5 | `agi.apify.com` | 1.00 USDC, Base |
| 17 | 150 | 647 | `airdroppulse.theaslangroupllc.com` | 0.05 USDC, Base |
| 17 | 129 | 52 | `agri.lonestaroracle.xyz` | 0.03 USDC, Base |
| 17 | 134 | 7 | `api.hyperextend.xyz` | 0.003375 USDC, Base |
| 17 | 150 | 17 | `x402.shizu.me` | 0.01 USDC, Base |
| 16 | 150 | 11 | `api.printmoneylab.com` | 0.01 USDC, Base |
| 16 | 148 | 3 | `pro-api.coinmarketcap.com` | 0.01 USDC, Base |

*(39 leads total; full set in `data/liveness.json`.)*

Two names stand out as recognizable businesses rather than weekend projects —
**Bitrefill**, **Nansen**, **CoinMarketCap**, **CoinGecko**, **Apify**, and **0x** all
have live x402 endpoints with real payer counts. Those are companies with a security
function that already has an opinion about autonomous spend.

### Caveat on the ranking

`settlement_count` saturates at 150 for most rows because the backfill paginates a
capped window, so the trust score is effectively driven by `distinct_payers` alone.
The ordering is sound; the absolute settlement numbers are floors, not totals.

## What this does not establish

The survey measures **reachability and format**, not demand. That 73 endpoints are
scoreable says the engine has valid inputs available; it says nothing about whether
any of their operators — or the agents paying them — want a verdict layer. The
distinct-payer counts show agents are paying these endpoints, which is a necessary
condition for a payer-side guard to matter, and not a sufficient one.
