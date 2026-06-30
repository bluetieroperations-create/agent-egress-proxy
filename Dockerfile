# Blackwall verdict service -- stdlib-only, no pip dependencies.
FROM python:3.12-slim

# Non-root.
RUN useradd -m -u 10001 blackwall
WORKDIR /app

# Source (stdlib only -- nothing to pip install).
COPY blackwall.py x402.py ledger.py addresses.py reputation_store.py \
     reputation_onchain.py settlement_watch.py discovery.py mcp_server.py \
     facilitator_sim.py sanctions.py readiness.py ./

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
    BLACKWALL_INGEST=1
# Set at deploy time (NOT baked into the image):
#   BLACKWALL_PAY_TO       -- your funded EVM wallet (turns billing ON)
#   BLACKWALL_FACILITATOR  -- real x402 facilitator base URL
#   BLACKWALL_RECEIPT_KEY  -- secret for signing receipts / report tokens
#   BLACKWALL_SANCTIONS    -- path to an OFAC list on the volume (optional)

EXPOSE 8402
# Health: GET /healthz  ;  discovery: GET /.well-known/x402
CMD ["python", "blackwall.py"]
