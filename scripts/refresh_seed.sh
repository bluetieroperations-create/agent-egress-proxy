#!/usr/bin/env sh
# refresh_seed.sh -- regenerate the committed prebuilt free-tier corpus, GUARDED.
#
# The free tier ships a FROZEN reputation store (data/reputation_seed.db.gz) + category
# price index (data/category_index.json) + divergence index. They go stale (see
# check_seed_age.py: the `stale` gate makes the whole corpus HOLD ~90-120 days after the
# build). This rebuilds them from live on-chain history.
#
# DURABILITY LOCK (Stage 2, docs/DATA_COMPLETENESS.md): the rebuild goes to a TEMP
# candidate first and only PROMOTES over the committed artifacts if refresh_guard.py
# ACCEPTs it -- so a partial crawl (sparse store) or a no-progress no-op can never
# overwrite a good corpus. On REJECT the committed artifacts are left untouched and the
# script exits non-zero.
#
# Usage:  sh scripts/refresh_seed.sh            (BACKFILL_PAGES=2 by default)
#         BACKFILL_PAGES=3 sh scripts/refresh_seed.sh
#
# Behind a proxy (local dev), export HTTPS_PROXY / SSL_CERT_FILE first; on a clean host
# with direct egress, no env is needed.
set -eu

cd "$(dirname "$0")/.."

PAGES="${BACKFILL_PAGES:-2}"
WORK="$(mktemp -d -t seedrefresh.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
TMP_STORE="$WORK/rep.db"
TMP_GZ="$WORK/reputation_seed.db.gz"
TMP_CAT="$WORK/category_index.json"
TMP_DIV="$WORK/divergence_index.json"

echo "refresh_seed: backfilling data/seed_payees.txt (--max-pages $PAGES) -> temp ..."
python3 chain_backfill.py --store "$TMP_STORE" \
    --payees-file data/seed_payees.txt --max-pages "$PAGES"

echo "refresh_seed: building per-category price index -> temp ..."
python3 category_pricing.py --store "$TMP_STORE" --out "$TMP_CAT" --max-pages 8

echo "refresh_seed: building advertised-vs-settled divergence index -> temp ..."
python3 price_integrity.py --store "$TMP_STORE" --out "$TMP_DIV" --max-pages 8

echo "refresh_seed: gzipping candidate store ..."
python3 -c "import gzip,shutil,sys; shutil.copyfileobj(open(sys.argv[1],'rb'), gzip.open(sys.argv[2],'wb',9))" "$TMP_STORE" "$TMP_GZ"

echo "refresh_seed: running the refresh guard (candidate vs committed) ..."
if python3 refresh_guard.py --old data/reputation_seed.db.gz --new "$TMP_GZ"; then
    echo "refresh_seed: guard ACCEPTED -- promoting candidate over committed artifacts."
    mv "$TMP_GZ"  data/reputation_seed.db.gz
    mv "$TMP_CAT" data/category_index.json
    mv "$TMP_DIV" data/divergence_index.json
    echo "refresh_seed: done. Freshness:"
    python3 check_seed_age.py || true
    echo ""
    echo "Now commit the refreshed artifacts:"
    echo "  git add data/reputation_seed.db.gz data/category_index.json data/divergence_index.json"
    echo "  git commit -m 'data: refresh prebuilt seed store'"
    echo "  # then redeploy (Render rebuilds the image)"
else
    echo "refresh_seed: guard REJECTED the candidate (see reasons above)." >&2
    echo "refresh_seed: committed artifacts left UNTOUCHED. Nothing to commit." >&2
    exit 1
fi
