# traceipt-verify

Independently verify a [Traceipt](https://traceipt.xyz) receipt — in Node or the
browser, with **zero dependencies**. A receipt checks out against the public
chain and the issuer's published key with **no call to the Traceipt server**;
that independence is the whole point.

```js
import { verifyReceipt } from "traceipt-verify";

// `doc` = a /attest 201 body, a raw proof, the payer client's saved run,
// or a signed receipt envelope ({ protected, payload, signature }).
const r = await verifyReceipt(doc, {
  rpcUrl: "https://mainnet.base.org", // omit to use the chain default
  jwks,                                // issuer JWKS, for receipt signatures
  // offline: true,                    // skip the on-chain RPC check
});

console.log(r.ok);       // overall
for (const c of r.checks) console.log(c.name, c.ok, c.detail);
```

## Checks

- **verdict_binding** — if a verdict is present, its digest equals the anchored leaf.
- **inclusion_proof** — the audit path recomputes the Merkle root (RFC 6962).
- **onchain_anchor** — the root really is in the tx calldata (`TRACEIPT-ANCHOR` + root) on Base.
- **signature** — the Ed25519 envelope signature verifies against the issuer JWKS (WebCrypto).
- **pq_signature** — if a hybrid post-quantum (ML-DSA) signature is present it is
  *reported*, not verified: this lib checks the classical Ed25519 path; verify the
  ML-DSA signature with the Python verifier or an ML-DSA library.

`ok` values are `true` (passed), `false` (failed), or `null` (skipped / not
applicable). Overall `ok` is true unless a check is `false`.

## Requirements

Node 20+ (for global `fetch` and WebCrypto Ed25519), or any modern browser. No
install-time or runtime dependencies. `node test.mjs` verifies the live mainnet
run.

## Also available

- `tools/verify.py` — the same checks as a Python CLI.
- `traceipt.xyz/verify` — the same checks as a paste-in web page.
