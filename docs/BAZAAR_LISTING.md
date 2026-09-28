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

### What I deliberately did NOT change (SUPERSEDED 2026-09-16 — `info` now added)

`extensions.bazaar.info` is present in **2000/2000** catalogued entries and we
emit only `schema`. I left it alone **on purpose**: the absolute url is the one
change with a mechanism behind it, and changing two things at once means a
listing that appears tells you nothing about which mattered. If we are still
absent 24–48h after this deploys, `info` is the next thing to add.

## 2026-09-16 — still absent at ~25h, so `info` was added

Re-checked ~25h after the first CDP settlement, with the absolute-url fix live
and re-verified in production (a hostile `resource` still comes back rooted at
our own origin). **NOT listed — the FULL catalog was scanned, 16,061 entries.**
The catalog itself grew from 15,572 → 16,061 across the two checks, so indexing
is demonstrably live for other sellers; absence is about us, not about a stalled
pipeline.

**The measurement corrected THIS document.** The table above records
`extensions.bazaar.info: { input: {method, type, queryParams}, output: {example} }`
as the shape. Sampling 100 live entries on 2026-09-16 shows that is the **GET**
form, and it is not universal:

| `info.input` key | of 100 sampled |
|---|---|
| `method` | 100 |
| `type` | 100 |
| `queryParams` | **80 (the GET form)** |
| `pathParams` | 32 |
| `body` + `bodyType` | **16 (the POST form)** |
| `headers` | 4 |

`info` itself is present in 100/100, and `output` (`{example, type}`) in 94/100.
**Ours is a POST endpoint**, so copying this document's own summary would have
advertised query params on an endpoint that reads a JSON body — the catalog
entry is invocable, so that is an entry an indexer could try and fail to call.
A real POST entry, measured:

```
info.input:  {body: {...}, bodyType: "json", method: "POST", type: "http"}
info.output: {example: {...}, type: "json"}
```

Note `info.input.body` is a **worked example** (concrete values), while
`extensions.bazaar.schema…input.properties.body` is a **JSON Schema**. They are
the two halves the catalog carries, not two spellings of one thing, which is why
`info` is additive and `schema` is untouched.

**The advertised example must be one our own engine accepts.** `BLACKWALL.md`'s
curl uses `0xKNOWNGOOD000…`, which `payee_syntax` grades `invalid_hex` — we would
have published an example that the gate answering it would flag. The advertised
body uses a valid placeholder instead, and a test asserts both that every
REQUIRED schema field is present and that the counterparty is not flagged.
Verified by POSTing the advertised body verbatim at a real server: HTTP 200,
`payee_syntax: ok`, verdict HOLD — an honest cold start, which is the correct
demonstration of what this endpoint does.

**Methodological cost, stated plainly:** the 24–48h window is not closed. We are
at ~25h, so if a listing now appears we cannot fully separate "`info` mattered"
from "indexing simply took longer" — the ambiguity the original hold-back existed
to avoid. It is a smaller ambiguity than the first one (absolute url vs nothing),
and the alternative is waiting another day having already learned that the url
alone was not sufficient at 25h. **An operator who wants a clean read can hold
the deploy until past 48h**; the change is additive and nothing else depends on
it.

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


## 2026-09-27 — THE MECHANISM, and it was never our 402

Third scan: **still absent, 17,663 entries, full pagination.** Both deployed
hypotheses had been live and verified for ~11 days. So I stopped guessing and
read the spec (`docs.x402.org/extensions/bazaar`), which states it outright:

> "Cataloging happens when a facilitator processes a `PaymentPayload` that
> includes the echoed `bazaar` extension."
>
> "A server-side declaration alone catalogs nothing if no paying client echoes
> it; a settlement whose payload omits the extension catalogs nothing either."

**The requirement is on the PAYMENT PAYLOAD, and the echo is the PAYER's job.**
A correct 402 is necessary and NOT sufficient. Verified against our own code in
one grep: `clients/x402_pay.py` — which made both CDP settlements — contained no
mention of `extensions` at all, and `x402.py` only ever writes `extensions` into
the 402 *body*. So our settlements catalogued nothing **by design, not by
defect**, and both 402 fixes were changes to the necessary half while the
missing half sat on the payer side the whole time.

### Why this went unfound for three rounds

CDP's own Bazaar page documents *consuming* the catalog, not entering it — the
ingestion rule lives in the x402 extension spec, not in the vendor docs we kept
re-reading. Two 402-shape hypotheses were each derived by **sampling catalogued
entries and diffing against ours**, which can only ever produce statements about
correlates of listing. No amount of sampling recovers a rule about a payload the
catalog does not publish.

### Not just us — three independent sellers, same symptom

- `coinbase/cdp-sdk` **#824** (2026-09-21, groundtruth-now): validator 25/25,
  settled CDP Base payment, absent from a full 15,118-row scan. **Closed, no
  staff answer.**
- `x402-foundation/x402` **#2112** (2026-04-23, Karl-Keller): 8 USDC settlements,
  still unlisted, and CDP never emits the `EXTENSION-RESPONSES` header. **Closed,
  no staff answer.**

That pattern is itself evidence: three unrelated sellers with valid 402s and real
CDP settlements, all absent, is what "the echo is missing" predicts and what "our
402 is malformed" does not.

**One correction to #2112's premise, since it matters before quoting it:** the
spec says a facilitator **MAY** return `EXTENSION-RESPONSES`. CDP not emitting it
is permitted, so it is a diagnostic gap rather than a violation — real, because it
is the only documented way to learn whether your metadata was accepted or
rejected, but weaker than "never emits the documented header" sounds.

### A wrong lead, recorded because it nearly shipped

A search summary asserted that listing requires `extensions.bazaar.discoverable:
true`. **It is not in the spec.** Reading the spec rather than the summary is what
stopped a fourth 402-shape guess going out, which would have been the same
mistake a third time.

### What was built

`x402_challenge.bazaar_echo(body)` — pure, stdlib, tolerant — returns the
`extensions` a paying client must attach, and `clients/x402_pay.py` now attaches
it. Three properties, each mutation-verified:

- **Only `bazaar` is carried.** The challenge is authored by the seller being
  paid and this lands in a payload we SIGN and a third party validates, so every
  other key is dropped — the untrusted-echo class, tenth instance here.
- **Oversize is refused, not truncated.** A truncated block is not what the
  seller declared and a validating facilitator may reject it; a mangled echo is
  worse than none, because none is merely today's status quo.
- **The echo is a SIBLING of `accepted` and `payload`, never nested inside.**
  Seller metadata must not reach the amount, recipient or terms. Asserted on the
  assignment's AST structure, after the first version of that test grepped a
  text window that fell just short of the thing it checked.

The pure decision is tested in the stdlib suite deliberately: the client is gated
behind `eth-account`, so logic placed there would be untested in the container
that runs the canonical check.

### Still a hypothesis, and what would settle it

This is now the *documented* mechanism plus a verified gap in our stack — not a
confirmed cause. It needs **one funded settlement through the patched client**,
then a re-scan. That is the operator's to run, and it is the first test of this
arc with a stated mechanism behind it rather than a shape diffed off the catalog.
