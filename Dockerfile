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

# Warm-boot manifest: the payee list to backfill so the engine boots with real
# history (not cold-start HOLD). Pre-warm on the volume with:
#   python3 chain_backfill.py --store /data/reputation.db \
#     --payees-file data/seed_payees.txt --max-pages 3
COPY data/seed_payees.txt ./data/

# OFAC sanctioned-address snapshot (from the published 0xB10C list). Baked in so
# screening is ON by default -- Blackwall is a SUPERSET of the free KYT baseline.
# Refresh periodically with:  python sanctions.py sanctions.txt  (then redeploy).
COPY sanctions.txt ./

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
    BLACKWALL_SANCTIONS_REFRESH=1
# Set at deploy time (NOT baked into the image):
#   BLACKWALL_PAY_TO       -- your funded EVM wallet (turns billing ON)
#   BLACKWALL_FACILITATOR  -- real x402 facilitator base URL
#   BLACKWALL_RECEIPT_KEY  -- secret for signing receipts / report tokens
# BLACKWALL_SANCTIONS defaults to the baked-in /app/sanctions.txt above (screening
# ON). Override to a volume path if you maintain your own list.

EXPOSE 8402
# Health: GET /healthz  ;  discovery: GET /.well-known/x402
CMD ["python", "blackwall.py"]
