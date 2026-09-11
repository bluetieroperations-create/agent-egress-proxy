# Blackwall -- dev chores. Stdlib-only project; these just wrap the scripts.

.PHONY: test test-native test-client test-all refresh-seed check-seed-age

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

# The stdlib test suite -- what a contributor runs before pushing. This list is
# kept in step with .github/workflows/tests.yml by test_ci_coverage.py, which
# fails the build if either one omits a test file. Twelve files had silently
# drifted out of both before that lock existed.
test:
	python3 -m unittest \
	  test_aa_cosigner.py test_aave_reserve.py test_addresses.py \
	  test_advertised_prices.py test_ap_gate.py test_approvals.py \
	  test_asset_coverage.py test_auth_sim.py test_backed_oracle.py test_bench.py \
	  test_blackwall.py test_blockscout.py test_bounded_server.py \
	  test_calibration_lock.py test_calldata.py test_categories.py \
	  test_category_pricing.py test_cdp_auth.py test_chain_backfill.py \
	  test_check_seed_age.py test_ci_coverage.py test_confidence.py \
	  test_coverage_eval.py test_creds_local.py test_demo_flywheel.py \
	  test_deploy_manifest.py test_dex_price.py test_directory_liveness.py \
	  test_discovery.py test_discovery_crawl.py test_ecosystem_scan.py \
	  test_egress_proxy.py test_eip712.py test_facilitator.py test_fuzz_verdict.py \
	  test_holder_concentration.py test_honeypot.py test_http_util.py \
	  test_issuer_trust_gate.py test_keccak.py test_ledger.py test_mcp_http.py \
	  test_mcp_server.py test_payee_syntax.py test_payer_graph.py \
	  test_payer_reputation.py test_payload_sim.py test_price_corroboration.py \
	  test_price_integrity.py test_pyth_price.py test_rams_readiness.py \
	  test_ratelimit.py test_readiness.py test_receipt_signer.py test_redteam.py \
	  test_refresh_guard.py test_reputation_onchain.py test_reputation_store.py \
	  test_revert_scan.py test_rpc_node.py test_rwa_aggregate.py \
	  test_rwa_backfill.py test_rwa_balance.py test_rwa_ledger.py \
	  test_rwa_outcomes.py test_rwa_readiness.py test_rwa_report.py \
	  test_sanctions.py test_screen_payer.py test_secp256k1.py test_seller_intel.py test_solana_backfill.py test_volume_integrity.py test_secret_scan.py \
	  test_seller_audit.py test_settlement_sim.py test_settlement_velocity.py \
	  test_settlement_watch.py test_solana_rwa.py test_token_decimals.py \
	  test_tokenized_stock_registry.py test_traceipt_attest.py \
	  test_traceipt_ingest.py test_traceipt_pull.py test_traceipt_verify.py \
	  test_transfer_sim.py test_two_stage_signer.py test_upto_scheme.py \
	  test_user_agent.py test_verdict_anchor.py test_verdict_oracle.py test_x402.py \
	  test_x402_challenge.py \
	  test_billing_preflight.py test_reachability_ledger.py test_seller_portal.py test_seller_report.py

# Suites needing a third-party package, kept OUT of `make test` so the stdlib
# guarantee stays honest. remote_ledger requires AES-GCM by design (it refuses to
# mirror payment records in plaintext); x402_pay signs a real payment. Signatures
# from receipt_signer's two backends must be byte-identical, so it runs BOTH here
# and in `test` above -- that is the point of it, not a duplicate.
test-native:
	python3 -m pip install --quiet --only-binary :all: -r requirements-signing.txt
	python3 -m unittest test_receipt_signer.py test_remote_ledger.py

test-client:
	python3 -m pip install --quiet -r clients/requirements.txt
	python3 -m unittest test_x402_pay.py

# Everything the `unittest` CI job runs, in one go.
test-all: test test-native test-client
