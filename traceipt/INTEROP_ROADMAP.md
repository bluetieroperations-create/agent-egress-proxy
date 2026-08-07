# Interop roadmap — deferred items (build when triggered, not before)

The interop layer built so far is the foundation: W3C Verifiable Credentials in
both securing mechanisms (Data Integrity `eddsa-jcs-2022` **and** VC-JWT /
VC-JOSE-COSE), AP2 Mandate linkage, lifecycle bundles + Verifiable Presentations,
a neutral public verifier, `did:web` identity, OpenAPI discovery, and a resolvable
JSON-LD context — all issuer-bound, audited, and cross-checked against independent
reference implementations (`canonicalize` + `bs58` + WebCrypto for DI, `jose` for
JWT).

The items below are **intentionally deferred**. Each is triggered by a *specific
real event*. The rule: don't build them speculatively — a real integration will
tell you the exact shape it needs. This file exists so none are forgotten.

| Deferred item | Build it when… | Where it plugs in |
|---|---|---|
| **Pin the real AP2 Mandate hash / schema** | you integrate with an *actual* AP2 counterparty. `ap2.mandate_hash` is our own convention today; it must match how the AP2 ecosystem canonically identifies a Mandate. Needs the real spec + a sample Mandate. | `ap2.py` |
| **Revocation (`credentialStatus`)** | a consumer must check a receipt is *still valid* (e.g. credited / refunded), not just validly issued. Maps to credit notes. | `vc.py` (add `credentialStatus`), `service.py`, `ledger.py` |
| **Conformance-suite / real VC-DI library cross-check** | you claim "W3C-conformant" to an enterprise or list formally. We've verified against reference *primitives* + `jose`; a formal buyer may want the official W3C VC test suite / a `eddsa-jcs-2022` library. | `tools/` |
| **Exercise key rotation in `did:web` + JWKS** | your production signing key rotates. The machinery exists (retired keys in the DID doc + JWKS history); it just needs a real rotation run through it. | `service.py` (`extra_public_jwks`), `vc.did_web_document` |
| **VP holder proofs / challenge-response** | an interactive verifier needs the *holder* to prove possession in real time, not just present signed VCs. | `bundle.py` (sign the Presentation) |
| **SD-JWT selective disclosure** | you need to interop specifically with SD-JWT-based verifiers (e.g. Mastercard's stack). Our Merkle-based selective disclosure would need an SD-JWT variant. | new `sdjwt.py` alongside `disclosure.py` |
| **Proper media types / content negotiation** | a strict VC wallet consumes over HTTP by media type (`application/vc+ld+json`, `application/vc+ld+json+jwt`). Today endpoints return `application/json`. | `service.py` response headers |
| **`did:web` at the brand apex** | you want the issuer to read `did:web:traceipt.xyz` (brand) rather than `did:web:api.traceipt.xyz`. Requires serving the DID doc from the apex site. | `site/` + `vc.did_web_document` |

**Through-line:** everything above is a *refinement triggered by a real
integration*, not a prerequisite. The bottleneck is not more interop — it is a
real counterparty. Build these when one asks.
