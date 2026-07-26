# Compliance context for Traceipt

> **This is engineering documentation, not legal advice.** Whether a given
> deployment is in scope of the regulations below — and whether these
> receipts satisfy a specific obligation — is a determination for qualified
> counsel. What this document does is map receipt fields to the *technical
> capabilities* those regulations demand, so that conversation with counsel
> is short.

## Why these two regulations

Both were cited by ecosystem builders (see
[x402#2357](https://github.com/x402-foundation/x402/issues/2357)) as the
reason the bare x402 `PAYMENT-RESPONSE` is insufficient for regulated
deployments: counterparties need independently verifiable records without
calling back into the facilitator's infrastructure.

## EU AI Act — Article 12 (record-keeping)

Article 12 requires that high-risk AI systems **technically allow for the
automatic recording of events (logs) over the system's lifetime**, to ensure
a level of **traceability** of the system's functioning appropriate to its
intended purpose.

An autonomous agent that spends money is, for its operator, an event source
whose actions need exactly that traceability. Mapping:

| Article 12 capability | Receipt mechanism |
|---|---|
| Automatic recording of events | A receipt is generated per settlement, machine-to-machine, no human step |
| Traceability of each event | `settlement.tx_hash` links to the on-chain transfer; `commerce.resource` + `request_hash`/`response_hash` link to what was bought and delivered |
| Records that cover the lifetime | Per-seller dense `sequence` + `prev_receipt_hash` chain — gaps and deletions are detectable, not silent |
| Integrity of the record | Ed25519 signature over canonical JSON; altering a receipt invalidates its signature, and the chain-verification path checks every receipt's signature (not just its hash) |
| Independent verifiability | `GET /jwks.json` + any JOSE library verifies offline; no dependency on this service being reachable |

**Honest limits:** Article 12 concerns the *system's* logging capability
broadly — receipts cover the *payment* events, not every internal system
event. Receipts are one component of an Article 12 posture, not the whole of
it. Whether a given agent is "high-risk" under the Act is a legal
determination. The record's integrity rests on the **signature**: the keyless
hash chain alone catches a naive row edit but not a rehash-consistent
rewrite, so integrity claims assume the signature check is run (it is, on the
`/verify` and `/chain` paths) and that the issuer key is not compromised.
Crucially, a receipt attests *that a settlement occurred and how it was
labelled* — in v0.1's seller-hosted mode it is bound to the operator's own
receiving address, but it does **not yet** cryptographically prove the
*payer's* identity (see the README threat model).

## MiCA — record-keeping (Article 76 and related CASP duties)

MiCA's Article 76 governs the **operation of trading platforms** by
crypto-asset service providers, including keeping records of orders and
their **link to executed transactions**, retained for years and usable by
supervisors. Related CASP duties require records of services and
transactions sufficient for supervision.

Mapping, *for parties in scope of MiCA* (e.g. platforms or service providers
in the payment path):

| Record-keeping need | Receipt mechanism |
|---|---|
| Link a commercial event to its executed transaction | The receipt is precisely that binding: quoted price + resource ↔ settled on-chain transfer, in one signed object (the transfer's existence and, in seller-hosted mode, its payee are verified; the payer identity is not yet proven) |
| Sequential, complete records | Dense per-seller sequence numbers; omission or reordering is detectable, and the signature check makes payload tampering detectable |
| Multi-year retention | Receipts are small JSON documents with offline-verifiable signatures — retention is a storage decision, verification does not decay |
| Supervisor-usable | Human-readable JSON, exportable, verifiable with published keys |

**Honest limits:** most individual x402 API sellers are probably *not*
CASPs and not in Article 76 scope. The claim here is narrower: when a party
in the x402 payment path *does* have MiCA record-keeping duties (or wants
records of equivalent quality because their counterparties do), these
receipts provide the transaction-linked, tamper-evident record MiCA-grade
supervision expects.

## Also relevant, no legal claims made

* **VAT / invoicing rules** — EU sellers generally must issue invoices with
  sequential numbering and specified fields. The `sequence` mechanism and
  `commerce.seller_entity` (name, VAT id, country) carry the data; a
  rendered invoice document is on the roadmap.
* **Tax treatment of stablecoin disposals** — base-unit amounts with linked
  tx hashes give an accountant lot-level ground truth.

## The one-sentence positioning

x402 settles the money; **Traceipt produces the record a regulated
business needs to have kept** — automatically, per transaction, in a form
that is still verifiable years later without trusting anyone's database.
