# Blackwall — live end-to-end proof (against real infrastructure)

_Run 2026-07-30._ Not unit tests — the actual code paths exercised against **live**
services, so a pitch can say "it works end-to-end on real infra," not just "it
passes tests."

## Summary

| What | Against | Result |
|---|---|---|
| **Packaged LangChain plugin** runs standalone | isolated install, no engine repo | ✅ `pip`-installed wheel; real `BlackwallPaymentGuardTool` returned a STOP over `HttpEngine` |
| **Integration A** — anchor a verdict | **live** `api.traceipt.xyz/attest` | ✅ real digest computed; endpoint returned a genuine x402 `402` (`payment_required`) — A speaks the live contract and fails open |
| **Integration B** — fetch + verify | **live** `api.traceipt.xyz/jwks.json` | ✅ real Ed25519 JWKS fetched (`kid=c6af05ad9ba858e4`); key decodes with our pure-Python verifier; a **forged** receipt under that live kid is **rejected** |
| **Payload-sim crypto** | `eth-account` / `eth_abi` | ✅ (earlier this session) 200/200 real signatures agreed; userOpHash matches `eth_abi` |

## Details

### Packaged plugin, standalone
Built `blackwall-langchain` (8.5 KB wheel), installed into an isolated target with
the engine repo **off** the path, and drove the real LangChain tool over HTTP:

```
VERDICT: STOP (BLOCK). STOP -- do NOT sign this payment. Reasons: counterparty is on a sanctions list
```

### A — live `/attest`
```
anchor_verdict("https://api.traceipt.xyz", {"verdict":"STOP", ...})
-> {"ok": false,
    "digest": "sha256:b841818983c3d15f6641f89171273ca4c085e0fbe2100f99341cdad480e8a07c",
    "reason": "payment_required"}     # a REAL x402 402 from the live endpoint
```
Anchoring is fail-open, so the 402 (no payment attached) is the correct outcome:
the digest is computed and the live endpoint's x402 gate is spoken correctly. A
funded run (`clients/traceipt_anchor.py`) would settle and anchor.

### B — live JWKS + authenticity gate
```
fetch_jwks("https://api.traceipt.xyz")  -> {"keys":[{"kty":"OKP","crv":"Ed25519",
                                             "kid":"c6af05ad9ba858e4","x":"3JFI...zEI", ...}]}
pure-Python verifier: live pubkey decodes to a valid 32-byte Ed25519 point  ✅
forged receipt claiming that kid  -> verify_envelope(...) is None (REJECTED)  ✅
```

### Live service state (honest)
`GET /health` → `{"ok":true,"gate":"facilitator","chain":"base-sepolia"}` and
`GET /anchors` → `{"anchors":[]}`. The service is **live and facilitator-gated on
Base Sepolia, but has no receipts/anchors yet** — so a genuinely-signed receipt
couldn't be pulled and verified end-to-end. That's the expected "no traffic yet"
state, not a gap in the code: the pull/verify pipeline ran against the real JWKS and
the security gate held. A full A→settle→B round-trip needs (a) a funded key for
`/attest` and (b) real receipt volume.

### Reputation backfill — live, from public Base data (no customers)
`chain_backfill.py` seeds counterparty reputation from public on-chain USDC history.
Live run against `base.blockscout.com`:
```
picked a real Base USDC payee (0xe903...abf) from the ecosystem feed
backfill -> fetched 150 real inbound USDC transfers (3 pages), ingested 150
seeded reputation -> settlement_count=150, distinct_payers=2
```
`distinct_payers=2` is the point: this address is paid by only two parties, so the
Sybil/wash-trade gate would **not** auto-GO it — the defense works on real data. This
is how the moat is fed **before customer #1**: point it at the payee addresses you
care about (x402 endpoints' `payTo`) and it accumulates their public settlement
history. Depth of dispute/outcome signal still comes from Traceipt receipts + the
verdict flywheel.

### Discovery crawl — live CDP x402 Bazaar (self-populates the payee list)
`discovery_crawl.py` walks Coinbase's public x402 Bazaar out of the box. Live run:
```
GET api.cdp.coinbase.com/platform/v2/x402/discovery/resources  -> pagination.total = 14,802
crawl 3 pages -> 514 resource records, 103 DISTINCT payees, 511 advertised prices
  (real price distribution: $0.0001 .. $0.0024+, e.g. api.onesource.io @ $0.001)
```
So `discovery_crawl --backfill-store rep.db` = the whole cold-start loop with zero
customers: enumerate ~15k x402 endpoints from the Bazaar -> their payees -> seed
reputation from public Base history; the advertised prices seed peer baselines.

## Reproduce

Python's `urllib` must trust the agent proxy's CA to reach external HTTPS:

```sh
export SSL_CERT_FILE=/root/.ccr/ca-bundle.crt      # proxy CA (env-specific)
python3 -c "import traceipt_verify as V; print(V.fetch_jwks('https://api.traceipt.xyz'))"
python3 -c "import traceipt_attest as A; print(A.anchor_verdict('https://api.traceipt.xyz', {'verdict':'STOP'}))"
```

## What this does and doesn't prove

- **Does:** the integration code speaks the live contracts (x402 `/attest`, JWKS);
  the packaged plugin installs and runs standalone; the authenticity gate rejects
  forgeries against the real key; the crypto matches real Ethereum tooling.
- **Doesn't:** that agents will *use* it or *pay* for it (that's `VALIDATION.md`),
  nor a funded A→B round-trip (needs funds + real receipts). The on-chain AA
  validator remains unbuilt (`docs/AA_COSIGNING.md`).
