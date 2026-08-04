# Deploying Traceipt

Two moving parts, deployed separately:

- **The website** (`site/`) — a static marketing page, hosted on **Cloudflare
  Pages** at `traceipt.xyz`. Section 0 below.
- **The API service** (the Python app) — a **free-tier testnet demo** on
  Fly/Render at `api.traceipt.xyz`, then production hardening. Sections 1–2.

---

## 0. The website (Cloudflare Pages → traceipt.xyz)

The site is plain static HTML in `site/`. `wrangler.toml` names the project
`traceipt` (no dashes) and points at `site/`.

**One-time auth** (uses your Cloudflare account, not stored in the repo):

```sh
cd traceipt
npm install                 # installs wrangler locally
npx wrangler login          # opens a browser once; OR set CLOUDFLARE_API_TOKEN
```

For a headless/CI deploy instead of `login`, create a token in the Cloudflare
dashboard (My Profile → API Tokens → template **"Edit Cloudflare Pages"**) and
export it:

```sh
export CLOUDFLARE_API_TOKEN=...      # Pages:Edit
export CLOUDFLARE_ACCOUNT_ID=...     # from the dashboard URL / Workers page
```

**Deploy:**

```sh
npm run deploy      # = wrangler pages deploy site --project-name=traceipt
```

**Attach the domain** (once): Cloudflare dashboard → Workers & Pages →
`traceipt` → Custom domains → add `traceipt.xyz`. Since the domain is already
in your Cloudflare account, DNS is wired automatically. Re-running
`npm run deploy` publishes updates to the same site.

> Keep the apex `traceipt.xyz` for the site and use `api.traceipt.xyz` for the
> service (section 1), so the two never collide.

---

## The API service

A **free-tier testnet demo** (for showing people and validating demand), then
**production** hardening on top of it. Start with the demo.

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

Do these in order. The first two make the demo *durable* (the biggest gap);
the rest make it *real*. On Render, set secrets in the dashboard's
**Environment** tab (mark them secret / `sync:false`), not in `render.yaml`.

**A. Persistence — stop losing receipts and keys on restart.** The free plan
has no disk, so the ledger and signing key reset when the instance idles or
redeploys, and every prior receipt stops verifying. Fix both:
- **Disk:** switch to a paid instance and mount a disk at `/data` (the
  `render.yaml` comment block and `fly.toml` already show how; Fly already
  has the volume). Now the ledger survives.
- **Signing key as a durable secret:** generate the key once and provide it
  as `RECEIPTS_KEY_PEM` (a secret env var) — the service loads it in preference
  to the key file, so the key survives even without a disk. **Prefer the
  single-line base64 form** — a multi-line PEM pasted into a web form (Render,
  etc.) gets its newlines collapsed and fails to load (`MalformedFraming`).
  Base64 has no newlines to mangle:
  ```sh
  python3 -c "import base64;from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as K;from cryptography.hazmat.primitives import serialization as s;print(base64.b64encode(K.generate().private_bytes(s.Encoding.DER,s.PrivateFormat.PKCS8,s.NoEncryption())).decode())"
  ```
  The loader accepts base64(DER), base64(PEM), a plain PEM, and even a
  newline-mangled or bare-body PEM (it repairs the framing), so any of those
  will work — but single-line base64 is the one that never gets corrupted in
  transit. Capture the matching public JWK for your records with
  `python3 -m traceipt.service --print-jwk`. **Back the key up** — it is the
  root of trust for every receipt.
- **`/attest` durability WITHOUT a disk:** set `RECEIPTS_ATTEST_SEAL=immediate`.
  In `batch` mode (default) a paid `/attest` is queued and sealed later — so a
  restart before sealing drops the paid attestation ("paid-but-lost"). In
  `immediate` mode each `/attest` is sealed on submit and the **full inclusion
  proof is returned in the 201**, so the payer holds a self-contained,
  verifiable receipt that no longer depends on this server remembering it. With
  `--publisher onchain` the root is also on-chain (trustless timestamp, durable
  across restarts). This is the recommended mainnet posture when you are not
  running a persistent disk. `/receipts` is already safe (the signed receipt is
  returned in its 201); this closes the same gap for `/attest`.

- **Durability WITHOUT a disk (zero-cost path).** You do not strictly need a
  paid disk. Two mechanisms make a receipt survive a disk-less reset:
  - **Self-verifying receipts + a standalone verifier.** With
    `RECEIPTS_ATTEST_SEAL=immediate` the full inclusion proof rides in the 201,
    and the signing key is a durable secret (`RECEIPTS_KEY_PEM`), so a receipt
    verifies against the on-chain root + the published JWKS with **no call to
    this server**. `tools/verify.py` (CLI) and `site/verify.html` (a static page,
    hosted free on Cloudflare Pages at `/verify`) do exactly that check —
    Merkle inclusion + on-chain calldata + Ed25519 signature. This is the real
    expression of "independently verifiable": the issuing server can be down or
    gone and the receipt still proves out.
  - **Rebuild-the-anchor-index-from-chain.** On startup, if the anchor table is
    empty but the gas wallet has published roots, the service re-derives every
    `(root, tx, timestamp)` straight from the gas wallet's own transactions
    (`recover.py`), so `/anchors` self-heals with no trusted DB. Set
    `RECEIPTS_EXPLORER_API_KEY` (a free Basescan key) for reliable enumeration;
    it is best-effort and never blocks startup. The leaves inside a batch are
    not on-chain (only the root is), so a caller presents the inclusion proof and
    it is checked against the on-chain-confirmed root.

