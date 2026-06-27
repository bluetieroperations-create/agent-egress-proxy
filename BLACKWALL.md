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
python blackwall.py --port 8402        # binds 127.0.0.1 only
```

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
- **Outcome reports are unauthenticated (ledger-integrity risk).**
  `POST /v1/report-outcome` accepts any `receipt_id` + outcome from any caller,
  so a malicious reporter could inflate a counterparty's reputation (spam
  `delivered`) or tank a rival (spam `disputed`). The ledger is only as
  trustworthy as its outcome reports. Mitigations for production, in order:
  (1) the **autonomous on-chain settlement watcher** — corroborate `settled`
  from chain, not self-report (trustless); (2) require reports to be **signed by
  the original `agent_id`** and only accept the issuing agent's report for a
  receipt; (3) weight self-reported delivery vs. chain-confirmed settlement
  differently in scoring. Built now: replays are idempotent (one outcome per
  receipt, last-wins) and receipts are unique per payment — but authenticity is
  not yet enforced.
- **Re-aggregates the whole ledger per lookup.** `LedgerReputationSource` folds
  every event on each `lookup()`, and `price_history` is unbounded. Fine at
  scaffold scale; production keeps a rolling in-memory aggregate updated on
  append and caps/decays history.

## Deferred (NOT in this build)

Per the spec's build order, on purpose:

2. **Real counterparty-history source** — `MockReputationSource` is the seam;
   `reputation_onchain.OnchainReputationSource` is a live Base-backed drop-in
   spike. Feasibility answered in [docs/DATA_SOURCE_SPIKE.md](docs/DATA_SOURCE_SPIKE.md):
   data is reachable no-key, but a free indexer is too slow for the hot path and
   the dispute/moat signal isn't on-chain — Blackwall needs its own indexed store
   + outcome ledger.
3. **x402 billing handshake** — Blackwall is itself an x402 resource (charges
   per forecast). Seam marked `TODO(step 3)` in `blackwall.py`.
4. **MCP server wrapper.**
5. **Directory listing** (awesome-x402 / x402 service discovery).
6. **Self-learned trust-graduation engine** — shrinks HOLD over time by
   graduating repeat-safe counterparties. **Scaffolded** in `ledger.py` (the
   verdict→outcome flywheel above); the production version keeps a rolling
   aggregate and adds the autonomous on-chain settlement watcher.

Also out of scope for v1 (per spec §7): escrow/custody, refund/dispute filing,
and sanctions screening as a standalone product (it's a STOP *signal* here, not
a separate build).
