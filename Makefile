# Blackwall -- dev chores. Stdlib-only project; these just wrap the scripts.

.PHONY: test refresh-seed check-seed-age

# Regenerate the committed free-tier corpus (reputation store + category index) from
# live on-chain history. Run periodically (before the ~90-day stale cliff), then commit
# data/reputation_seed.db.gz + data/category_index.json and redeploy. Override depth
# with BACKFILL_PAGES=N.
refresh-seed:
	sh scripts/refresh_seed.sh

# Report how fresh the committed seed store is; exit non-zero once it's within the
# refresh window (so CI can gate on it). See check_seed_age.py.
check-seed-age:
	python3 check_seed_age.py

# The full test suite (canonical command lives in CLAUDE.md).
test:
	python -m unittest \
	  test_egress_proxy.py test_blackwall.py test_ledger.py test_reputation_onchain.py \
	  test_settlement_watch.py test_addresses.py test_x402.py test_mcp_server.py \
	  test_reputation_store.py test_facilitator.py test_discovery.py test_sanctions.py \
	  test_readiness.py test_ap_gate.py test_cdp_auth.py test_creds_local.py \
	  test_traceipt_attest.py test_traceipt_ingest.py test_traceipt_verify.py \
	  test_payload_sim.py test_traceipt_pull.py test_keccak.py test_secp256k1.py \
	  test_eip712.py test_calldata.py test_seller_audit.py test_aa_cosigner.py \
	  test_chain_backfill.py test_discovery_crawl.py test_ecosystem_scan.py \
	  test_http_util.py test_payer_graph.py test_payer_reputation.py \
	  test_settlement_velocity.py test_confidence.py test_redteam.py \
	  test_demo_flywheel.py test_verdict_anchor.py test_categories.py \
	  test_category_pricing.py test_check_seed_age.py
