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
server (`traceipt/mcp_server.py`) is a thin MCPServer shell (mcp>=2.0).
`pyproject.toml` makes the package pip-installable with a `traceipt-mcp` console
entry point, and `server.json` is the registry manifest.

Runbook to list it in the [official registry](https://registry.modelcontextprotocol.io):

```sh
# 1. Build + publish the package to PyPI (name `traceipt` must be available).
python -m build            # produces dist/*.whl and *.tar.gz from pyproject.toml
python -m twine upload dist/*

# 2. Publish the registry manifest (server.json in this dir).
mcp-publisher login github  # verifies the io.github.bluetieroperations-create/* namespace
mcp-publisher publish
```

Notes:
- The namespace `io.github.bluetieroperations-create/...` in `server.json` is
  verified via the GitHub login, so it must match the repo owner.
- `server.json` field names track the registry schema; if `mcp-publisher`
  reports a mismatch, reconcile against the `$schema` URL in the file.
- Until the PyPI publish lands, the server still runs locally from this repo via
  the client config above — publishing only affects *discoverability*.
