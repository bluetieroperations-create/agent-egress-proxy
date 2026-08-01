"""
Tests for discovery_crawl.py -- parse x402 discovery, extract payees/prices,
auto-feed the backfill. Fake fetch; each test states its mutation.
"""
import tempfile
import unittest

import discovery_crawl as D

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
P1 = "0x" + "a" * 40
P2 = "0x" + "b" * 40


def _accept(payto, amount="1000", resource="https://svc/x", key="amount"):
    return {"scheme": "exact", "network": "base", "asset": USDC,
            "payTo": payto, key: amount, "resource": resource}


class TestExtract(unittest.TestCase):
    """
    Mutation notes:
      - only read v2 `amount` (not v1 `maxAmountRequired`) -> test_v1_v2 FAILS.
      - not recurse into items/resources -> test_nested FAILS.
      - accept a non-address payTo -> test_bad_payto_dropped FAILS.
    """
    def test_402_body(self):
        doc = {"x402Version": 2, "accepts": [_accept(P1), _accept(P2)]}
        recs = D.extract_resources(doc)
        self.assertEqual({r["payTo"] for r in recs}, {P1, P2})
        self.assertEqual(recs[0]["price_atomic"], 1000)

    def test_v1_and_v2_amount(self):
        v1 = {"accepts": [_accept(P1, amount="2000", key="maxAmountRequired")]}
        self.assertEqual(D.extract_resources(v1)[0]["price_atomic"], 2000)

    def test_nested_items(self):
        doc = {"items": [{"accepts": [_accept(P1)]},
                         {"resources": [{"accepts": [_accept(P2)]}]}]}
        self.assertEqual({r["payTo"] for r in D.extract_resources(doc)}, {P1, P2})

    def test_v2_resourceinfo_url(self):
        doc = {"resource": {"url": "https://svc/y"},
               "accepts": [{"payTo": P1, "asset": USDC, "network": "base", "amount": "5"}]}
        self.assertEqual(D.extract_resources(doc)[0]["resource"], "https://svc/y")

    def test_bad_payto_dropped(self):
        doc = {"accepts": [{"payTo": "not-an-addr", "amount": "1"},
                           _accept(P1)]}
        recs = D.extract_resources(doc)
        self.assertEqual([r["payTo"] for r in recs], [P1])

    def test_garbage_never_raises(self):
        for junk in (None, 5, "str", [], {}, {"accepts": "x"}, {"accepts": [1, 2]}):
            self.assertEqual(D.extract_resources(junk), [])

    def test_depth_bounded(self):
        doc = cur = {}
        for _ in range(20):
            cur["items"] = [{}]
            cur = cur["items"][0]
        cur["accepts"] = [_accept(P1)]
        self.assertEqual(D.extract_resources(doc), [])   # deeper than the cap -> empty


class TestDerive(unittest.TestCase):
    def test_payees_deduped(self):
        recs = D.extract_resources({"accepts": [_accept(P1), _accept(P1), _accept(P2)]})
        self.assertEqual(D.payees(recs), [P1, P2])

    def test_price_observations_human(self):
        recs = D.extract_resources({"accepts": [_accept(P1, amount="90000")]})
        obs = D.price_observations(recs)
        self.assertEqual(obs[0]["amount"], "0.09")        # 90000 atomic USDC -> 0.09
        self.assertEqual(obs[0]["counterparty"], P1)

    def test_price_observations_skip_zero(self):
        recs = D.extract_resources({"accepts": [_accept(P1, amount="0")]})
        self.assertEqual(D.price_observations(recs), [])


class TestCrawl(unittest.TestCase):
    """Mutation note: a raising source aborts the crawl -> test_skips_bad FAILS."""
    def test_aggregates_sources(self):
        docs = {"u1": {"accepts": [_accept(P1)]}, "u2": {"accepts": [_accept(P2)]}}
        recs = D.crawl(["u1", "u2"], fetch=lambda u, **k: docs[u])
        self.assertEqual({r["payTo"] for r in recs}, {P1, P2})

    def test_skips_bad_source(self):
        def fetch(u, **k):
            if u == "bad":
                raise ConnectionError("down")
            return {"accepts": [_accept(P1)]}
        recs = D.crawl(["bad", "ok"], fetch=fetch)
        self.assertEqual([r["payTo"] for r in recs], [P1])


class TestCrawlBazaar(unittest.TestCase):
    """
    Mutation notes:
      - not advancing offset -> test_paginates loops the same page / never stops.
      - not stopping at `total` -> test_paginates over-fetches.
    """
    def _bazaar(self, pages):
        """pages: list of item-lists; serves them by offset with a pagination.total."""
        total = sum(len(pg) for pg in pages)

        def fetch(url, timeout=12):
            # parse offset/limit from the URL
            import urllib.parse as up
            q = up.parse_qs(up.urlsplit(url).query)
            offset = int(q.get("offset", ["0"])[0])
            limit = int(q.get("limit", ["100"])[0])
            idx = offset // max(limit, 1)
            items = pages[idx] if idx < len(pages) else []
            return {"items": items, "pagination": {"limit": limit, "offset": offset,
                                                    "total": total}, "x402Version": 2}
        return fetch

    def test_paginates_to_total(self):
        pages = [[{"accepts": [_accept(P1)]}], [{"accepts": [_accept(P2)]}]]
        recs = D.crawl_bazaar(fetch=self._bazaar(pages), page_limit=1, max_pages=10)
        self.assertEqual({r["payTo"] for r in recs}, {P1, P2})

    def test_stops_at_max_pages(self):
        pages = [[{"accepts": [_accept(P1)]}]] * 5
        recs = D.crawl_bazaar(fetch=self._bazaar(pages), page_limit=1, max_pages=2)
        self.assertEqual(len(recs), 2)          # only 2 pages walked

    def test_empty_page_stops(self):
        def fetch(url, timeout=12):
            return {"items": [], "pagination": {"total": 0}}
        self.assertEqual(D.crawl_bazaar(fetch=fetch), [])

    def test_crawl_all_combines(self):
        pages = [[{"accepts": [_accept(P1)]}]]
        recs = D.crawl_all(extra_sources=["u"], bazaar=True,
                           fetch=lambda u, **k: (self._bazaar(pages)(u)
                                                 if "discovery" in u or "limit" in u
                                                 else {"accepts": [_accept(P2)]}),
                           max_pages=1)
        self.assertEqual({r["payTo"] for r in recs}, {P1, P2})


class TestCrawlAndBackfill(unittest.TestCase):
    """Discovery -> payees -> reputation, end to end (fake chain transport)."""
    def test_end_to_end(self):
        from reputation_store import ReputationStore
        import chain_backfill as CB
        store = ReputationStore(tempfile.mkdtemp() + "/r.db")
        disc = {"accepts": [_accept(P1)]}

        def chain_fetch(addr, params):     # one inbound USDC transfer to P1
            if addr.lower() == P1:
                return ([{"token": {"address_hash": USDC},
                          "total": {"value": "90000", "decimals": 6},
                          "to": {"hash": P1}, "from": {"hash": P2},
                          "transaction_hash": "0x" + "1" * 64,
                          "timestamp": "2026-07-01T00:00:00Z"}], None)
            return ([], None)
        summary = D.crawl_and_backfill(store, ["u"], fetch=lambda u, **k: disc,
                                       chain_fetch=chain_fetch)
        self.assertEqual(summary["payees"], 1)
        self.assertEqual(summary["ingested"], 1)
        self.assertGreaterEqual(store.lookup(P1).get("settlement_count", 0), 1)


if __name__ == "__main__":
    unittest.main()
