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

Submit this entry to the active x402 directories —
[`xpaysh/awesome-x402`](https://github.com/xpaysh/awesome-x402) and
[`Merit-Systems/awesome-x402`](https://github.com/Merit-Systems/awesome-x402)
(the older `coinbase/awesome-x402` URL may be stale — verify before submitting):

> **[Blackwall](#)** — Pre-signature **financial counterparty-risk** verdict for
> x402. Returns **GO / HOLD / STOP** before an agent signs a payment, from
> *behavioral* counterparty reputation (chain-confirmed settlement/dispute
> history), **price-anomaly** vs the counterparty's own median, and **OFAC
> sanctions** screening. Complements endpoint-readiness (Ontario) and KYT
> (AnChain): it judges *whether to trust this payee with this payment at this
> price*, not whether the endpoint is set up. Itself an x402 resource
> (pay-per-forecast, sub-cent; reusable sessions) and an **MCP server**. Verdict,
> never custody. `POST /v1/forecast-payment` · MCP tool `forecast_payment` ·
> Base / USDC.

Category: **payment-risk / agent-guardrails**.
Tags: `x402` `payments` `counterparty-risk` `price-anomaly` `sanctions`
`reputation` `agent-guardrail` `base` `usdc` `mcp`.

See [`COMPETITIVE.md`](COMPETITIVE.md) for the full landscape and how this
positioning is sourced.

## Distribution lift (spec §6)

1. **List** (this doc) — lowest lift; discovery via the service card +
   awesome-x402. ✅ here.
2. **Ship the MCP server** (`mcp_server.py`) — any wallet-holding agent finds
   and calls it self-serve. ✅ built.
3. **Partner with a facilitator** for in-flow risk checks — relationship-gated,
   later. (The facilitator seam is built; `--facilitator <url>`.)
