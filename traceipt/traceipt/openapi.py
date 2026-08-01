"""
OpenAPI 3.1 description of the Traceipt API, served at GET /openapi.json so
agents and ecosystem discovery (the x402 Bazaar, AP2 tooling) can find and use
the endpoints. Built as a plain dict, parameterized by the deployment's base URL
and x402 pricing. Pure + stdlib.
"""
from __future__ import annotations

VERSION = "0.2"


def _paid(summary, resource):
    return {
        "summary": summary,
        "description": f"x402-gated. Without an X-PAYMENT header this returns "
                       f"402 with a payment challenge (scheme=exact, USDC). Pay "
                       f"and resend to {resource}.",
        "responses": {
            "201": {"description": "created (signed artifact returned)"},
            "200": {"description": "idempotent replay of an earlier submission"},
            "402": {"description": "payment required (x402 challenge)"},
            "400": {"description": "malformed request"},
        },
    }


def _ok(summary, desc="ok"):
    return {"summary": summary, "responses": {"200": {"description": desc}}}


def spec(base_url: str, price_base_units: str = "2000", chain: str = "base-sepolia") -> dict:
    base = base_url.rstrip("/")
    dollars = f"${int(price_base_units) / 1_000_000:.6f}".rstrip("0").rstrip(".")
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Traceipt",
            "version": VERSION,
            "summary": "Verifiable receipts for machine (x402) payments.",
            "description": (
                "Turns an x402 USDC payment into a signed, on-chain-verified, "
                "independently-verifiable receipt. Emits W3C Verifiable "
                "Credentials (eddsa-jcs-2022) that sit alongside AP2 Mandates; "
                "supports selective disclosure, provable chain completeness, "
                "compliance (verdict) binding, Merkle anchoring, and a neutral "
                f"verifier. Price: {dollars} USDC per paid call on {chain}."),
        },
        "servers": [{"url": base}],
        "paths": {
            "/receipts": {"post": _paid("Issue a signed receipt for a verified "
                                        "settlement.", "/receipts")},
            "/receipts/{id}": {"get": _ok("Fetch a stored signed receipt envelope.")},
            "/receipts/{id}/vc": {"get": _ok("The receipt as a W3C Verifiable "
                                             "Credential (eddsa-jcs-2022, AP2-ready).")},
            "/receipts/{id}/vc.jwt": {"get": _ok("The receipt as a VC-JWT "
                                                 "(W3C VC-JOSE-COSE) for JWT-native verifiers.")},
            "/receipts/{id}/invoice.pdf": {"get": _ok("Rendered A4 invoice PDF.")},
            "/receipts/{id}/proof": {"get": _ok("Merkle inclusion proof (once anchored).")},
            "/receipts/{id}/disclose": {"post": {
                "summary": "Signed redacted disclosure of chosen fields (operator-gated).",
                "responses": {"200": {"description": "signed disclosure"},
                              "403": {"description": "admin token required"}}}},
            "/attest": {"post": _paid("Anchor an external digest (e.g. a risk "
                                      "verdict) in the next Merkle batch.", "/attest")},
            "/attest/{id}": {"get": _ok("Attestation status.")},
            "/attest/{id}/proof": {"get": _ok("Merkle inclusion proof for the attestation.")},
            "/attest/{id}/vc": {"get": _ok("The anchoring attestation as a W3C VC.")},
            "/credits": {"post": {"summary": "Issue a credit note for a verified "
                                  "refund (operator-gated).",
                                  "responses": {"201": {"description": "credit note"},
                                                "403": {"description": "admin token required"}}}},
            "/anchor": {"post": {"summary": "Seal a Merkle batch (operator-gated).",
                                 "responses": {"200": {"description": "anchor summary"},
                                               "403": {"description": "admin token required"}}}},
            "/anchors": {"get": _ok("List sealed anchors.")},
            "/verify": {"post": _ok("Neutral verifier: verify any W3C VC, "
                                    "VerifiablePresentation, or lifecycle bundle "
                                    "(public, offline).")},
            "/verify/{id}": {"get": _ok("Full verification report for a stored receipt.")},
            "/chain/{seller}": {"get": _ok("Chain integrity check for a seller.")},
            "/chain/{seller}/completeness": {"get": _ok("Signed proof the chain is "
                                                        "complete (nothing hidden).")},
            "/jwks.json": {"get": _ok("Issuer public keys (JWKS).")},
            "/credentials/v1": {"get": _ok("The JSON-LD context for Traceipt "
                                           "credential terms.")},
            "/.well-known/did.json": {"get": _ok("did:web DID document for the issuer.")},
            "/health": {"get": _ok("Liveness + gate/chain mode.")},
            "/openapi.json": {"get": _ok("This document.")},
        },
    }
