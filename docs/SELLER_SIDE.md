# Seller-side scope — what we can sell to the OTHER side of the market

Written 2026-09-06, after a full live competitor sweep (`COMPETITIVE.md`).

## Why this exists

Every gate in this repo serves the BUYER: an agent about to pay, asking whether
it should. Ontario Protocol sells to the SELLER — a paid directory, a "why are
agents not buying your endpoint" diagnostic, a discoverability check, and a free
published dataset. That is revenue from a side of the market we have never
addressed, and **we already own every input**. Nothing below needs new data
collection.

## What we can compute per seller TODAY

Given one payee address or host, from committed artifacts plus a live probe:

| Question a seller pays to answer | Where it already comes from |
|---|---|
| Can an agent reach me at all? | `asset_coverage` reach — 195 probed, **177 answered, 18 silent** (2026-09-05) |
| Can an agent PARSE my price? | `x402_challenge.parse_challenge` — which of the three carriers, or none |
| Is my payee address even valid? | `payee_syntax` — found a real seller advertising a glued `.env` value |
| Is my asset identifier valid? | `asset_coverage` malformed — found a 39-hex BSC address, still live |
| Can my amount be SCALED? | `payload_sim.known_decimals` — an unresolvable asset means no amount check |
| Am I priced out of my category? | `category_pricing` — per-category settled median |
| Does my advertised price match what I collect? | `price_integrity` divergence |
| Would a buyer's engine cold-start HOLD me? | `decide_payment` thin/Sybil gates — **18 of 266 payees are `thin`** |
| Are my payers real, or am I paying myself? | `payer_reputation.sybil_ring` + the cross-payee graph |

That last row is the one worth charging for, and the section below explains why.

## The four surfaces, ordered by whether they need a network effect

### 1. Seller diagnostic — "why agents are not paying you"  *(build first)*

A report keyed by payee/host, assembled from the table above. NO network effect
required: it is a measurement, true on the day it is run, useful to a seller with
zero buyers.

It is also the OUTREACH artifact. The seller email has been blocked for weeks on
having nothing worth saying; "here is why agents cannot pay you, measured across
195 live hosts" is worth saying, and we have already found two sellers with real
defects this way.

Effort: ~2 days. Reuses everything; the work is assembly, a report format, and a
CLI. Ship as a CLI + a report before any endpoint.

**BUILT** — `seller_report.py`. `python3 seller_report.py <payee|host>
[--offline] [--store rep.db]`; exits 0/1/2 (nothing blocking / an engine will
not clear you / an agent cannot pay you), so a batch run over the corpus is
actionable without reading every report.

Four bugs found by running it live, all fixed, and every one is the kind that
only appears when a diagnostic is pointed at a real business:

| what happened | why it matters |
|---|---|
| **Two businesses in one report.** `blockrun.ai` carries three payees; the CLI resolved the probe and payer graph from `matches[0]` while the report described `max(settlement_count)`. One payee's graph ("26 payers corroborated") landed in another's report ("1 distinct payer, possible wash-trading"). | The numbers contradicted each other on the page. Fixed structurally: `select_subject` is the one selection site and callers now pass FUNCTIONS, so nothing can be resolved against a different row. |
| **Congratulated a seller on absent evidence.** The engine's Sybil flags need a minimum payer count to fire, so a payee with ONE payer tripped neither and fell into the positive branch: "0 of your payers also pay other known endpoints, which is the hard-to-fake half of a reputation", marked ok. | Fixed with three measured tiers. Zero corroboration is 11 of 266 endpoints (4.1%) against a median of 10, so it is a real warning — and every tier now quotes that median, which turns an accusation into a measurement a seller can check. |
| **Accused a gift-card merchant of gouging.** Bitrefill's dearest advertised option is $1000 against a $0.25 commerce median, reported as "4000x your category". It is a gift card. | The hull hazard from `advertised_prices.py`, compounded: the engine gates on the AMOUNT PAID, not the listing, so judging the listing was stricter than the engine and wrong about it. Now stated as the engine's actual consequence — where the hold line sits and which of your options cross it. |
| **The flagship finding was unreachable.** `PayerReputationSource` takes EDGES; passing the store raised a `TypeError` that the fail-soft turned into a benign "not assessed". | The wired-and-inert pattern, fourth time in this repo. Fixed to `from_store`, made loud, and the tests drive that exact path. |

Guarded by `test_seller_report.py` — 40 tests, 23 mutations verified killed.

### 2. Reachability / discoverability check  *(build second)*

Ontario's `coinbase-bazaar-readiness`. Narrower than the diagnostic and mostly a
subset of it — worth a separate surface only if sellers ask for it by name.

Effort: ~half a day once (1) exists.

### 3. Free published dataset  *(cheap, do alongside)*

`GET /v1/ecosystem.json` — the census we already commit. Ontario ships this and
it costs them nothing: it is the marketing, the proof, and the inbound.

Two rules, both learned the hard way in this repo: it must be **dated**, and it
must lead with **reach** (177 of 195 answered), because a quiet report and a
blind one look identical.

Effort: ~half a day. `data/asset_coverage.json` is already the artifact.

### 4. Paid directory — `list-service` / `refresh-listing`  *(LAST, and maybe never)*

**This is a two-sided marketplace and we have no demand side.** A seller pays to
be found *by buyers who exist*. Our own census says the entire live x402 market
is 195 hosts and ~371 quotes; the buyers are not queueing. Ontario has the same
problem and shipped anyway, which is evidence they believe in the market, not
evidence the listings sell.

Do NOT build this to copy them. The honest precondition is buyer traffic through
`/v1/forecast-payment` from someone other than us. Until then a listing is a
receipt for nothing.

`seller_audit.py` already implements the defensible version — a **verified tier
that is EARNED, not paid**: audited from readiness + on-chain history + sanctions
+ price fairness, signed, expiring, revocable. Selling placement would corrupt
exactly the thing that makes the badge worth having. If this is ever built, the
seller pays for the AUDIT, never for the RANK.

## Where we beat Ontario on their own flagship, and it is not close

`POST /api/x402/demand-authenticity-report` classifies **up to 20** Base USDC
settlement receipts as self-payment / related-party / unattributed.

Ours is not sample-bounded: **37,943 settlements from 2,028 payers**, with a
CROSS-PAYEE graph. `payer_reputation.sybil_ring` can say *none of your payers
pays anyone else* — a statement a 20-receipt window cannot make at any sample
size, because the evidence lives in the OTHER payees, not in the receipts.

That is the diagnostic's headline finding and the one a seller cannot get
anywhere else.

## Stale input to fix first

`data/liveness.json` carries a `class` per host from the **2026-08-18** survey,
before the `payment-required` carrier was implemented. It still says **86 hosts
serve `opaque_402`**, which was true then and is not true now — that fix moved
scoreable hosts from 73/195 to 153/195. Today's real number is **18 of 195 do
not answer**.

A diagnostic that told a seller "your challenge is unparseable" from that field
would be reporting our own stale artifact as their bug. Re-derive parseability
live, or re-run the survey, BEFORE the first report goes to anyone.

**RESOLVED by construction, not by refreshing the file.** `seller_report.py`
derives reachability and parseability from a LIVE probe or reports them as NOT
CHECKED; it never reads that field, and `test_seller_report` asserts so against
the source. Refreshing `liveness.json` would have fixed today's number and left
the next stale snapshot free to become tomorrow's false accusation.

## What is NOT in scope

No new gates. No new corpus. No paid placement. Nothing here changes a verdict —
these are reports over data the engine already produces, and a report must never
become an input to the gate that scores the same seller.
