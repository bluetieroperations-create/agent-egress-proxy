# Blackwall — pre-signature payment verdict (x402)

> **Status: build-order step 1 of the Blackwall × x402 spec.** Self-contained
> verdict endpoint with a **mocked** reputation source. Steps 2–6 (real data
> source, x402 billing handshake, MCP wrapper, directory listing, trust-graduation
> engine) are deferred — see [Deferred](#deferred-not-in-this-build) below.

Blackwall is the **action-layer** guardrail that pairs with this repo's
**network-layer** egress proxy. The proxy decides *where an agent can reach*;
Blackwall decides *whether an agent should sign a payment*. Two locks.

It sits in the **pre-signature window** of an [x402](https://www.x402.org) flow —
after the agent receives a `402 Payment Required`, before it signs:

```
Agent → GET /resource
Server → 402 { price, asset, chain, recipient }
                  │
                  ▼  POST /v1/forecast-payment   ← Blackwall
            GO ───┼─── HOLD ─── STOP
          sign &  │   defer to    do not
           pay    │  spending-cap  sign
                  ▼   layer
```

Blackwall **never touches funds** and is **not in the settlement path**. It
returns a verdict; the agent decides. (Verdict, not custody — the clean
regulatory posture.)

## Run it

```sh
python blackwall.py --port 8402        # binds 127.0.0.1 only (MOCK reputation)
```

**On REAL on-chain reputation** (not the mock) — point it at a SQLite store:

```sh
# pre-warm the store with real Base counterparty history (background, slow ok):
python reputation_store.py rep.db 0xCounterparty1 0xCounterparty2 ...

# serve verdicts from the real store (sub-ms hot-path reads):
python blackwall.py --port 8402 --store rep.db --ledger bw.jsonl

# or self-populate on first sight of a counterparty (first call slow, then cached):
python blackwall.py --port 8402 --store rep.db --ingest
```

With `--store` the source becomes the SQLite `ReputationStore` (on-chain
settlement breadth); add `--ledger` and it's `CombinedReputationSource`
(store + the ledger's observed disputes). Same `--store`/`--ingest` flags on
`mcp_server.py`.

```sh
curl -s http://127.0.0.1:8402/v1/forecast-payment \
  -H 'Content-Type: application/json' \
  -d '{"counterparty":"0xKNOWNGOOD000000000000000000000000000001",
       "amount":"0.09","asset":"USDC","chain":"base",
       "resource":"https://api.example.com/run"}'
```

```json
{"verdict":"GO","score":0.997,
 "reasons":["counterparty has 1240 prior settlements, 0.2% dispute rate",
            "quoted amount within 1.00x of the counterparty's median for this resource class"],
 "signals":{"counterparty_reputation":0.997,"price_anomaly":0.0,
            "reversibility":"irreversible","blast_radius":"bounded"},
 "receipt_id":"bw_..."}
```

## The verdict contract

**Request** `POST /v1/forecast-payment`:

| field         | required | notes                                            |
|---------------|----------|--------------------------------------------------|
| `counterparty`| yes      | wallet from the 402 `recipient` field            |
| `amount`      | yes      | **decimal string** (`"0.09"`); floats rejected   |
| `asset`       | yes      | e.g. `USDC`                                       |
| `chain`       | yes      | e.g. `base`                                       |
| `agent_id`    | no       | DID / ERC-8004 identity of the caller             |
| `payer`       | no       | agent's on-chain wallet (the x402 signer); validated as a real EVM address, **binds settlement confirmation to this agent** |
| `resource`    | no       | what's being paid for                             |
| `context`     | no       | `{quoted_price_history:[...], expected_recipient}`|

**Response:** `verdict` (`GO`/`HOLD`/`STOP`), `score` (Bayesian trust, 0–1),
`reasons[]`, `signals{counterparty_reputation, price_anomaly, reversibility,
blast_radius}`, `receipt_id` (HMAC-signed, for the agent's audit trail).

## How the verdict is reached

The decision boundary is five **pure, unit-tested** functions (the analogue of
the proxy's `parse_connect_target`/`host_allowed`/`decide`):

- **`reputation_score`** — Bayesian `Beta(good+1, bad+1)` posterior mean from
  settlement count + dispute rate. A no-history wallet scores `0.5` (the prior),
  not `1.0`: *no evidence is not trust*.
- **`price_anomaly_ratio` / `anomaly_score`** — `amount` vs the counterparty's
  **own** median price for this resource class. Only overcharge counts; a
  discount is never anomalous. No history → `None` (UNKNOWN), kept distinct from
  "fine".
- **`decide_payment`** — composes them into GO / HOLD / STOP.

| verdict  | when                                                                                 |
|----------|--------------------------------------------------------------------------------------|
| **GO**   | reputable (≥ `0.70`), not thin (≥ `20` settlements), price within norms, in budget   |
| **HOLD** | resolvable: amount over the auto-approve threshold, thin counterparty, or price anomalous → hand to the spending-cap / slow-escalation layer |
| **STOP** | severe & unambiguous: sanctioned / known-bad counterparty, recipient ≠ the 402's recipient, or price ≥ `8×` the counterparty's own median |

Thresholds live as named constants at the top of `blackwall.py`; the tests pin
them, so changing one flips a named test (mutation-checked, like the proxy).

## Tests

```sh
python -m unittest test_blackwall.py -v
```

## The moat flywheel (data accumulation)

The data-source spike found that the durable moat isn't data Blackwall can *read*
from a public indexer — it's data Blackwall *accumulates*. The
**verdict→outcome ledger** (`ledger.py`) is the write path that closes the loop:

```
forecast() ──writes──▶ VERDICT event ──┐
                                        │  (settlement watch / agent report)
LedgerReputationSource.lookup() ◀──aggregate──  OUTCOME event  (joined by receipt_id)
        │
        └──feeds──▶ next forecast()
```

Each verdict is logged with its signed `receipt_id`. The agent (or, later, an
on-chain settlement watcher) reports what the payment actually did via
`POST /v1/report-outcome`, keyed by that receipt. `aggregate_counterparties`
folds the stream into per-counterparty records — `settlement_count`,
**`dispute_rate` (the signal that is NOT on-chain)**, price history — which
`LedgerReputationSource` serves back through the same `lookup()` seam. A fresh
counterparty HOLDs; after enough clean settlements it graduates to GO
(`test_ledger.py::TestFlywheel`). `dispute_rate` stays `None` (UNKNOWN) until an
outcome is actually observed — never a fabricated 0.

`ChainedReputationSource` lets the ledger lead and an on-chain/bootstrap source
fill the cold-start gap.

**Trustless settlement confirmation** (`settlement_watch.py`) closes the loop
*without agent goodwill* and fixes the unauthenticated-report hole for the one
fact that lives on-chain: it reads Base and writes a **chain-confirmed**
`settled` outcome (`source="chain-watch"`) only when a real USDC transfer of the
right amount reached the counterparty — by independent tx verification or a
zero-cooperation scan. USDC is identified by **contract** (`token.address_hash`),
never the spoofable `symbol`; one on-chain tx confirms at most one receipt; and
the record exposes `_meta.chain_confirmed_settlements` so a GO policy can gate on
trustless settlements, not self-reports.

**Other accumulation taps** (don't all need agent cooperation):

| Tap | Harvests | Cooperation |
|-----|----------|-------------|
| **402 quote corpus** | server's declared price/recipient per resource — price-norms from the quotes themselves | none, every call |
| **On-chain settlement watch** | confirms paid / amount / timing after a GO | none |
| **Egress proxy (sibling product)** | which endpoints an agent reaches *before* paying | none (if both used) |
| **Verdict→outcome ledger** | dispute/underdelivery — the moat signal | partial |
| **MCP distribution** | counterparty/amount/resource on every decision | none, once adopted |
| **Facilitator partnership** | settlement confirmations for everyone | relationship |
| **OFAC / scam-list feeds** | sanctions & known-bad bootstrap | none |

## Billing — Blackwall is itself an x402 resource (`x402.py`)

Opt-in (`--pay-to <wallet> [--price 0.001]`). The service that judges x402
payments is itself paid via x402:

```
Agent → POST /v1/forecast-payment                 (no payment)
Blackwall → 402 { accepts:[{maxAmountRequired, asset, network, payTo}] }
Agent → retry with  X-PAYMENT: <base64 payload>
Blackwall → match → facilitator.verify → reserve nonce → facilitator.settle
          → serve verdict (+ settlement tx)
```

**Division of labor (spec §5.4):** the **facilitator** does signature + balance +
on-chain replay + settlement and sits behind the `Facilitator` seam
(`MockFacilitator` here; `HttpFacilitator` is the real-deployment stub — there's
no live facilitator in this environment). Blackwall owns the **protocol
envelope**: the 402 challenge, matching the payment to the requirements (right
`payTo`/asset/network/amount), **local idempotency** (a nonce is reserved before
settling, so a replay can never double-settle; released if settlement fails so a
legit retry survives), and **sessions**.

**Sessions (fund-once, many checks).** `POST /v1/session` with a payment covering
the session price returns an HMAC-signed `session_token` good for N credits;
forecast calls then present `X-PAYMENT-SESSION` and skip per-call signing
(latency/cost answer from spec §5). Credits decrement under lock; expiry enforced.

Pricing default is `0.001` USDC/call (sub-cent, the spec's per-check ceiling); a
session is `price × credits` (no bulk discount yet).

## MCP server (`mcp_server.py`)

Wraps the verdict engine as a Model Context Protocol server over stdio, so any
MCP-capable agent discovers and calls Blackwall self-serve. A thin transport
wrapper — the tools delegate straight to `forecast()` / `record_outcome()`.

```sh
python mcp_server.py [--ledger blackwall_ledger.jsonl]
```

Tools: **`forecast_payment`** (GO/HOLD/STOP + signed receipt) and, with a ledger,
**`report_outcome`**. Stdlib-only — a minimal JSON-RPC 2.0 loop (no `mcp` SDK),
`initialize` / `tools/list` / `tools/call` / `ping`; the `handle()` dispatch core
is pure and unit-tested, and the real stdio path is exercised via subprocess.
Logs go to **stderr** so stdout stays clean JSON-RPC. MCP stdio is the local
self-serve interface and is **unbilled**; monetized/remote access is the x402
HTTP endpoints.

## Known limitations (eval notes)

- **No price history → GO on small amounts.** A reputable counterparty with no
  recorded price history for a resource class gets `price_anomaly = 0` (UNKNOWN,
  surfaced as a reason) and can GO. The exposure is **bounded by the budget
  gate**: anything above `HOLD_AMOUNT_THRESHOLD` with no history is HOLD. This is
  a deliberate noise-vs-safety tradeoff — HOLDing every first-seen price would
  make the tool noisy and push agents to route around it (spec's high-volume-GO
  requirement). Step 2's real data source narrows the no-history window.
- **`score` is trust, not verdict confidence.** A HOLD can carry a high `score`
  (e.g. reputable counterparty, amount merely over the auto-approve threshold).
  Consumers should branch on `verdict`, using `score`/`signals` for their own
  logic — not infer the verdict from the score.
- **`Content-Length`-only body read.** The MVP server reads the body by
  `Content-Length`; chunked `Transfer-Encoding` is not parsed. Fine for the
  localhost/agent path; revisit if exposed behind a proxy.
- **Outcome-report trust model (chain-confirmed anchored).** Every
  verdict-affecting ledger signal — `settlement_count`, `confirmed_settlement_count`,
  `dispute_rate`, `price_history` — derives **only from chain-confirmed settlements**
  (`source="chain-watch"`, written by `settlement_watch` after on-chain
  verification, deduped by `settlement_tx`, payer-bound). A self-report on a
  receipt that never settled on-chain is **advisory only** (`_meta.advisory_self_reports`)
  and moves nothing. This closes the self-report poisoning channels found in the
  full audit (verified): you can't drag a counterparty GO→HOLD with fake
  `disputed` reports, can't poison `price_history` with a fake `observed_amount`,
  can't inflate the confirmed count by replaying one tx across N receipts, and a
  self-report can't mask a downstream `sanctioned` flag. Among confirmed
  settlements, the payer's latest delivery report sets quality (you can only
  dispute a payment that really happened); chain-confirmation is **sticky** (a
  later self-report can't erase it). Reports are also authenticated by a
  `report_token` (HMAC capability returned with the verdict).
  - **Residual (Sybil / wash-trading — design limit, documented).** On-chain
    settlement *count* — whether from the indexed store or chain-watch — is
    **wash-tradeable**: an attacker who controls both wallets can pay themselves
    real USDC to manufacture settlement history (the payer-binding doesn't help
    when the attacker owns the payer). So raw confirmed-count is not Sybil-proof;
    the real defenses are **counterparty diversity** (many distinct payers),
    amount floors, and dispute/age weighting — future work, not a code one-liner.
    Treat confirmed-count as *necessary, not sufficient* for trust. Delivery
    `disputed` on a real settlement still biases toward HOLD (griefing), never an
    unsafe GO. A payer-less verdict uses the weaker recipient+amount match.
- **Re-aggregates the whole ledger per lookup.** `LedgerReputationSource` folds
  every event on each `lookup()`, and `price_history` is unbounded. Fine at
  scaffold scale; production keeps a rolling in-memory aggregate updated on
  append and caps/decays history.
- **Billing: facilitator is mocked; nonce ledger is unbounded; price discovery
  needs a valid body.** `MockFacilitator` always approves — a real deployment
  points `HttpFacilitator` at an x402 facilitator's `/verify` + `/settle`. The
  `NonceLedger` grows without eviction (production should evict on the payment's
  `validBefore` expiry). The 402 challenge is returned only for a well-formed
  forecast request body, so price discovery requires a valid request, not an
  empty probe.

## Deferred (NOT in this build)

Per the spec's build order, on purpose:

2. **Real counterparty-history source** — `MockReputationSource` is the seam;
   `reputation_onchain.OnchainReputationSource` is a live Base-backed drop-in
   spike. Feasibility answered in [docs/DATA_SOURCE_SPIKE.md](docs/DATA_SOURCE_SPIKE.md):
   data is reachable no-key, but a free indexer is too slow for the hot path and
   the dispute/moat signal isn't on-chain — Blackwall needs its own indexed store
   + outcome ledger.
3. ~~**x402 billing handshake**~~ — **BUILT** (`x402.py`); see "Billing" below.
4. ~~**MCP server wrapper**~~ — **BUILT** (`mcp_server.py`); see "MCP" below.
5. ~~**Directory listing**~~ — **BUILT** (`discovery.py`, `DISCOVERY.md`):
   `GET /.well-known/x402` service card + awesome-x402 submission entry.
6. ~~**Self-learned trust-graduation engine**~~ — **BUILT**: the verdict→outcome
   flywheel (`ledger.py`) + the autonomous settlement watcher (`settlement_watch.py`)
   + the indexed reputation store (`reputation_store.py`, sub-ms hot-path reads).
   `CombinedReputationSource` fuses on-chain settlement breadth (store) with the
   ledger's observed disputes into one verdict-driving record.

**Production data path** (replaces the mocks): `reputation_store.ReputationStore`
is a SQLite-indexed settlement store — background ingest from Base (slow ok),
**sub-millisecond hot-path `lookup()`** (vs 1–14 s for the direct indexer — see
the spike). It is the spike's recommended architecture, realized. The x402
billing now runs against a real facilitator over HTTP (`HttpFacilitator`,
`--facilitator <url>`), with `facilitator_sim.py` as a local reference; only the
production facilitator URL is credential-gated.

Also out of scope for v1 (per spec §7): escrow/custody, refund/dispute filing,
and sanctions screening as a standalone product (it's a STOP *signal* here, not
a separate build).
