"""
Tests for blockscout.py -- free on-chain enrichment. Each test states its mutation.
The HARD BOUNDARY (review-only, never a compliance clear/hit) is the key thing guarded.
"""
import unittest

import blockscout as B


class TestAddressEnrichment(unittest.TestCase):
    """
    Mutation notes:
      - review on something other than is_scam -> test_review_only_is_scam FAILS
        (would dress raw chain data up as a compliance signal).
      - count non-distinct tokens -> test_distinct_tokens FAILS.
    """
    def test_scam_flag_sets_review(self):
        s = B.address_enrichment({"hash": "0xa", "is_scam": True})
        self.assertTrue(s["review"])
        self.assertTrue(s["reasons"])                       # explains WHY, marked REVIEW-only

    def test_review_only_from_is_scam(self):
        # a contract with lots of tokens + no ENS is NOT review (those are descriptive)
        s = B.address_enrichment(
            {"hash": "0xa", "is_scam": False, "is_contract": True},
            [{"token": {"address_hash": "0xt%d" % i}} for i in range(40)])
        self.assertFalse(s["review"])                       # review is is_scam ONLY
        self.assertTrue(s["is_contract"])                   # surfaced as context
        self.assertEqual(s["distinct_incoming_tokens"], 40)

    def test_distinct_tokens(self):
        s = B.address_enrichment(
            {"hash": "0xa"},
            [{"token": {"address_hash": "0xT"}}, {"token": {"address_hash": "0xt"}},
             {"token": {"address_hash": "0xOTHER"}}])       # 0xT == 0xt (lowercased)
        self.assertEqual(s["distinct_incoming_tokens"], 2)

    def test_tags_and_ens(self):
        s = B.address_enrichment(
            {"hash": "0xa", "ens_domain_name": "foo.base.eth",
             "public_tags": [{"label": "Coinbase"}], "watchlist_names": ["vip"]})
        self.assertTrue(s["has_ens"])
        self.assertIn("Coinbase", s["tags"])
        self.assertIn("vip", s["tags"])

    def test_empty_input_safe(self):
        s = B.address_enrichment(None, None)
        self.assertFalse(s["review"])
        self.assertEqual(s["distinct_incoming_tokens"], 0)


class TestEnrichmentSource(unittest.TestCase):
    """
    Mutation notes:
      - let a fetch exception propagate -> test_fail_open FAILS (must never break a verdict).
      - skip the cache -> test_caches would re-fetch (perf, not correctness).
    """
    def _src(self, fetch):
        return B.BlockscoutEnrichmentSource(fetch=fetch, cache=True)

    def test_enrich_happy(self):
        def fetch(url, timeout):
            if url.endswith("/token-transfers?type=ERC-20"):
                return {"items": [{"token": {"address_hash": "0xt1"}}]}
            return {"hash": "0xcp", "is_scam": True}
        s = self._src(fetch).enrich("0xCP")
        self.assertTrue(s["review"])
        self.assertEqual(s["distinct_incoming_tokens"], 1)

    def test_fail_open_on_fetch_error(self):
        def boom(url, timeout):
            raise RuntimeError("403 / offline / rate-limited")
        self.assertIsNone(self._src(boom).enrich("0xCP"))   # None, never raises

    def test_empty_counterparty(self):
        self.assertIsNone(self._src(lambda *a: {}).enrich(""))
        self.assertIsNone(self._src(lambda *a: {}).enrich(None))

    def test_transfers_failure_keeps_review_signal(self):
        # REGRESSION (found in live eval): the token-transfers page times out on
        # high-volume addresses. It feeds only the descriptive distinct-tokens count,
        # so its failure must NOT discard the is_scam review from the address fetch.
        # Mutation: re-couple the two fetches in one try -> this returns None and FAILS.
        def fetch(url, timeout):
            if "token-transfers" in url:
                raise TimeoutError("transfers page too big / slow")
            return {"hash": "0xcp", "is_scam": True}
        s = self._src(fetch).enrich("0xCP")
        self.assertIsNotNone(s)
        self.assertTrue(s["review"])                        # review survives a slow transfers page
        self.assertEqual(s["distinct_incoming_tokens"], 0)

    def test_caches_per_counterparty(self):
        calls = {"n": 0}

        def fetch(url, timeout):
            calls["n"] += 1
            return {"hash": "0xcp", "is_scam": False}
        src = self._src(fetch)
        src.enrich("0xCP"); src.enrich("0xcp")              # same addr, different case
        self.assertEqual(calls["n"], 2)                     # 2 fetches for the FIRST call only


class TestBoundaryInVerdict(unittest.TestCase):
    """The hard boundary, enforced in the verdict: enrichment can ONLY push GO->HOLD.

    Mutation notes:
      - let enrichment reach the STOP path -> test_scam_holds_not_stops FAILS.
      - let enrichment clear a STOP -> test_cannot_clear_stop FAILS.
    """
    import blackwall as _bw
    GOOD = {"settlement_count": 500, "dispute_rate": 0.0, "distinct_payers": 30}
    SCAM = {"review": True, "is_scam": True, "reasons": ["scam tag"]}

    def test_scam_holds_not_stops(self):
        import blackwall as bw
        v = bw.decide_payment("0.09", dict(self.GOOD), ["0.09"], counterparty="0xV",
                              enrichment=self.SCAM)
        self.assertEqual(v["verdict"], "HOLD")              # never STOP on a crowd tag
        self.assertFalse(v["hard_stop"])

    def test_clean_enrichment_no_effect(self):
        import blackwall as bw
        v = bw.decide_payment("0.09", dict(self.GOOD), ["0.09"], counterparty="0xV",
                              enrichment={"review": False})
        self.assertEqual(v["verdict"], "GO")

    def test_cannot_clear_a_sanction(self):
        import blackwall as bw
        v = bw.decide_payment("0.09", dict(self.GOOD, sanctioned=True), ["0.09"],
                              counterparty="0xV", enrichment={"review": False})
        self.assertEqual(v["verdict"], "STOP")              # enrichment never clears
        self.assertTrue(v["hard_stop"])


if __name__ == "__main__":
    unittest.main()
