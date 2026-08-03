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
| `BLACKWALL_READINESS_LOCAL` | set to score endpoint readiness **ourselves** from public signals (no third-party call, no query-stream leak). Preferred over `BLACKWALL_READINESS`. |
| `BLACKWALL_READINESS` | base URL of an EXTERNAL readiness oracle (e.g. `https://ontarioprotocol.com`); folds its grade in, but calls a third party per request and reveals your query stream. Prefer `BLACKWALL_READINESS_LOCAL`. |
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

### Hosted platforms (config included)

Two ready blueprints live at the repo root — both pull the non-secret config from
their file and take the secrets at deploy time (never committed, never baked in):

- **fly.io** — `fly.toml`. `fly launch --no-deploy` adopts it, then:
  ```sh
  fly volume create blackwall_data --size 1 --region iad
  fly secrets set BLACKWALL_PAY_TO=0xYourFundedWallet \
      BLACKWALL_FACILITATOR=https://facilitator.x402.rs \
      BLACKWALL_RECEIPT_KEY=$(openssl rand -hex 32)
  fly deploy
  ```
  Defaults to scale-to-zero (idle cost ~$0); set `min_machines_running = 1` to
  keep it warm before launch.
- **Render** — `render.yaml`. New → Blueprint → point at the repo; Render prompts
  for the `sync: false` secrets and attaches the `/data` disk.

Both bind `0.0.0.0:8402` and route HTTPS to it; health is `GET /healthz`. Also
deploys to Railway or any Kubernetes/ECS — the only requirements are a persistent
volume for `/data` and the secrets above.

### Free tier (Render, $0) — PREBUILT warm corpus committed to the image

`render-free.yaml` runs on Render's free plan with **no persistent disk**. Rather than
crawl the chain at build time, the **full 292-payee reputation store + the per-category
price index are PREBUILT once and committed** (`data/reputation_seed.db.gz` ≈ 2.8 MB,
`data/category_index.json`). The Dockerfile just `COPY`s them and decompresses the store
into `/app/data/reputation.db` (`BLACKWALL_STORE` / `BLACKWALL_CATEGORY_INDEX` point
there). The container boots **warm with the whole manifest** — ~264 of 292 payees clear
the thin-history/Sybil gates, and 5+ service-category price baselines are live — and
re-warms from the image on every cold start.

Why prebuilt (option C) beats a build-time backfill:
- **Full coverage, free.** The persistent disk buys *persistence* + *always-on*, NOT
  coverage — coverage is a build-time/image concern. Committing the store gives the free
  tier the full 292-payee corpus without a disk.
- **Fast, reliable builds (~1 min).** No build-time crawl, so a build can't be broken or
  slowed by Blockscout/Bazaar downtime or rate limits.
- **As deep as you want.** Depth is set when you build the artifact, not capped by build
  time — no per-payee truncation of the busiest endpoints.

**Regenerate the artifacts periodically** (so reputation doesn't go stale), then commit
+ redeploy:
```sh
python3 chain_backfill.py --store /tmp/rep.db --payees-file data/seed_payees.txt --max-pages 2
python3 category_pricing.py --store /tmp/rep.db --out data/category_index.json --max-pages 8
python3 -c "import gzip,shutil; shutil.copyfileobj(open('/tmp/rep.db','rb'), gzip.open('data/reputation_seed.db.gz','wb',9))"
```
Tradeoff: a ~2.8 MB binary lives in git and goes stale between refreshes. `data/
seed_payees_bake.txt` (top-60, tier-sectioned) is kept for reference/regeneration.

**Still free-tier-only** (needs the paid `/data` disk, `render.yaml`): runtime-learned
ledger outcomes reset on restart, and the instance cold-starts after ~15 min idle.
Neither is about coverage.

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
   - **Pre-warm the store from the committed manifest** so the service boots warm
     (a known payee gets real history, not a cold-start HOLD), then set
     `BLACKWALL_STORE` to that path on the volume:
     ```sh
     python3 chain_backfill.py --store /data/reputation.db \
       --payees-file data/seed_payees.txt --max-pages 3   # ~198 payees, ~30 anchors
     ```
     The same `--store` drives the **full signal stack** — base reputation **plus**
     the cross-counterparty payer graph / reputation (Sybil corroboration) and the
     temporal `stale` gate are built off it automatically (no extra flags), and every
     verdict carries a `confidence` block (`level` + `backed_by` / `missing`).
   - Verify: `GET /healthz` → ok; `GET /.well-known/x402` shows your `payTo` +
     price; an x402 client gets `402` then a verdict after paying. A quick local
     smoke test (no billing) — the verdict should include `confidence`,
     `signals.cross_counterparty`, and `signals.temporal`:
     ```sh
     python3 blackwall.py --store /data/reputation.db --port 8402 &
     curl -s -XPOST localhost:8402/v1/forecast-payment -H 'content-type: application/json' \
       -d '{"counterparty":"0x0e84dded…","amount":"0.05","asset":"0x8335…2913","chain":"base"}'
     ```
3. **Fork + listing.** Only now is it real. Fork `xpaysh/awesome-x402`, add the
   entry from `DISCOVERY.md` (point it at your live endpoint), open the PR.

## Still deferred (not needed for the listing)

- **MCP-over-HTTP** for remote MCP clients (the stdio MCP server is local-only).
- **Scale**: rolling reputation aggregate + bounded history/nonce eviction.
- These are post-traffic concerns; the HTTP verdict API + discovery are what the
  listing needs.