**B. Auto-anchor.** Set `RECEIPTS_ANCHOR_INTERVAL` (seconds) so batches seal on
their own; otherwise callers need an admin `POST /anchor` each time. The
blueprint sets `300` (5 min) for the demo; `3600` (hourly) is fine for
lower-traffic prod. On the free plan the loop pauses while the instance is idle
and resumes on the next request, so a pending item seals within the interval of
activity.

**C. Admin token.** `RECEIPTS_ADMIN_TOKEN=<random>` so `POST /anchor` and
`POST /credits` aren't open. Call them with `Authorization: Bearer <token>`.

**D. Real chain + settlement.** `RECEIPTS_CHAIN=base`,
`RECEIPTS_SETTLEMENT=rpc`, `RECEIPTS_RPC_URL=<reliable Base RPC>` (a provider
endpoint, not the public node). Receipts then attest a *verified* transfer,
and `verification_method` reads `rpc` instead of `mock`. For real mainnet
money also set `RECEIPTS_MIN_CONFIRMATIONS` to a few blocks (e.g. `3`): a
settlement is only attested once it is that many confirmations deep, so a tx
that later reorgs out is never turned into a durable receipt. Base has ~2s
blocks, so this adds only a few seconds of latency. Leave it `0` on testnet.
> Do NOT flip to `rpc` without also setting `RECEIPTS_PAY_TO` (step E) — an
> unbound `rpc` service would let a caller claim any third party's transfer
> as "verified". Honest `mock` beats dishonest `rpc`.

**E. Receiving wallet + payment gate.** `RECEIPTS_PAY_TO=0xYourWallet` and
`RECEIPTS_SELLER_ID=your.domain` (seller-hosted binding; only the **address**,
never the key). Then `X402_GATE=facilitator`,
`RECEIPTS_FACILITATOR_URL=<Coinbase CDP / Cloudflare>`, `RECEIPTS_BIND_PAYER=1`
to actually collect payment and close the payer-fraud gap.

**F. On-chain anchoring (optional).** Publish Merkle roots to the chain so
existence-by-time is provable against a public ledger, not just our DB.
Build the image with `--build-arg ONCHAIN=1` (installs `eth-account`), then
set `RECEIPTS_PUBLISHER=onchain` and `RECEIPTS_GAS_KEY=<key of a dedicated,
tiny gas wallet>`. **Never the receiving wallet's key** — the gas wallet
holds a few dollars of ETH and does nothing but sign root transactions. With
the publisher off, anchoring still works locally (`onchain_tx` stays null;
proofs still verify).

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
| `RECEIPTS_MIN_CONFIRMATIONS` | `--min-confirmations` | `0` (set a few for mainnet) |
| `RECEIPTS_PAY_TO` | `--pay-to` | zero address (unset) |
| `RECEIPTS_SELLER_ID` | `--seller-id` | — |
| `RECEIPTS_PRICE_BASE_UNITS` | `--price` | `2000` ($0.002) |
| `RECEIPTS_KEY` | `--key` | `issuer_ed25519.pem` |
| `RECEIPTS_KEY_PEM` | (inline) | — (preferred over key file; a durable secret. Accepts base64(DER/PEM) or plain/mangled PEM) |
| `RECEIPTS_DB` | `--db` | `receipts.db` |
| `RECEIPTS_ADMIN_TOKEN` | `--admin-token` | — (gates `/anchor`, `/credits`) |
| `RECEIPTS_JWKS_HISTORY` | `--jwks-history` | — |
| `RECEIPTS_PUBLISHER` | `--publisher` | `off` (off / mock / onchain) |
| `RECEIPTS_GAS_KEY` | `--gas-key` | — (onchain only; dedicated gas wallet) |
| `RECEIPTS_ANCHOR_INTERVAL` | `--anchor-interval` | `0` (seconds; 0 = manual) |
| `RECEIPTS_ATTEST_SEAL` | `--attest-seal` | `batch` (`immediate` = seal-on-submit + self-contained proof in the 201) |
| `RECEIPTS_EXPLORER_API_KEY` | (inline) | — (free Basescan key; enables startup anchor-recovery from chain) |

See the main [README](README.md#security-model--threat-model) for the threat
model before going to production.
