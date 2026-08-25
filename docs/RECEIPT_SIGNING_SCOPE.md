# Scope: verifiable receipts (Ed25519 now, ML-DSA hybrid later)

## The gap

Blackwall publicly advertises "an independently-verifiable Ed25519 signed receipt"
(awesome-x402) and "a third-party-verifiable Ed25519 signed receipt" (the remote
MCP tool description). Neither is true today.

What the engine actually returns:

```json
{"receipt_id": "bw_b91f275b7234cf2032370835",
 "report_token": "b17877047f484ed1137295f632e3bb5c"}
```

Both are HMAC-SHA256 (`sign_receipt`, `sign_report_token`). `grep -i ed25519
blackwall.py` returns **nothing**. The `sign_receipt` docstring is explicit:

> "Same verdict + same key -> same id (verifiable), so the agent can prove later
> exactly what Blackwall returned."

That is SYMMETRIC verification: it needs the secret. Only Blackwall can check it,
and anyone given the secret can forge. "Independently verifiable" is precisely
what an HMAC is not.

Related: `blackwall-mcp-remote/src/index.mjs:21` fetches
`/.well-known/blackwall-receipt-key.json` to verify receipts. That route has never
existed on the engine (404 on every host). It was written expecting a key the
engine does not publish.

## Do not invent a scheme -- Traceipt already solved this

The sibling project (`claude/x402-product-ideas-6adgah`, `traceipt/`) ships a
complete, tested design. Reuse it so one verifier covers both products.

| piece | Traceipt | reuse for Blackwall |
|---|---|---|
| envelope | JWS-style `{protected, payload, signature}`; `protected` carries `alg` + `kid` | same shape |
| key publication | `GET /jwks.json`, plus `/.well-known/did.json` (did:web) | same, on the engine |
| key rotation | active key first, **retired keys still published** so old receipts stay verifiable | same |
| PQ hedge | optional second ML-DSA-65 signature (`traceipt/pqsign.py`) | same, phase 2 |
| verifier | `clients/traceipt-verify` -- standalone, zero-dep npm lib | extend to Blackwall receipts |

## Constraint that differs: stdlib-only

Traceipt signs with the `cryptography` package. **Blackwall core is stdlib-only**,
so that dependency is not available here. It does not need to be:

- `cdp_auth.py` already implements **Ed25519 signing from RFC 8032 primitives,
  hashlib only** -- verified against the RFC test vectors in `test_cdp_auth.py`.
- It is deliberately **SIGN-ONLY** ("we never verify attacker-supplied
  signatures"). That is exactly Blackwall's role: Blackwall signs receipts, third
  parties verify them. The engine never verifies its own output.

So phase 1 needs no new dependency.

## Phase 1 -- Ed25519 (ships without a dependency)

**Additive. `receipt_id` keeps its current meaning and stays the ledger join key.**

1. **`receipt_signer.py`** (new) -- wraps `cdp_auth.ed25519_sign`:
   - load the seed from `BLACKWALL_SIGNING_SEED` (32 bytes, base64url)
   - `kid` = a stable hash of the public key (mirror `pqsign.pq_kid`)
   - `sign_envelope(payload)` -> `{protected:{alg:"EdDSA",kid}, payload, signature}`
   - canonical JSON must match `sign_receipt`'s (`sort_keys=True`,
     `separators=(",",":")`) so the digest is reproducible
2. **`blackwall.py`** -- in `forecast()`, alongside `receipt_id`, add
   `receipt` (the envelope) when a signing key is configured. Absent key ->
   field simply absent, exactly as today.
3. **Route** `GET /jwks.json` -- active + retired public keys.
4. **Route** `GET /.well-known/blackwall-receipt-key.json` -- the URL
   `blackwall-mcp-remote` ALREADY fetches. Serving it fixes an existing broken
   dependency for free. (Or repoint the worker at `/jwks.json`; serving both
   costs nothing and avoids a coordinated deploy.)
5. **Verifier** -- extend `clients/traceipt-verify`, or ship the equivalent in
   `blackwall-sdk`. Without a verifier "independently verifiable" is still just a
   claim.

**Key management -- the part not to be sloppy about.** `BLACKWALL_RECEIPT_KEY` is
an HMAC secret and must NOT be reused as an Ed25519 seed (different algorithm,
different exposure). Follow the precedent in commit `cc60fe0`, which made a
forgeable receipt-signing key LOUD at boot: refuse to emit a `receipt` envelope
at all unless a real seed is configured, and log loudly. A receipt signed with a
committed dev key is worse than no receipt, because it looks verifiable.

## Phase 2 -- the quantum question

**Why it matters here.** A compliance receipt is an audit artifact kept for years.
Traceipt's framing is the right one: the promise is "still un-forgeable a decade
from now." Ed25519 is elliptic-curve and therefore the one quantum-exposed piece;
SHA-256 digests and Merkle anchors are already quantum-sound.

**Why it is phase 2, not phase 1.**
- ML-DSA-65 needs `dilithium-py`. Blackwall core is stdlib-only, so it must be an
  OPTIONAL, lazily imported dependency -- never on the core verdict path.
  `traceipt/pqsign.py` is exactly this pattern: nothing is imported unless a PQ
  key is configured.
- Ed25519 is what verifiers support **today** (WebCrypto verifies it natively;
  `traceipt-verify` already does). ML-DSA has almost no verifier ecosystem yet.
- The threat is not urgent. Nothing is broken by shipping Ed25519 first, and a
  hybrid receipt is strictly additive later.

**Shape when it lands:** a second signature beside the Ed25519 one, Ed25519
remaining primary. `traceipt/pqsign.py` is directly portable -- `PQ_ALG`,
`pq_sign`, `pq_kid`, `pack_keypair` -- gated on `BLACKWALL_PQ_KEY`.

## What NOT to do

- **Do not drop the receipt and keep the claim.** The claim is the trust story:
  `COMPETITIVE.md` criticises Sentinel for a PLATFORM-HOSTED audit trail versus a
  neutral offline-verifiable one. An HMAC id only Blackwall can check IS a
  platform-hosted audit trail.
- **Do not build PQ first.** No verifier ecosystem, and a dependency on the core
  path of a stdlib-only engine.
- **Do not reuse the HMAC secret as the signing seed.**
- **Do not ship phase 1 without a working verifier.** Verifiability nobody can
  exercise is the same claim, unproven.

## Interim (today, minutes)

Until phase 1 ships, correct the two public claims -- the `blackwall-mcp-remote`
tool description and the awesome-x402 entry -- to describe what is returned: a
deterministic receipt id for the caller's audit trail. Overstating is the only
part that is actively wrong.
