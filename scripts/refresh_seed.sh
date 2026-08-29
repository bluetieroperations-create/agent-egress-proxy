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
# Usage:  sh scripts/refresh_seed.sh            (BACKFILL_PAGES=4 by default)
#         BACKFILL_PAGES=6 sh scripts/refresh_seed.sh
#
# Behind a proxy (local dev), export HTTPS_PROXY / SSL_CERT_FILE first; on a clean host
# with direct egress, no env is needed.
set -eu

cd "$(dirname "$0")/.."

# Depth. Raised from 2 now that the refresh MERGES: extra pages can only ADD payers
# (which is what the Sybil gate needs), and if some fetches fail nothing is lost. Kept
# modest on purpose -- more pages means more requests against the same keyless,
# rate-limited Blockscout endpoint that already produced zero-item fetches, so pushing
# this higher trades coverage risk for freshness gain.
PAGES="${BACKFILL_PAGES:-4}"
WORK="$(mktemp -d -t seedrefresh.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
TMP_STORE="$WORK/rep.db"
TMP_GZ="$WORK/reputation_seed.db.gz"
TMP_CAT="$WORK/category_index.json"
TMP_DIV="$WORK/divergence_index.json"

# MERGE, don't REPLACE. Seed the candidate FROM the committed store, then crawl into
# it -- so the refresh ACCUMULATES history instead of re-sampling a narrow window.
#
# Why this matters (measured on the 2026-08-17 run): a replace-style refresh dropped 41
# established payees ENTIRELY -- median 100 settlements each, and all still visibly
# active on-chain -- because their fetch returned 0 items (rate limiting, not inactivity).
# 55 more lost enough distinct payers to trip the Sybil gate. Net effect: payees able to
# earn a GO fell 237 -> 207. A settlement that happened last month still happened;
# discarding it because today's crawl was throttled is simply wrong, and no amount of
# extra pagination fixes a fetch that returned nothing.
#
# The store's natural key is UNIQUE(tx_hash, counterparty, amount) and re-ingest is
# idempotent, so seeding from the committed store and re-crawling adds only genuinely
# new settlements. A failed fetch now costs FRESHNESS, never COVERAGE.
echo "refresh_seed: seeding candidate from the committed store (merge, not replace) ..."
python3 -c "import gzip,shutil,sys; shutil.copyfileobj(gzip.open(sys.argv[1],'rb'), open(sys.argv[2],'wb'))" \
    data/reputation_seed.db.gz "$TMP_STORE"

echo "refresh_seed: backfilling data/seed_payees.txt (--max-pages $PAGES) -> temp ..."
python3 chain_backfill.py --store "$TMP_STORE" \
    --payees-file data/seed_payees.txt --max-pages "$PAGES"

# INDEX DEPTH. The guard below validates the STORE only -- payees, edges, age --
# so a shallow index build passes it while quietly shrinking coverage. Measured
# 2026-08-28 on the same store: 8 pages produced FOUR category baselines, 24
# produced SEVEN, restoring `dev-tools` and surfacing `commerce` and
# `content-media` that 8 pages never reached. A missing baseline is fail-open, so
# the cost is a gate that silently does nothing rather than a wrong verdict --
# which is exactly why it needs saying out loud here.
INDEX_PAGES="${INDEX_PAGES:-24}"

echo "refresh_seed: building per-category price index -> temp (--max-pages $INDEX_PAGES) ..."
python3 category_pricing.py --store "$TMP_STORE" --out "$TMP_CAT" --max-pages "$INDEX_PAGES"

echo "refresh_seed: building advertised-vs-settled divergence index -> temp ..."
python3 price_integrity.py --store "$TMP_STORE" --out "$TMP_DIV" --max-pages "$INDEX_PAGES"

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
