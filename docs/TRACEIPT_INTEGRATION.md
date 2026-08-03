# Blackwall × Traceipt

Blackwall and [Traceipt](https://traceipt.xyz) are two halves of one payment
lifecycle:

```
   BEFORE signing                          AFTER settling
  ┌──────────────┐                       ┌──────────────┐
  │  BLACKWALL   │   agent pays on GO    │   TRACEIPT   │
  │  the VERDICT │ ────────────────────► │  the RECEIPT │
  │ GO/HOLD/STOP │                       │ signed proof │
  └──────────────┘                       │  of what     │
         ▲                               │  settled     │
         │                               └──────┬───────┘
         └───────── feeds reputation ───────────┘
                (receipt → better next verdict)
```

Blackwall answers *"should I sign this?"* **before** payment. Traceipt issues a
signed, on-chain-verified *"this is what actually settled"* **after**. They wire
together in **two directions**, with opposite failure postures on purpose.

---

## Direction A — anchor the verdict (Blackwall → Traceipt)

Anchor a verdict's canonical digest in Traceipt's RFC-6962 Merkle log so
"Blackwall issued verdict X at time T" becomes a trustless, time-stamped proof —
**a proof, never the data** (only a `sha256` of the verdict; the private reputation
corpus never leaves). Useful for disputes, audits, and compliance.

- **Module:** `traceipt_attest.py` (stdlib, key-free) — `verdict_digest()`,
  `anchor_verdict(base_url, verdict, pay=…, max_amount_atomic=…)`.
- **Client:** `clients/traceipt_anchor.py` — the funded real-path runner (signs the
  x402 payment with `eth-account`; the one dep boundary).
- **Posture:** **FAIL-OPEN** — anchoring is best-effort and must never delay or break
  a verdict. Any error returns a benign dict, never raises.
- **Payment:** `/attest` is x402-paid (~0.002 USDC). Auto-pay is **spend-capped**
  (`max_amount_atomic`, default $1.00) so a spoofed challenge can't drain the signer.

### Optional: auto-anchor from the running server

Set `BLACKWALL_ANCHOR=1` and the verdict server anchors **every** verdict's digest
automatically (`verdict_anchor.py`). Three invariants:

1. **NON-BLOCKING** — the anchor POST runs on a background daemon thread *after* the
   response is written. It can never add latency to, block, or fail a verdict. (No
   network on the hot path — the lesson from the lazy-ingest 502.)
2. **FAIL-OPEN** — offline, 402, missing signer: all swallowed and logged.
3. **KEY-FREE CORE** — the stdlib verdict engine never touches a private key. The
   x402 signer is loaded lazily from `SIGNER_PRIVATE_KEY` via `clients/x402_pay`
   **only** when you opt in; without a key the anchor 402s and fails open, unsigned.

The anchored digest is a **tokenless projection** of the response — `report_token`
(a secret that authorizes outcome reporting) is stripped so the proof stays
reproducible by anyone holding the verdict, while `receipt_id` (the public handle)
is kept.

| env | default | meaning |
|---|---|---|
| `BLACKWALL_ANCHOR` | off | `1/true/yes/on` enables auto-anchor |
| `BLACKWALL_ANCHOR_URL` | `https://api.traceipt.xyz` | Traceipt base URL |
| `BLACKWALL_ANCHOR_MAX_ATOMIC` | `1000000` ($1.00) | refuse to auto-pay above this |
| `SIGNER_PRIVATE_KEY` | — | funded throwaway key that pays `/attest` |
| `BLACKWALL_ANCHOR_NETWORK` | `base-sepolia` | x402 network for the payment |

**OFF by default**, and you should keep it off unless you want a per-verdict audit
trail — it costs ~0.002 USDC per verdict and is subject to the reliability caveat
below.

### Confirming delivery (don't trust the 201)

A `POST /attest` `201` means **accepted**, not **delivered**. The attestation sits
PENDING until Traceipt seals it into a Merkle batch. Confirm out-of-band:

```python
from traceipt_attest import poll_proof, PROOF_SEALED, PROOF_LOST
r = poll_proof("https://api.traceipt.xyz", attestation_id, attempts=6, interval=10)
# r["state"]: "sealed" (delivered, carries root + onchain_tx)
#           | "pending" (still batching)
#           | "lost"   (404 -- PAID-BUT-DROPPED; treat as underdelivered)
```

`proof_status()` classifies a single `GET /attest/{id}/proof`; `poll_proof()` loops
until a terminal state (sealed or lost) or attempts run out. Both fail-open.

---

## Direction B — ingest receipts (Traceipt → Blackwall)

Feed Traceipt's signed, on-chain-verified settlement receipts into Blackwall's
reputation store. This compounds the flywheel (verdict → payment → receipt → better
next verdict) with a **fraud-resistant** signal: Traceipt already verified the
settlement on-chain and bound payer/payee/amount.

- **Modules:** `traceipt_pull.py` (pull receipt envelopes by id) →
  `traceipt_verify.py` (pure-Python Ed25519 verify against Traceipt's JWKS) →
  `traceipt_ingest.py` (map authenticated receipts to reputation transfers).
- **Posture:** **FAIL-CLOSED** — a receipt whose signature doesn't verify is
  **dropped, never ingested**. A forged `verified: true` can't poison reputation
  because authenticity is checked here, not trusted from the receipt.
- **Guards:** only `kind == "payment"` + on-chain-`verified` receipts; amounts
  normalized to token decimals; addresses lowercased to match the verdict key.
- **Delivery guard (from the finding below):** ingest only on a **sealed** proof;
  treat a prior-pending id that returns 404 as `underdelivered`, not `settled`.

```sh
python traceipt_pull.py --base-url https://api.traceipt.xyz \
    --store rep.db --ids-file my_receipt_ids.txt
```

---

## The security asymmetry (the design point)

| | Direction | Failure mode | Why |
|---|---|---|---|
| **Anchor** (A) | Blackwall → Traceipt | **FAIL-OPEN** | just an audit stamp; must never block a verdict |
| **Ingest** (B) | Traceipt → Blackwall | **FAIL-CLOSED** | a forged receipt would corrupt the verdict; unverified = dropped |

The path that *affects the verdict* is paranoid; the path that's *only an audit
stamp* never gets in the way.

---

## What the customer sees

- **A paying agent (core customer):** nothing new to integrate. Same GO/HOLD/STOP
  call, same response bytes. Traceipt sits behind the scenes.
- **An operator:** opt-in. `BLACKWALL_ANCHOR=1` for the audit trail; `traceipt_pull`
  for the reputation flywheel. Neither is on the `/v1/forecast-payment` hot path.
- **A compliance/audit consumer:** can independently verify an anchored digest
  against Traceipt's Merkle root — no trust in Blackwall required.

---

## ⚠️ Reliability caveat (Traceipt-side)

See [`TRACEIPT_ATTEST_FINDING.md`](TRACEIPT_ATTEST_FINDING.md). In live testing,
Traceipt **dropped pending attestations on restart** (409 → 404) while the x402
payment had already settled, and sealed one attestation per batch instead of
aggregating. Until Traceipt persists its pending queue:

- Don't queue several `/attest` calls — anchor one, `poll_proof` it to `sealed`,
  then proceed.
- Treat Direction A as best-effort; **confirm delivery**, don't trust the 201.
- Direction B is unaffected (its fail-closed Ed25519 verification is independent),
  but it should ingest only on a resolvable sealed proof.
