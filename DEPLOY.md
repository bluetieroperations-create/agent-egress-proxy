# Deploying Blackwall (the hosted endpoint)

This makes the verdict service publicly callable so agents can reach it and the
awesome-x402 listing has a real target. The code is stdlib-only — the container
has **no pip dependencies**.

> **Posture change.** By default the service binds `127.0.0.1` (localhost-only,
> like the egress proxy). A public deploy binds `0.0.0.0` (`BLACKWALL_HOST`). On
> a public bind with **billing off**, anyone can call `/v1/forecast-payment` for
> free — the server prints a loud warning. For a real deploy, turn billing ON
> (`BLACKWALL_PAY_TO`) and point at a real facilitator (`BLACKWALL_FACILITATOR`).

## What the endpoint exposes

| route | purpose |
|-------|---------|
| `POST /v1/forecast-payment` | the verdict (GO/HOLD/STOP) — billed via x402 when `--pay-to` is set |
| `POST /v1/session` | fund-once session token (when billing on) |
| `POST /v1/report-outcome` | outcome reports (require the `report_token`) |
| `GET /.well-known/x402` | the service descriptor the listing/crawlers read |
| `GET /healthz` | health check |

## Config (env vars — set at deploy time, NOT baked into the image)

| var | meaning |
|-----|---------|
| `BLACKWALL_HOST` | `0.0.0.0` for a container (set in the image) |
| `BLACKWALL_PORT` | listen port (default 8402) |
| `BLACKWALL_STORE` | SQLite reputation store path (on a volume, e.g. `/data/reputation.db`) |
| `BLACKWALL_LEDGER` | verdict→outcome ledger path (`/data/ledger.jsonl`) |
| `BLACKWALL_INGEST` | self-populate the store from chain on first sight |
| `BLACKWALL_PAY_TO` | **your funded EVM wallet** — turns billing ON (you get paid here) |
| `BLACKWALL_FACILITATOR` | real x402 facilitator base URL (verify/settle) |
| `BLACKWALL_PRICE` | flat per-forecast price in USDC (default `0.001`) |
| `BLACKWALL_VALUE_PRICING` | set to enable value-aligned pricing (fee tracks amount-at-risk; micro is free) |
| `BLACKWALL_FREE_BELOW` / `BLACKWALL_PRICE_BPS` / `BLACKWALL_MIN_FEE` / `BLACKWALL_MAX_FEE` | value-pricing knobs (defaults `1.00` / `10`bps / `0.001` / `0.10`) |
| `BLACKWALL_SANCTIONS` | path to an OFAC sanctioned-address file (the "superset of free" screen). Refresh it: `python sanctions.py /data/sanctions.txt` |
| `BLACKWALL_RECEIPT_KEY` | **secret** for signing receipts + report tokens (set a strong random value) |

## Build & run (any container host)

```sh
docker build -t blackwall .
docker run -p 8402:8402 -v blackwall-data:/data \
  -e BLACKWALL_PAY_TO=0xYourFundedWallet \
  -e BLACKWALL_FACILITATOR=https://facilitator.x402.rs \
  -e BLACKWALL_RECEIPT_KEY="$(openssl rand -hex 32)" \
  blackwall
curl http://localhost:8402/healthz
curl http://localhost:8402/.well-known/x402
```

Deploys as-is to fly.io (`fly launch` detects the Dockerfile; add a volume +
`fly secrets set …`), Render (Docker web service + a disk + env), Railway, or any
Kubernetes/ECS. The only requirements are a persistent volume for `/data` and the
secrets above.

## Runbook — the order you asked for

1. **Deploy repo.** Push this codebase (or a fork you control) to the repo you'll
   deploy from. Add it to your Claude Code environment's repo scope if you want
   me to help iterate on it.
2. **Endpoint.**
   - Fund a wallet (Blackwall's `payTo`) with Base ETH + USDC (start on
     Base-Sepolia testnet; see `TESTNET_DRYRUN.md`).
   - Deploy the container with the env above. Point `BLACKWALL_FACILITATOR` at a
     real facilitator (e.g. `https://facilitator.x402.rs`, which supports
     `base-sepolia`).
   - Pre-warm or let `--ingest` populate the reputation store.
   - Verify: `GET /healthz` → ok; `GET /.well-known/x402` shows your `payTo` +
     price; an x402 client gets `402` then a verdict after paying.
3. **Fork + listing.** Only now is it real. Fork `xpaysh/awesome-x402`, add the
   entry from `DISCOVERY.md` (point it at your live endpoint), open the PR.

## Still deferred (not needed for the listing)

- **MCP-over-HTTP** for remote MCP clients (the stdio MCP server is local-only).
- **Scale**: rolling reputation aggregate + bounded history/nonce eviction.
- These are post-traffic concerns; the HTTP verdict API + discovery are what the
  listing needs.
