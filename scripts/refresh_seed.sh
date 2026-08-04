#!/usr/bin/env sh
# refresh_seed.sh -- regenerate the committed prebuilt free-tier corpus.
#
# The free tier ships a FROZEN reputation store (data/reputation_seed.db.gz) + category
# price index (data/category_index.json). Both go stale (see check_seed_age.py: the
# `stale` gate makes the whole corpus HOLD ~90-120 days after the build). Run this to
# rebuild them from live on-chain history, then COMMIT the two artifacts and redeploy.
#
# Usage:  sh scripts/refresh_seed.sh            (BACKFILL_PAGES=2 by default)
#         BACKFILL_PAGES=3 sh scripts/refresh_seed.sh
#
# Behind a proxy (local dev), export HTTPS_PROXY / SSL_CERT_FILE first; on a clean host
# with direct egress, no env is needed.
set -eu

cd "$(dirname "$0")/.."

PAGES="${BACKFILL_PAGES:-2}"
TMP_STORE="$(mktemp -t rep.XXXXXX.db)"
trap 'rm -f "$TMP_STORE"' EXIT

echo "refresh_seed: backfilling data/seed_payees.txt (--max-pages $PAGES) ..."
python3 chain_backfill.py --store "$TMP_STORE" \
    --payees-file data/seed_payees.txt --max-pages "$PAGES"

echo "refresh_seed: building per-category price index ..."
python3 category_pricing.py --store "$TMP_STORE" \
    --out data/category_index.json --max-pages 8

echo "refresh_seed: gzipping store -> data/reputation_seed.db.gz ..."
python3 -c "import gzip,shutil,sys; shutil.copyfileobj(open(sys.argv[1],'rb'), gzip.open('data/reputation_seed.db.gz','wb',9))" "$TMP_STORE"

echo "refresh_seed: done. Freshness:"
python3 check_seed_age.py || true
echo ""
echo "Now commit the refreshed artifacts:"
echo "  git add data/reputation_seed.db.gz data/category_index.json"
echo "  git commit -m 'data: refresh prebuilt seed store'"
echo "  # then redeploy (Render rebuilds the image)"
