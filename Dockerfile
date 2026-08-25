# Blackwall verdict service -- stdlib-only, no pip dependencies.
FROM python:3.12-slim

# Non-root.
RUN useradd -m -u 10001 blackwall
WORKDIR /app

# Source (stdlib only -- nothing to pip install). Copy ALL modules: forecast() lazily
# imports payload_sim / calldata (-> keccak / secp256k1 / eip712) and others at request
# time, so a hand-maintained subset silently 502s the verdict path when one is missing.
# Copy everything and never play that whack-a-mole again. (test_*.py ride along unused.)
COPY *.py ./

# Warm-boot manifests: seed_payees.txt (full 292, the source of the prebuilt store
# below) and seed_payees_bake.txt (kept for reference/regeneration). Not used at build
# time anymore -- the store is PREBUILT and committed (see below).
COPY data/seed_payees.txt data/seed_payees_bake.txt ./data/

# The x402 discovery-crawl artifact: each payee's OWN advertised price bounds.
# advertised_prices.py loads this at startup so a price STOP can be withheld on a
# route the payee publicly lists (blackwall.price_stop_is_corroborated). WITHOUT
# IT THE ARM IS SILENTLY INERT -- the loader fails OPEN to an empty index, so the
# container would boot healthy and simply STOP three legitimate live endpoints.
# That is exactly how the RWA gate shipped unwired once; do not drop this COPY.
COPY data/directory.json ./data/

# OFAC sanctioned-address snapshot (from the published 0xB10C list). Baked in so
# screening is ON by default -- Blackwall is a SUPERSET of the free KYT baseline.
# Refresh periodically with:  python sanctions.py sanctions.txt  (then redeploy).
COPY sanctions.txt ./

# ca-certificates for the RUNTIME sanctions refresh (BLACKWALL_SANCTIONS_REFRESH=1
# fetches the live OFAC list over HTTPS). No build-time network is used anymore.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# PREBUILT warm corpus (committed, not backfilled at build time). The free tier has no
# persistent disk, so the whole 292-payee reputation store + the per-category price
# index are built ONCE from PUBLIC Base USDC history and COMMITTED (data/
# reputation_seed.db.gz, data/category_index.json), then decompressed into the image
# here. The container boots WARM with the FULL manifest (~264 payees clear the gates,
# 5+ category baselines) -- no build-time crawl, so builds are fast (~1 min) and can't
# be broken by Blockscout/Bazaar downtime. Baked to /app/data (NOT the /data VOLUME
# below, whose build-time writes Docker discards); the free deploy points
# BLACKWALL_STORE / BLACKWALL_CATEGORY_INDEX here (render-free.yaml).
#
# REGENERATE the artifacts (periodically, so reputation doesn't go stale):
#   python3 chain_backfill.py --store /tmp/rep.db --payees-file data/seed_payees.txt --max-pages 2
#   python3 category_pricing.py --store /tmp/rep.db --out data/category_index.json --max-pages 8
#   python3 -c "import gzip,shutil; shutil.copyfileobj(open('/tmp/rep.db','rb'), gzip.open('data/reputation_seed.db.gz','wb',9))"
# then commit data/reputation_seed.db.gz + data/category_index.json and redeploy.
COPY data/reputation_seed.db.gz data/category_index.json data/divergence_index.json /app/prebuilt/
RUN mkdir -p /app/data \
    && python3 -c "import gzip,shutil; shutil.copyfileobj(gzip.open('/app/prebuilt/reputation_seed.db.gz','rb'), open('/app/data/reputation.db','wb'))" \
    && cp /app/prebuilt/category_index.json /app/data/category_index.json \
    && cp /app/prebuilt/divergence_index.json /app/data/divergence_index.json \
    && rm -rf /app/prebuilt \
    && chown -R blackwall /app/data

# Persistent state (SQLite reputation store + JSONL ledger) lives on a volume.
RUN mkdir -p /data && chown -R blackwall /data
USER blackwall
VOLUME ["/data"]

# NOTE: BLACKWALL_PORT is intentionally NOT set here. Blackwall falls back to the
# platform's $PORT (Render/Cloud Run/Heroku) when BLACKWALL_PORT is unset, and to
# 8402 otherwise -- so the image binds whatever port the host routes to. fly.toml
# sets BLACKWALL_PORT=8402 explicitly; local `docker run` defaults to 8402.
ENV BLACKWALL_HOST=0.0.0.0 \
    BLACKWALL_STORE=/data/reputation.db \
    BLACKWALL_LEDGER=/data/ledger.jsonl \
    BLACKWALL_INGEST=0 \
    BLACKWALL_SANCTIONS=/app/sanctions.txt \
    BLACKWALL_SANCTIONS_REFRESH=1 \
    BLACKWALL_CATEGORY_INDEX=/app/data/category_index.json \
    BLACKWALL_DIVERGENCE_INDEX=/app/data/divergence_index.json
# Simulation gates are OFF by default. To enable them, set at deploy time (see
# docs/RPC_SELFHOST.md and fly.toml for the full profile):
#   BLACKWALL_RPC_NODE_UPSTREAM -- run our own RPC front door in-process and route all
#                                  chain reads through it (cache + single-flight +
#                                  read-only allowlist). Point at YOUR node for zero
#                                  third-party query leakage. Set this FIRST.
#   BLACKWALL_SETTLEMENT_SIM=1  -- pre-signature settlement + EIP-3009 authorization sim
#   BLACKWALL_RWA_READINESS=1   -- only if agents buy tokenized RWAs
#
# Set at deploy time (NOT baked into the image):
#   BLACKWALL_PAY_TO       -- your funded EVM wallet (turns billing ON)
#   BLACKWALL_FACILITATOR  -- real x402 facilitator base URL
#   BLACKWALL_RECEIPT_KEY  -- secret for signing receipts / report tokens
# BLACKWALL_SANCTIONS defaults to the baked-in /app/sanctions.txt above (screening
# ON). Override to a volume path if you maintain your own list.

EXPOSE 8402
# Health: GET /healthz  ;  discovery: GET /.well-known/x402
CMD ["python", "blackwall.py"]
