# x402-receipts

**Signed, verifiable, chain-anchored receipts for x402 machine payments.**

Every x402 settlement moves money between machines and leaves nothing a
human can file: no invoice, no expense record, no proof of what was bought.
This service turns a settlement into a **cryptographically signed receipt**
that binds together:

1. **the on-chain fact** — the USDC transfer (chain, tx hash, payer, payee,
   amount), *verified against an RPC node before anything is issued*;
2. **the commercial context** — what was bought, from which endpoint, at what
   quoted price, with optional SHA-256 digests of the request and response
   (dispute evidence without storing anyone's data);
3. **its place in a tamper-evident ledger** — per-seller dense sequential
   numbering with a hash chain, so receipts can't be silently deleted,
   reordered, or back-dated.

The receipt itself is issued over x402: callers hit `POST /receipts`, get a
`402 Payment Required`, pay cents, and get their signed receipt — the service
eats its own dog food.

Python 3.8+; only non-stdlib dependency is [`cryptography`](https://pypi.org/project/cryptography/)
(Ed25519).

---

## Quick start

```sh
cd x402-receipts
pip install cryptography
python3 -m receipts.service --gate dev --settlement mock --chain base-sepolia
```

Issue a receipt (dev gate: any `X-PAYMENT` header is accepted; `--settlement
mock` skips RPC — both for local development only):

```sh
curl -s -X POST http://127.0.0.1:8402/receipts \
  -H 'X-PAYMENT: dev-token' -H 'Content-Type: application/json' -d '{
  "seller_id": "api.example.com",
  "settlement": {
    "chain": "base-sepolia",
    "tx_hash": "0xcccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "amount_base_units": "5000",
    "payer": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "payee": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "commerce": {
    "resource": "https://api.example.com/v1/lookup",
    "description": "domain reputation query",
    "quoted_amount_base_units": "5000"
  }
}'
```

The response contains the signed envelope and a `verify_url`. Then:

* `GET /verify/{receipt_id}` — re-checks the signature and the seller's whole
  chain, returns a PASS/FAIL report.
* `GET /receipts/{receipt_id}` — the raw signed envelope.
* `GET /chain/{seller_id}` — full-chain integrity check.
* `GET /jwks.json` — issuer public keys, for **offline** verification with any
  JOSE/Ed25519 library. No SDK required.

For real operation: `--settlement rpc` (the default) verifies the claimed
transfer against a Base RPC node (`--rpc-url` to override the public default)
and refuses to issue a receipt when the transaction is missing, reverted, or
doesn't contain a matching USDC `Transfer(payer → payee, amount)` log.

Amounts are **strings of base units** (USDC has 6 decimals: `"5000"` =
$0.005). Floats are rejected everywhere — see *Design choices*.

---

## Receipt format

```json
{
  "protected": {"alg": "EdDSA", "kid": "1c9f…", "typ": "x402-receipt+json"},
  "payload": {
    "receipt_id": "rcpt_9be51ff385fc77069cf1",
    "spec": "x402-receipt/v0.1",
    "seller_id": "api.example.com",
    "sequence": 42,
    "prev_receipt_hash": "sha256:…",
    "issued_at": "2026-07-26T12:00:00Z",
    "settlement": {
      "chain": "base-sepolia", "tx_hash": "0x…", "asset": "USDC",
      "asset_contract": "0x…", "amount_base_units": "5000",
      "payer": "0x…", "payee": "0x…",
      "verified": true, "verification_method": "rpc"
    },
    "commerce": {
      "resource": "https://api.example.com/v1/lookup",
      "description": "domain reputation query",
      "quoted_amount_base_units": "5000",
      "request_hash": "sha256:…", "response_hash": "sha256:…"
    }
  },
  "signature": "base64url(Ed25519 over canonical JSON of {payload, protected})"
}
```

## Design choices

* **Ed25519 via a JWS-style envelope** — verifiable offline in any language
  with the published JWKS; the `alg`/`kid` header is covered by the signature
  (no algorithm-substitution).
* **Canonical JSON, floats forbidden** — signatures and hash chains require
  byte-stable serialization; within our type domain (str/int/bool/null,
  sorted keys, no whitespace) the output matches RFC 8785. Money is integer
  base-unit strings, so float canonicalization never arises.
* **`receipt_id` is content-derived** — the same inputs always produce the
  same receipt; issuance is idempotent by construction.
* **One settlement, one receipt** — `(seller_id, tx_hash)` is unique in the
  ledger (enforced both in the issue path and by a DB constraint).
  Re-submitting a settlement returns the *original* receipt with HTTP 200
  and `"idempotent": true`; it can never mint a second receipt for the same
  payment, even with different commerce data attached. Issuance for a seller
  is serialized, so concurrent requests can't fork the chain.
* **Per-seller hash chain with dense sequence numbers** — sequential
  numbering (what invoice rules expect) *and* tamper-evidence in one
  mechanism. `verify_chain` re-derives every hash from stored envelopes, so
  a mutated database row is detected rather than trusted.
* **Settlement verified before issuance** — the receipt attests a checked
  on-chain fact, not a caller's claim. Mock mode exists for development and
  its receipts are permanently marked `verification_method: "mock"`.

## Security notes

* The issuer key is created `0600` on first run (`issuer_ed25519.pem`).
  In production, keep it in a KMS/HSM; the entire product is that key's
  trustworthiness.
* The service binds `127.0.0.1` and is intended to run behind a TLS reverse
  proxy in production.
* Request bodies are capped (64 KB); all inputs are validated by pure
  functions (`schema.py`) that are unit-tested first — same TDD-on-the-
  security-boundary approach as the egress proxy in this repository.

## Roadmap (deliberately not in v0.1)

* **Real x402 gate** — verify `X-PAYMENT` via a facilitator (Coinbase CDP /
  Cloudflare) instead of the dev gate.
* **Merkle batch anchoring** — publish a periodic Merkle root of issued
  receipts on-chain (certificate-transparency style) so existence-by-time is
  provable without trusting the issuer at all.
* **PDF/HTML rendering** — human-facing invoice documents (VAT fields,
  entity details) generated from the signed payload.
* **Alignment with the x402 receipt-extension discussion**
  ([x402#2357](https://github.com/x402-foundation/x402/issues/2357)) as it
  evolves.

## Compliance context

See [COMPLIANCE.md](COMPLIANCE.md) for how receipt fields map onto EU AI Act
Article 12 record-keeping and MiCA record-keeping duties — and, just as
importantly, what this service does **not** claim.

## Tests

```sh
python3 -m unittest test_receipts -v
```
