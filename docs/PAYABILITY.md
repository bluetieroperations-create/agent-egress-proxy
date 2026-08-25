# The state of x402 payability

**44% of x402 endpoints advertise a price no client can read.**

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

## What we cannot say

We do not know *why* the 86 are opaque. Candidates: a third carrier nobody has
implemented, a middlebox stripping the header, an incomplete server, or a deliberate
choice to gate discovery. **Nobody has inspected more than a sample by hand**, and
this survey classifies rather than diagnoses. That is the obvious next piece of work
and we have not done it.

The sample is also not the whole ecosystem — it is the hosts reachable from our crawl
of the CDP Bazaar and public discovery documents, not a census.

## Reproducing

```sh
python directory_liveness.py --directory data/directory.json --out data/liveness.json
```

Pure helpers with injected network; `rank_leads` is prioritisation only and never
touches a verdict. See `docs/DIRECTORY_LIVENESS.md` for the classifier's internals.
