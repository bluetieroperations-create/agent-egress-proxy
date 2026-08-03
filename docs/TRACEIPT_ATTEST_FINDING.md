# Finding: Traceipt `/attest` drops paid attestations on restart (settled-but-not-delivered)

**Date:** 2026-08-03
**Environment:** Traceipt live API `https://api.traceipt.xyz`, x402 on Base Sepolia (`eip155:84532`)
**Payer:** funded testnet burner `0x7Fe6…9794` → payTo `0x3ec5e0ec1e1cb8e2afa36f3b40eed9057d9004e1`
**Severity:** High (paid-for deliverable lost) — testnet only, no real funds at risk
**Status:** Reproduced; reported here. This is a defect in the *external* Traceipt service, not in Blackwall.

## Summary

We made **five** real paid x402 `POST /attest` calls, each anchoring a distinct Blackwall
verdict digest, and deliberately did **not** call `/anchor` — the goal was to confirm that
Traceipt seals pending attestations into a Merkle batch **on its own**.

It does not batch them. When we polled the five proof endpoints:

- **4 of 5** attestations (the older ones) returned **`404 unknown attestation_id`** — they
  had previously returned `409 not yet anchored`, i.e. they were accepted-and-pending, then
  **vanished**.
- **1 of 5** (the most recent) sealed — but into a **size-1 tree** (`tree_size: 1`,
  `audit_path: []`, `anchor_id: 1`), i.e. it was anchored **alone**, not aggregated with the
  other four.

Each `/attest` had already **taken payment** (0.002 USDC settled on-chain via EIP-3009). So
four payments settled but bought an attestation that is now **unrecoverable**. The payment
moved; the receipt disappeared.

## Evidence

Five paid calls, all `201 {ok:true, status:"pending"}` at creation, each a distinct digest
(proving no idempotent replay — a changed `receipt_id` flows through the canonical JSON to a
new `sha256`):

| # | receipt_id | attestation_id | verdict_digest | proof at poll time |
|---|---|---|---|---|
| 1 | bw_sanctions_demo_0001 | `att_6d7f255926c6db8fdcc3` | `sha256:1f3e3a2f…` | **404 unknown** |
| 2 | bw_sanctions_demo_0002 | `att_1c2e3f4feaa9f6ad1fa3` | `sha256:02fe1045…` | **404 unknown** |
| 3 | bw_sanctions_demo_0003 | `att_12d7e26225751cabc3eb` | `sha256:aa3d022e…` | **404 unknown** |
| 4 | bw_sanctions_demo_0004 | `att_705f7f65211f6eda5192` | `sha256:1c69a088…` | **404 unknown** |
| 5 | bw_sanctions_demo_0005 | `att_452739d4d1db99521d23` | `sha256:46dbf0f7…` | **200 sealed** |

The verdict anchored (identical across all five except `receipt_id`) was a genuine Blackwall
STOP for an OFAC-listed counterparty:

```json
{"amount":"2.50","asset":"USDC","chain":"base",
 "counterparty":"0x8589427373D6D84E98730D7795D8f6f8731FDA16",
 "hard_stop":true,
 "reasons":["counterparty is on a sanctions list",
            "counterparty has 0 prior settlements, 0.0% dispute rate",
            "no price history for this counterparty/resource -- price anomaly unknown"],
 "receipt_id":"bw_sanctions_demo_0005","score":0.0,"verdict":"STOP"}
```

Sealed proof for #5 (the only survivor):

```json
{
  "attestation_id": "att_452739d4d1db99521d23",
  "anchor_id": 1,
  "leaf_index": 0,
  "tree_size": 1,
  "leaf_data": "sha256:46dbf0f74410520340db947d0c20dfb23d8efd2da3a69e06cd7ec9f1382f673b",
  "audit_path": [],
  "root": "0205e6e2bf9e7961f7b241052f186203fe4db26f1069efcecd7f37b7251d5b85",
  "onchain_network": "base-sepolia",
  "onchain_tx": "0x1a9b1db1992d157ce1e0da6dc30d854fd0eaa99a524a1862b7838ba960848010",
  "anchored_at": "2026-08-03T02:18:23Z"
}
```

The `404` on #1 was re-polled twice and is stable — not a transient read-your-writes race.

