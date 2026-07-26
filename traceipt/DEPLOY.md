# Deploying Traceipt

Two paths here: a **free-tier testnet demo** (for showing people and validating
demand), and the **production** hardening on top of it. Start with the demo.

The service binds `127.0.0.1` by default. In a container you set
`RECEIPTS_HOST=0.0.0.0` so the platform's HTTPS edge can reach it — the
Dockerfile and both platform configs already do this. **Never expose
`0.0.0.0` without an HTTPS edge / reverse proxy in front of it.**

---

## 1. Free-tier testnet demo

Goal: a public HTTPS URL anyone can hit to see the whole flow — 402 → pay →
signed receipt → invoice PDF → verify — on **Base Sepolia**, with **no real
money** required.

Demo defaults (already set in `Dockerfile` / `fly.toml` / `render.yaml`):

| Setting | Value | Why |
|---|---|---|
| `RECEIPTS_CHAIN` | `base-sepolia` | testnet |
| `X402_GATE` | `dev` | any `X-PAYMENT` header is accepted — visitors can try it without a wallet |
| `RECEIPTS_SETTLEMENT` | `mock` | no real tx needed; issued receipts are permanently marked `verification_method:"mock"` so they're visibly non-attestations |
| `RECEIPTS_HOST` | `0.0.0.0` | reachable behind the platform edge |
| `RECEIPTS_BASE_URL` | your public URL | so verify links + QR resolve |

### Fly.io

```sh
cd traceipt
fly launch --no-deploy                 # accept the app name or edit fly.toml
fly volumes create traceipt_data --size 1 --region iad
# set RECEIPTS_BASE_URL in fly.toml to the URL Fly shows you, then:
fly deploy
```

`auto_stop_machines` scales to zero when idle, which keeps a low-traffic demo
inside free usage.

### Render.com

Push the repo, then **New → Blueprint** and point at it; `render.yaml` does the
rest. After the first deploy, set `RECEIPTS_BASE_URL` to the assigned
`onrender.com` URL (or your custom domain) and redeploy.

### Smoke-test the live demo

```sh
BASE=https://your-demo-url
curl -s $BASE/health
# issue a receipt (dev gate: any X-PAYMENT works)
curl -s -X POST $BASE/receipts -H 'X-PAYMENT: demo' -H 'Content-Type: application/json' -d '{
  "seller_id":"demo.seller","settlement":{"chain":"base-sepolia",
  "tx_hash":"0x'"$(printf 'cc%.0s' {1..32})"'","amount_base_units":"5000",
  "payer":"0x'"$(printf 'aa%.0s' {1..20})"'","payee":"0x'"$(printf 'bb%.0s' {1..20})"'"},
  "commerce":{"resource":"https://demo.seller/api","description":"demo query",
  "quoted_amount_base_units":"5000"}}'
# then open the verify_url from the response, and /receipts/<id>/invoice.pdf
```

---

## 2. Production hardening

Change these off the demo defaults, in roughly this order:

1. **Real chain + settlement.** `RECEIPTS_CHAIN=base`, `RECEIPTS_SETTLEMENT=rpc`,
   and `RECEIPTS_RPC_URL=<a reliable Base RPC>` (a provider endpoint, not the
   public node). Now receipts attest a *verified* on-chain transfer.

2. **Real payment gate + payer binding.** `X402_GATE=facilitator`,
   `RECEIPTS_FACILITATOR_URL=<Coinbase CDP / Cloudflare facilitator>`, and
   `RECEIPTS_BIND_PAYER=1`. This collects payment and closes the last fraud gap.

3. **Your receiving wallet.** `RECEIPTS_PAY_TO=0xYourNewTraceiptWallet` and
   `RECEIPTS_SELLER_ID=your.domain` (seller-hosted binding). Only the
   **address** goes here — never the private key.

4. **Public URL.** `RECEIPTS_BASE_URL=https://<your domain>` so every receipt's
   verify link and QR point at a domain you own **permanently** (receipts are
   meant to verify years later).

5. **Signing key as a secret.** Generate the Ed25519 key once and mount it as a
   platform secret at `RECEIPTS_KEY`, rather than letting the container create
   an ephemeral one. Back it up — it is the root of trust for every receipt.
   (Roadmap: move it into a KMS/HSM.)

6. **Admin token.** `RECEIPTS_ADMIN_TOKEN=<random>` so `POST /anchor` (sealing
   Merkle batches) isn't open. Call it with `Authorization: Bearer <token>`.

7. **On-chain anchoring (optional).** Anchoring works locally without it
   (`onchain_tx` stays null, proofs still verify). To publish roots on-chain,
   wire a publisher with a **separate small gas wallet** — never the receiving
   wallet's key.

### Env var reference

| Env | Flag | Default |
|---|---|---|
| `RECEIPTS_HOST` | `--host` | `127.0.0.1` |
| `RECEIPTS_PORT` | `--port` | `8402` |
| `RECEIPTS_BASE_URL` | `--base-url` | `http://<host>:<port>` |
| `RECEIPTS_CHAIN` | `--chain` | `base-sepolia` |
| `X402_GATE` | `--gate` | `dev` |
| `RECEIPTS_FACILITATOR_URL` | `--facilitator-url` | — |
| `RECEIPTS_BIND_PAYER` | `--bind-payer` | off |
| `RECEIPTS_SETTLEMENT` | `--settlement` | `rpc` |
| `RECEIPTS_RPC_URL` | `--rpc-url` | public node |
| `RECEIPTS_PAY_TO` | `--pay-to` | zero address (unset) |
| `RECEIPTS_SELLER_ID` | `--seller-id` | — |
| `RECEIPTS_PRICE_BASE_UNITS` | `--price` | `2000` ($0.002) |
| `RECEIPTS_KEY` | `--key` | `issuer_ed25519.pem` |
| `RECEIPTS_DB` | `--db` | `receipts.db` |
| `RECEIPTS_ADMIN_TOKEN` | `--admin-token` | — |
| `RECEIPTS_JWKS_HISTORY` | `--jwks-history` | — |

See the main [README](README.md#security-model--threat-model) for the threat
model before going to production.
