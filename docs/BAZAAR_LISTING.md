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

## The change made (2026-09-15) — and a security finding it exposed

**My first recommendation above was half wrong and is corrected here.**
`build_resource_info` returns a **v2-spec ResourceInfo object**, so the object
shape is correct and CDP's string is its own projection of `resource.url`. The
fix is not to emit a string; it is to make **`url` absolute**.

### The security finding

`_challenge` passed the request's `resource` field into `build_resource_info`
verbatim, and **that field is client-supplied**. Measured on the live service
before fixing — each of these came back inside a real 402 advertising our
`payTo`:

```
resource=https://evil.example/owned   ->  url "https://evil.example/owned"
resource=javascript:alert(1)          ->  url "javascript:alert(1)"
resource=//evil.example/x             ->  url "//evil.example/x"
```

The 402 is the document CDP indexes. So an attacker could pay us **0.001 USDC**
with a foreign `resource` and have **their** url catalogued against **our**
payout address — borrowing our settlement history for the price of one call. And
`javascript:` in a field a catalog UI renders is an XSS primitive we would be
publishing ourselves.

### The fix

`x402.canonical_resource_url(origin, requested)` — **the origin is ours, the path
is theirs.** Scheme and netloc from the request are discarded unconditionally
(which also disposes of `javascript:`, `data:`, `file:` and `//host/x`);
traversal segments are normalized away; control characters are stripped, since
the value is echoed into a base64 response header where a newline forges header
structure; length is capped. The path is still taken from the request, because
different paths are different priced resources.

Fed from `BLACKWALL_ORIGIN` / `--origin`, which **already existed** for
`openapi.json`'s `servers[]` and was simply never used here. With no origin
configured the path is returned unchanged, so an existing deploy is unaffected —
but a client-supplied origin is discarded either way, because it was never
legitimate.

Verified on the real boot path:

```
(no resource sent)            -> https://blackwall-free.onrender.com/v1/forecast-payment
/v1/forecast-payment          -> https://blackwall-free.onrender.com/v1/forecast-payment
https://evil.example/owned    -> https://blackwall-free.onrender.com/owned
javascript:alert(1)           -> https://blackwall-free.onrender.com/alert(1)
//evil.example/x              -> https://blackwall-free.onrender.com/x
```

### To deploy

Set `BLACKWALL_ORIGIN` on the service (now declared `sync: false` in both
blueprints) to the public URL of *that* service, then Manual Deploy:

```
BLACKWALL_ORIGIN = https://blackwall-free.onrender.com
```

**Without it the security fix still applies** (a hostile origin is discarded) but
the url stays relative, so the listing hypothesis is untested.

### What I deliberately did NOT change

`extensions.bazaar.info` is present in **2000/2000** catalogued entries and we
emit only `schema`. I left it alone **on purpose**: the absolute url is the one
change with a mechanism behind it, and changing two things at once means a
listing that appears tells you nothing about which mattered. If we are still
absent 24–48h after this deploys, `info` is the next thing to add.

## Original recommendation (superseded by the section above)

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