## Root-cause reading

The state transition we observed is: `pending (409)` → `gone (404)`, with `anchor_id`
reset to `1` and the sole survivor sealed into a fresh size-1 tree. That signature is
consistent with:

- **A non-durable pending queue.** Pending attestations are held **in memory**, not
  persisted. A process restart (deploy, crash, idle spin-down on a free tier) drops the whole
  queue. Only attestations created **after** the restart survive, and the Merkle counter
  restarts from 1.
- **Seal-per-request, not seal-per-batch.** `tree_size: 1` + empty `audit_path` means the
  self-seal anchored a single leaf rather than aggregating the outstanding pending set. Real
  batching would show `tree_size ≥ 2` and a non-empty `audit_path`.

Either way, the **money side is durable (on-chain settlement) but the deliverable side is
not** — the worst possible split for a paid API.

## Why this matters for Blackwall (this is our thesis, live)

This is a textbook instance of the gap Blackwall exists to catch: **payment settled ≠ service
delivered.** The x402 payment confirmed on-chain; the thing it bought (a resolvable, batched
proof) did not materialize. A naive integration that treats "payment settled" as success
would score all five as good outcomes. The truth is four `underdelivered`.

Concrete implications for our own code:

1. **`traceipt_ingest` / `traceipt_pull` must gate on a *resolvable, sealed proof*, not on the
   `201 {status:"pending"}` acknowledgement.** A pending attestation that can later 404 must
   never be ingested as a confirmed receipt. Our `traceipt_verify` fail-closed posture is
   correct here — but we should confirm the ingest path keys off proof resolution, not the
   POST response.
2. **`traceipt_attest.anchor_verdict` should expose a settled-but-unconfirmed state.** It
   returns `{ok:true, attestation_id, status:"pending"}` today. Callers cannot distinguish
   "anchored" from "paid, receipt pending, may evaporate." A follow-up proof poll (with the
   404→lost transition treated as `underdelivered`) closes the loop.
3. **Outcome mapping:** in the verdict→outcome→reputation flywheel, a Traceipt payee whose
   attestations settle-then-404 should accrue `underdelivered`/`disputed` outcomes, which is
   exactly the `going_bad` signal — the payee took the money and didn't deliver.

## Cost

5 × 0.002 USDC = **0.010 USDC**, testnet — no real loss. Four of those five bought a
deliverable that is now unrecoverable. On **mainnet** the same behavior is paid-and-lost.

## Recommendations

**To Traceipt (external):**
- Persist the pending-attestation queue so a restart does not drop paid, accepted work.
- Seal outstanding pending attestations as a real batch (`tree_size ≥ 2`), or document that
  `/attest` self-seals one-per-request.
- Consider returning payment/settlement identifiers in the `201` so a payer can independently
  reconcile "I paid" against "my attestation resolved."

**To us (Blackwall integration):**
- Do **not** queue multiple `/attest` calls against the live service — the queue is not
  durable. Anchor one, poll its proof to a sealed `200`, then proceed.
- Add a regression/integration note in `traceipt_pull` / `traceipt_ingest`: ingest only on a
  sealed proof; treat a prior-pending id that returns 404 as `underdelivered`.
- (Optional) a small `poll_proof(attestation_id)` helper in `traceipt_attest.py` that
  classifies `200 sealed` / `409 pending` / `404 lost`, so the anchor path can confirm
  delivery rather than stopping at the payment ack.

## Reproduction

```sh
# One paid anchor (testnet burner in SIGNER_PRIVATE_KEY), then poll its proof.
SSL_CERT_FILE=/root/.ccr/ca-bundle.crt SIGNER_PRIVATE_KEY=0x<burner> \
python3 clients/traceipt_anchor.py \
  --base-url https://api.traceipt.xyz --network base-sepolia \
  --max-atomic 1000000 \
  --verdict '{"verdict":"STOP","score":0.0,"receipt_id":"bw_sanctions_demo_XXXX", ...}'
# then: GET https://api.traceipt.xyz/attest/<attestation_id>/proof
#   200 sealed  -> delivered
#   409 pending -> not yet
#   404 unknown -> LOST (paid, dropped)  <-- the bug
```
