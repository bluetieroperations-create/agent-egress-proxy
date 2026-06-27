# Listing Blackwall (x402 service discovery — spec step 5)

Blackwall is discoverable two ways: **machine-readable** (served by the running
service) and **human submission** (to the ecosystem directory / awesome-x402).

## Machine-readable (live)

The verdict server self-describes at:

```
GET /.well-known/x402      # x402 service card
GET /v1/discovery          # alias
```

It returns the descriptor built by `discovery.build_descriptor` — name,
category, tags, the `forecast_payment` resource (input schema, GO/HOLD/STOP
output), the per-call x402 `accepts` (price/asset/network/payTo, when billing is
on), MCP availability, and `custody: false` (verdict, not custody). x402
service-discovery crawlers and agents can fetch and parse this with no prior
knowledge of Blackwall.

```sh
curl http://<host>/.well-known/x402
```

## Human submission (awesome-x402 entry)

Submit this entry to [awesome-x402](https://github.com/coinbase/awesome-x402)
and any x402 service directory:

> **[Blackwall](#)** — Pre-signature x402 payment guardrail. Returns a
> **GO / HOLD / STOP** verdict before an agent signs a payment, from behavioral
> counterparty reputation (its own settlement/dispute ledger + an indexed Base
> store) and price-anomaly signals. Itself an x402 resource (pay-per-forecast,
> sub-cent; reusable sessions) and an **MCP server**. Verdict, not custody.
> `POST /v1/forecast-payment` · MCP tool `forecast_payment` · Base / USDC.

Category: **payment-risk / agent-guardrails**.
Tags: `x402` `payments` `risk` `reputation` `agent-guardrail` `base` `usdc` `mcp`.

## Distribution lift (spec §6)

1. **List** (this doc) — lowest lift; discovery via the service card +
   awesome-x402. ✅ here.
2. **Ship the MCP server** (`mcp_server.py`) — any wallet-holding agent finds
   and calls it self-serve. ✅ built.
3. **Partner with a facilitator** for in-flow risk checks — relationship-gated,
   later. (The facilitator seam is built; `--facilitator <url>`.)
