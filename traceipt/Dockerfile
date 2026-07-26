# Traceipt — minimal container for the receipt service.
# stdlib + `cryptography` only; no build toolchain needed on slim.
FROM python:3.11-slim

# Don't buffer stdout (so the startup banner + logs show immediately).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The package.
COPY traceipt/ ./traceipt/

# Persistent state (ledger DB + issuer key) lives here; mount a volume on it.
# The key is created 0600 on first run if absent — but in production you
# should mount a pre-generated key as a secret instead (see DEPLOY.md).
ENV RECEIPTS_DB=/data/receipts.db \
    RECEIPTS_KEY=/data/issuer_ed25519.pem \
    RECEIPTS_HOST=0.0.0.0 \
    RECEIPTS_PORT=8402
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8402

# Defaults target the FREE-TIER TESTNET DEMO: Base Sepolia, dev gate (any
# X-PAYMENT header is accepted so anyone can try the flow without a wallet),
# mock settlement (so a demo caller needn't supply a real testnet tx).
# Override these envs for production — see DEPLOY.md.
ENV RECEIPTS_CHAIN=base-sepolia \
    X402_GATE=dev \
    RECEIPTS_SETTLEMENT=mock

CMD ["python", "-m", "traceipt.service"]
