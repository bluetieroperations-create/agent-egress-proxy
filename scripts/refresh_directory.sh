#!/usr/bin/env sh
# refresh_directory.sh -- regenerate the committed x402 trust directory, GUARDED.
#
# The sibling of refresh_seed.sh, for the OTHER stale corpus. `data/directory.json` is
# the ecosystem map `ecosystem_scan.py` derives from the CDP Bazaar crawl; five modules
# read it, and `payto_baseline` will only gate on it while it is DATED and younger than
# MAX_INDEX_AGE_DAYS (21). Nothing refreshed it, so it un-reached itself every three
# weeks -- and once the tripwire test landed, it turned the whole repo's CI red with it.
#
# DURABILITY LOCK, the same shape refresh_seed.sh uses: the rebuild goes to a TEMP
# candidate and only PROMOTES over the committed artifacts if directory_guard.py ACCEPTs
# it -- so a partial Bazaar crawl (12 entries instead of ~266), a candidate that lost the
# multi-host payees the gate keys on, or an undated one can never overwrite a good
# corpus. On REJECT the committed artifacts are left untouched and the script exits
# non-zero.
#
# THE REPUTATION STORE IS NEVER TOUCHED. `ecosystem_scan.py --backfill-store` is
# required and it WRITES, so it is pointed at a throwaway copy seeded from the committed
# store. Seeded, not empty, and that is load-bearing: the directory keeps only records
# whose `distinct_payers is not None`, so scanning against an empty store would emit a
# near-empty directory -- a "successful" run that silently guts the payTo index. The
# throwaway dies with the temp dir; refreshing the reputation corpus is refresh_seed.sh's
# job and stays that way.
#
# Usage:  sh scripts/refresh_directory.sh
#         BAZAAR_PAGES=40 BACKFILL_TOP=100 sh scripts/refresh_directory.sh
#
# Behind a proxy (local dev), export HTTPS_PROXY / SSL_CERT_FILE first.
set -eu

cd "$(dirname "$0")/.."

# Bazaar crawl depth. The documented full re-scan (docs/FEEDING.md) uses 80; extra pages
# can only ADD endpoints, and the guard rejects a candidate that lost coverage, so the
# cost of going deep is time rather than risk.
PAGES="${BAZAAR_PAGES:-80}"
# On-chain backfill for the most-active endpoints, which is what populates
# `distinct_payers` for entries the committed store has not seen.
TOP="${BACKFILL_TOP:-200}"
BACKFILL_PAGES="${BACKFILL_MAX_PAGES:-3}"

WORK="$(mktemp -d -t dirrefresh.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
TMP_STORE="$WORK/rep.db"
TMP_DIR="$WORK/directory.json"

echo "refresh_directory: seeding a THROWAWAY reputation store from the committed one ..."
echo "refresh_directory: (ecosystem_scan --backfill-store writes; the shipped store is never touched)"
python3 -c "import gzip,shutil,sys; shutil.copyfileobj(gzip.open(sys.argv[1],'rb'), open(sys.argv[2],'wb'))" \
    data/reputation_seed.db.gz "$TMP_STORE"

echo "refresh_directory: scanning the ecosystem (bazaar --max-pages $PAGES, backfill top $TOP) -> temp ..."
# ecosystem_scan dates the directory itself, writing the content-pinned sidecar beside
# --out-directory via payto_baseline.write_meta. That is the correct caller: it just
# generated the bytes, so it is the only one entitled to vouch for the date.
python3 ecosystem_scan.py \
    --backfill-store "$TMP_STORE" \
    --max-pages "$PAGES" \
    --backfill-top "$TOP" \
    --backfill-max-pages "$BACKFILL_PAGES" \
    --out-directory "$TMP_DIR"

echo "refresh_directory: running the directory guard (candidate vs committed) ..."
if python3 directory_guard.py --old data/directory.json --new "$TMP_DIR"; then
    echo "refresh_directory: guard ACCEPTED -- promoting candidate over committed artifacts."
    # BOTH files, and the sidecar LAST is not merely tidy. The sidecar pins the sha256 of
    # the corpus beside it; a run that moved the corpus and then died would leave the
    # PREVIOUS sidecar pinning bytes that no longer exist, which reads as undated -- the
    # gate silently stops working. Moving the corpus first and the sidecar immediately
    # after keeps that window as small as two renames on the same filesystem.
    mv "$TMP_DIR" data/directory.json
    mv "$WORK/directory.meta.json" data/directory.meta.json
    # POST-CONDITION, and not decoration. Those two renames are the one window where the
    # corpus and its sidecar can end up disagreeing, and EVERY way they disagree reads
    # identically from the outside: undated, which silently un-reaches the gate -- the
    # exact state this script exists to leave behind. So prove the promoted pair
    # verifies before reporting success. A half-finished promotion then fails LOUDLY
    # here, rather than quietly at the next boot, or three weeks later in CI.
    if ! python3 -c "import sys, payto_baseline as PB; sys.exit(0 if PB.index_age_days('data/directory.json') is not None else 1)"; then
        echo "refresh_directory: FATAL -- the promoted corpus does not verify against" >&2
        echo "refresh_directory: its sidecar. The working tree now holds a MISMATCHED" >&2
        echo "refresh_directory: pair, which reads as UNDATED and gates nothing." >&2
        echo "refresh_directory: restore it with: git checkout -- data/" >&2
        exit 1
    fi
    echo "refresh_directory: done. Directory age:"
    python3 -c "import payto_baseline as PB; print('directory is %.2f days old (MAX_INDEX_AGE_DAYS %d)' % (PB.index_age_days('data/directory.json'), PB.MAX_INDEX_AGE_DAYS))"
    echo ""
    echo "Now commit the refreshed artifacts:"
    echo "  git add data/directory.json data/directory.meta.json"
    echo "  git commit -m 'data: refresh the x402 trust directory'"
else
    echo "refresh_directory: guard REJECTED the candidate (see reasons above)." >&2
    echo "refresh_directory: committed artifacts left UNTOUCHED. Nothing to commit." >&2
    exit 1
fi
