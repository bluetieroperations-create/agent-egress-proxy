"""
Traceipt MCP server: exposes independent receipt/credential verification as MCP
tools so any MCP-capable agent can discover and use it.

Run:
    pip install -r requirements-mcp.txt
    python -m traceipt.mcp_server            # stdio transport (default)

The verification LOGIC lives in mcp_tools.py (pure, stdlib, unit-tested). This
file is only the thin FastMCP shell that registers those functions as tools, so
the SDK dependency stays out of the core service and the tests.

See MCP.md for registering this server in the public MCP registry.
"""
from __future__ import annotations

# mcp 2.0 renamed the high-level server from FastMCP to MCPServer; the
# @tool() decorator + run() ergonomics are otherwise the same.
from mcp.server.mcpserver import MCPServer

from .mcp_tools import (
    fetch_and_verify as _fetch_and_verify,
    verify_compliance_binding as _verify_compliance_binding,
    verify_credential as _verify_credential,
    verify_onchain_anchor as _verify_onchain_anchor,
    verify_receipt_envelope as _verify_receipt_envelope,
)

mcp = MCPServer(
    name="traceipt",
    instructions=(
        "Independent, neutral verification of machine-payment (x402) receipts. "
        "Verify a W3C Verifiable Credential, VerifiablePresentation, lifecycle "
        "bundle, VC-JWT, Traceipt receipt envelope, compliance (risk-verdict) "
        "binding, or on-chain Merkle anchor -- offline where the credential is "
        "self-certifying. Read-only; no payment or keys required."),
)


@mcp.tool()
def verify_receipt(document: str) -> dict:
    """Verify a W3C Verifiable Credential, VerifiablePresentation, Traceipt
    lifecycle bundle, or VC-JWT. `document` is the JSON (or a bare VC-JWT
    string). Self-certifying: verifies any issuer's did:key/did:web signature,
    not just Traceipt's. Returns {ok, kind, issuer, ...}."""
    return _verify_credential(document)


@mcp.tool()
def verify_receipt_envelope(envelope: str, jwks_url: str) -> dict:
    """Verify a Traceipt signed-receipt envelope (protected/payload/signature)
    against the issuer's published JWKS at `jwks_url` (e.g.
    https://api.traceipt.xyz/jwks.json). Reports whether it is compliance-bound."""
    return _verify_receipt_envelope(envelope, jwks_url)


@mcp.tool()
def verify_compliance_binding(receipt: str, verdict: str) -> dict:
    """Verify a compliance-bound receipt is bound to the ACTUAL risk/sanctions
    verdict object (recomputes the digest; catches a swapped verdict). If the
    verdict carries its screens, also re-derives the GO/STOP/REVIEW decision."""
    return _verify_compliance_binding(receipt, verdict)


@mcp.tool()
def verify_onchain_anchor(base_url: str, attestation_id: str) -> dict:
    """Fetch a Traceipt attestation's Merkle inclusion proof, verify it OFFLINE
    against the anchor root, and report the on-chain anchor transaction if the
    root was published. `base_url` e.g. https://api.traceipt.xyz."""
    return _verify_onchain_anchor(base_url, attestation_id)


@mcp.tool()
def fetch_and_verify(url: str) -> dict:
    """Fetch a receipt or credential from a URL and verify it, auto-detecting the
    format (VC, VP, VC-JWT, or a Traceipt receipt envelope)."""
    return _fetch_and_verify(url)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
