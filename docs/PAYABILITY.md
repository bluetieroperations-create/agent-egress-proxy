# The state of x402 payability

> **SUPERSEDED 2026-08-28 — read this first.** The headline below was wrong, and
> wrong in our favour to fix. The 86 `opaque_402` hosts were not opaque: **80 of
> them carry a complete x402 v2 challenge in a `payment-required:` header** that
> nothing in this repo read. Their bodies are literally `{}`, which is why they
> looked empty. The other 6 have simply moved or gone (400/404/405/410) since the
> survey. Scoreable hosts go from **73/195 (37.4%) to 153/195 (78.5%)**. The
> carrier is now implemented in `x402_challenge.py`; the sections below are kept
> as the record of what a body-and-`WWW-Authenticate`-only client sees.
>
> **What that 78.5% does and does not buy — measured, not assumed.** It is a
> parseability number, not a data-volume one. Crawling all 86 yields 65 resource
> records across 55 payees, and **every one was already known from the Bazaar**:
> 65/65 payees already had advertised min/max in `data/directory.json`, and 0
> resource URLs were new. The reputation corpus does not grow by a single payee.
> What actually changes is listed in "Why this matters" below.

Measured 2026-08-18 by probing every distinct host in `data/directory.json`
(`directory_liveness.py`). Method and raw data are in this repo; `data/liveness.json`
is the full result.

## Result

| class | hosts | share | meaning |
|---|---|---|---|
| `opaque_402` | 86 | **44.1%** | returns 402 with no readable payment requirements in either carrier |
| `body_accepts` | 71 | 36.4% | requirements in the JSON body — the common v1 style |
| `wellknown` | 23 | 11.8% | no 402, but serves a `/.well-known/x402` descriptor |
| `other` | 12 | 6.2% | reachable, not an x402 challenge |
| `hdr_accepts` | 2 | 1.0% | requirements in `WWW-Authenticate: X402 requirements="<b64>"` — the v2 style |
| `dead` | 1 | 0.5% | does not resolve |
| **total** | **195** | | |

**Only 73 of 195 hosts (37.4%) present payment requirements a client can parse and act on.**

## Why this matters

Three things change, and none of them is "more data":

1. **Payability.** `clients/x402_pay` can now sign against these endpoints. Before,
   it saw a 402 with `{}`, found no `accepts[]`, and gave up. This is the funded-
   signer path, so it is the difference between being able to transact with 80
   endpoints and not.
2. **Independence from the Bazaar.** Requirements can now be read from the endpoint
   itself rather than only from Coinbase's catalog. The endpoint is authoritative for
   its *current* price; a catalog lags, and can be stale, wrong, or unavailable.
3. **A cross-check that did not exist — measured, and NOT worth building.** Having
   both sides makes catalog-vs-live divergence measurable. So it was measured:
   **170 live quotes compared against the catalog, 0 divergent.** (An earlier run on
   a narrower Base-only filter found 1 of 126, at 4.5x.) It is also redundant where
   it matters: in x402 the agent pays what the LIVE 402 says, so a catalog mismatch
   misdirects *selection*, not price — and an anomalous live quote is already caught
   at verdict time by the quoted-vs-settled-median gate, whatever the catalog said.
   Recorded here so nobody builds it twice.

An x402 client cannot pay what it cannot parse. The 402 response is the entire
negotiation: it carries the payee, the amount, the asset and the chain. An endpoint
that returns a bare 402 has advertised that it wants money and given no way to send
it. To an automated agent that is indistinguishable from being broken.

This is not a liveness problem. All 86 respond. They are running, they intend to
charge, and they are unreachable by any conforming client.

## They are not mostly demos

The obvious objection is that these are weekend projects. **They are not.** Only
**23 of 86 (27%)** sit on free hosting (`*.vercel.app`, `*.workers.dev`,
`*.onrender.com`, and similar). The remaining **73% run on their own domains** —
which is what someone does when they mean it.

## Two measurement traps

Both of these were mistakes made while running the survey by hand, and both make the
ecosystem look smaller than it is. Anyone repeating this work should avoid them:

1. **GET-only probing.** A `405` is a POST-only endpoint, not a dead one. Retrying
   with POST recovered **14** hosts that a GET-only sweep had written off.
2. **Body-only challenge parsing.** The x402 v2 style carries requirements in a
   `WWW-Authenticate` header. Until 2026-08-25 nothing in this repo read it, so those
   endpoints were uncrawlable and unpayable despite being perfectly well-formed. Now
   parsed by `x402_challenge.py`. It is only 2 hosts today — but it is 2 hosts that
   every body-only client in the ecosystem still cannot pay.

## What we could not say — now answered

The original text read: *"We do not know why the 86 are opaque. Candidates: a third
carrier nobody has implemented, a middlebox stripping the header, an incomplete
server, or a deliberate choice to gate discovery."*

**It was the first candidate.** Probed all 86 on 2026-08-28:

| | hosts |
|---|---|
| `payment-required:` header, bare base64 x402 v2 doc | **80** |
| moved or gone since the survey (400/404/405/410) | 6 |

They are not demos and not abandoned. `api.ipintel.ai` — one of the 80 — has **78
distinct payers and 145 settlements**. Someone has been paying these endpoints all
along; we simply could not read what they were asking for.

The lesson generalises past this repo: **three different carriers now exist for the
same challenge** (JSON body, `WWW-Authenticate: X402 requirements=""`, and
`payment-required:`), and a client that implements one or two of them silently sees
a smaller ecosystem than exists. Measured cost of reading only two of three: 41
percentage points of the ecosystem invisible.

The sample is also not the whole ecosystem — it is the hosts reachable from our crawl
of the CDP Bazaar and public discovery documents, not a census.

## Reproducing

```sh
python directory_liveness.py --directory data/directory.json --out data/liveness.json
```

Pure helpers with injected network; `rank_leads` is prioritisation only and never
touches a verdict. See `docs/DIRECTORY_LIVENESS.md` for the classifier's internals.
