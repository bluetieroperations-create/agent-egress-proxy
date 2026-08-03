# Traceipt MCP server — a distribution surface

Agents discover capabilities through MCP. This server exposes Traceipt's
**neutral, no-payment value** — independent verification of machine-payment
receipts — as MCP tools, so any MCP-capable agent (Claude, Cursor, etc.) can
find and use it. The MCP registry's payment category is nearly empty today, and
"verify a payment receipt" is exactly the job an agent has when it receives a
receipt it didn't create.

## Tools

| Tool | What it verifies | Needs |
|---|---|---|
| `verify_receipt` | any W3C VC (Data Integrity or VC-JWT), VerifiablePresentation, or lifecycle bundle | nothing (self-certifying via did:key) |
| `verify_receipt_envelope` | a Traceipt signed-receipt envelope | the issuer's `jwks_url` |
| `verify_compliance_binding` | that a receipt is bound to a specific risk/sanctions verdict | nothing |
| `verify_onchain_anchor` | a Merkle inclusion proof, offline, + reports the on-chain tx | a Traceipt `base_url` |
| `fetch_and_verify` | fetches a receipt/credential from a URL and verifies it | a URL |

All read-only. No keys, no wallet, no payment. The point: an agent can verify a
Traceipt receipt **without trusting Traceipt's server** — credentials carry their
own signature and Merkle proofs verify offline against a published root.

## Run

```sh
pip install -r requirements-mcp.txt
python -m traceipt.mcp_server        # stdio transport
```

Add to an MCP client (e.g. Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "traceipt": {
      "command": "python",
      "args": ["-m", "traceipt.mcp_server"]
    }
  }
}
```

## Publish to the public MCP registry

The verification logic (`traceipt/mcp_tools.py`) is pure + unit-tested; the
server (`traceipt/mcp_server.py`) is a thin MCPServer shell (mcp>=2.0). To list it in the
[official registry](https://registry.modelcontextprotocol.io), publish a
`server.json` with the `mcp-publisher` CLI:

```jsonc
// server.json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-07-09/server.schema.json",
  "name": "io.traceipt/verifier",
  "description": "Independent, neutral verification of x402 machine-payment receipts, credentials, compliance bindings, and on-chain anchors.",
  "repository": { "url": "https://github.com/bluetieroperations-create/agent-egress-proxy", "source": "github" },
  "version": "0.1.0",
  "packages": [
    { "registry_type": "pypi", "identifier": "traceipt", "version": "0.1.0",
      "transport": { "type": "stdio" } }
  ]
}
```

```sh
mcp-publisher login github
mcp-publisher publish
```

(Publishing to PyPI as `traceipt` is a prerequisite for the pypi package entry;
until then, the server runs fine locally from this repo via the config above.)
