# Getting listed in the CDP x402 Bazaar

Status as of **2026-09-15**: our first CDP settlement landed
(`0x96559181…44bfd`, Base mainnet), and we are **not in the catalog** — verified
by scanning all 15,572 entries, not by sampling.

## Two things worth knowing about the catalog

**It is PUBLIC.** `GET /discovery/resources` and `/discovery/search` on
`https://api.cdp.coinbase.com/platform/v2/x402` both answer **200
unauthenticated**. `cdp_bazaar_check.py` used to mint a Bearer JWT and refuse to
run without credentials, so the check went unrun for that reason alone. It now
needs none (and still honours them if set).

**The search endpoint cannot prove absence.** `?q=` *is* honoured when there are
matches (`q=onesource` → 19 of 20 results contain it), but on a **miss** it
silently returns **20 arbitrary entries** with `partialResults: true`. So a miss
looks like a page of unrelated sellers. Search may only ever *confirm* a hit;
absence is settled by the full paginated scan. Pagination is offset-based and
the response states `pagination.total`, so the scan knows when it is genuinely
finished rather than inferring the end from a short page.

## Why we are probably not indexed — measured, not guessed

Sampled **2000 listed entries**:

| | listed entries | what we advertise |
|---|---|---|
| `resource` type | **string, 2000/2000 (100%)** | a **dict** |
| `resource` value | **absolute URL, 2000/2000 (100%)** | `.url` is **relative** (`/v1/forecast-payment`) |
| `extensions.bazaar.info` | **2000/2000 (100%)** | absent |
| `extensions.bazaar.schema` | 2000/2000 (100%) | present ✅ |

A listed entry looks like:

```
resource: "https://api.onesource.io/api/chain/erc20-balance"
extensions.bazaar.info: { input: {method, type, queryParams}, output: {example} }
```

Ours:

```
resource: {"url": "/v1/forecast-payment", "description": "...", "tags": [...]}
extensions.bazaar.schema: { properties: { input: {...}, output: {...} } }
```

**The reasoning, stated as a hypothesis rather than a proof.** The catalog entry
is what CDP *stores*, which may be a normalized projection rather than a verbatim
copy of what a seller advertised — so 100% string-typed `resource` could partly
be CDP normalizing. But CDP has to derive an absolute URL from *somewhere*, and a
relative path carries no host. It cannot invent `blackwall-free.onrender.com`
from `/v1/forecast-payment`. So "a relative path gives the indexer nothing to key
on" is sound, consistent with our absence, and **not yet confirmed** — the test
is to emit an absolute string and see whether we get indexed.

## Recommended change

In `discovery.py`, emit `resource` as an **absolute URL string** and move the
descriptive fields into `extensions.bazaar` (adding `info` alongside the existing
`schema`).

**Low risk to our own stack, checked rather than assumed:**
`x402_challenge.parse_challenge` reads `accepts[]`, never `resource`, and
`billing_preflight`'s challenge round-trip compares the fields a payer *signs* —
`resource` is not one of them. So no parser, client or preflight depends on the
dict shape.

Re-run `python cdp_bazaar_check.py` after deploying. Exit codes: **0** listed,
**1** not yet, **2** inconclusive — so it can be scheduled.
