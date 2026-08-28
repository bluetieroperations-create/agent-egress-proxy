"""
Tests for discovery_crawl.py -- parse x402 discovery, extract payees/prices,
auto-feed the backfill. Fake fetch; each test states its mutation.
"""
import base64
import json
import tempfile
import urllib.error
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

    def test_accept_level_resource_dict_flattened(self):
        # a ResourceInfo dict on the ACCEPT itself -> flattened to its url (never a dict)
        doc = {"accepts": [{"payTo": P1, "amount": "5",
                            "resource": {"url": "https://svc/z"}}]}
        rec = D.extract_resources(doc)[0]
        self.assertEqual(rec["resource"], "https://svc/z")
        self.assertNotIsInstance(rec["resource"], dict)

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



class PriceObservationsRespectDecimals(unittest.TestCase):
    """price_observations divided EVERY asset by 10^6 ("USDC (6dp) assumed").

    A payee that offers several chains -- CoinMarketCap advertises Base USDC and
    BSC 18-decimal options for the same resource -- had its 18-dp options read as
    10^12 times too large. Measured on the live catalog: 0.01 became 10,000,000,000.

    This is the same defect AUDIT_ZEROCUSTOMER filed as H2 and fixed in
    ecosystem_scan's price layer; it was never fixed here. Not on the verdict path
    (reputation_store's price_observations come from the settlements table), but
    this function exists to feed peer-group price baselines, and a 10^12 outlier
    poisons any baseline built from it.
    """

    BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    BSC_18DP = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"

    def _rec(self, asset, atomic):
        return {"payTo": "0x" + "ab" * 20, "resource": "https://s/api",
                "asset": asset, "network": "eip155:8453", "price_atomic": atomic}

    def test_known_6dp_usdc_is_converted(self):
        obs = D.price_observations([self._rec(self.BASE_USDC, 10000)])
        self.assertEqual([o["amount"] for o in obs], ["0.01"])

    def test_unknown_decimals_asset_is_excluded(self):
        # Mutation: converting every asset at 6dp. 0.01 of an 18-dp token would
        # be reported as 10000000000.000000 -- a 10^12 error presented as a price.
        obs = D.price_observations([self._rec(self.BSC_18DP, 10000000000000000)])
        self.assertEqual(obs, [], "unknown-decimals asset must not yield a USD price")

    def test_a_multi_chain_payee_keeps_only_its_comparable_option(self):
        # The real shape: one resource, several accepts, different chains.
        obs = D.price_observations([self._rec(self.BASE_USDC, 10000),
                                    self._rec(self.BSC_18DP, 10000000000000000)])
        self.assertEqual([o["amount"] for o in obs], ["0.01"])

    def test_asset_match_is_case_insensitive(self):
        # Mutation: comparing the checksummed address without .lower().
        obs = D.price_observations([self._rec(self.BASE_USDC.lower(), 10000)])
        self.assertEqual(len(obs), 1)

    def test_missing_or_non_positive_amounts_are_dropped(self):
        for atomic in (None, 0, -1, "junk"):
            self.assertEqual(D.price_observations([self._rec(self.BASE_USDC, atomic)]),
                             [], repr(atomic))

    def test_missing_asset_is_excluded(self):
        # Mutation: treating an absent asset as USDC. Decimals unknown means the
        # number is not comparable, and guessing is how the 10^12 error happened.
        self.assertEqual(D.price_observations([self._rec(None, 10000)]), [])


class CrawlReadsHeaderCarriedChallenges(unittest.TestCase):
    """A 402 is a price quote, not a failure -- and its requirements may sit in
    the WWW-Authenticate header rather than the body."""

    ACCEPTS = [{"payTo": "0x" + "ab" * 20, "maxAmountRequired": "25000",
                "asset": "0x" + "cd" * 20, "network": "base",
                "resource": "https://seller/api"}]

    @staticmethod
    def _http_error(code=402, headers=None, body=b""):
        class _Stream:
            def read(self, *a):
                return body

            def close(self):
                pass

        return urllib.error.HTTPError("http://seller/", code, "why",
                                      headers or {}, _Stream())

    @classmethod
    def _hdr(cls, accepts):
        blob = base64.b64encode(json.dumps({"accepts": accepts}).encode()).decode()
        return {"WWW-Authenticate": 'X402 requirements="%s"' % blob}

    def test_harvests_a_header_carried_402(self):
        # Mutation: reverting crawl() to a bare `except Exception: continue`.
        # Such a seller was previously invisible, so neither its payee nor its
        # advertised price ever reached reputation.
        def fetch(url):
            raise self._http_error(headers=self._hdr(self.ACCEPTS))

        got = D.crawl(["https://seller/api"], fetch=fetch)
        self.assertEqual([r["payTo"] for r in got], [self.ACCEPTS[0]["payTo"]])

    def test_harvests_a_body_carried_402(self):
        # Mutation: handling only the header carrier. get_json raises on ANY
        # 402, so a body-carried challenge from a raising transport was lost too.
        def fetch(url):
            raise self._http_error(body=json.dumps({"accepts": self.ACCEPTS}).encode())

        got = D.crawl(["https://seller/api"], fetch=fetch)
        self.assertEqual([r["payTo"] for r in got], [self.ACCEPTS[0]["payTo"]])

    def test_carries_the_source_url_as_the_resource(self):
        # Mutation: building doc = {"accepts": accepts} without the URL. A
        # header-carried challenge has no `resource` of its own, so the record
        # lands with resource=None -- silently costing the category classifier
        # its only input and price observations their per-resource key. Caught
        # by running the real crawl against blockrun.ai, not by a unit test.
        url = "https://seller/api/v1/quote"
        # A header-carried challenge typically names no resource of its own --
        # that is exactly why the source URL has to supply it.
        bare = [{k: v for k, v in self.ACCEPTS[0].items() if k != "resource"}]

        def fetch(_):
            raise self._http_error(headers=self._hdr(bare))

        got = D.crawl([url], fetch=fetch)
        self.assertEqual([r["resource"] for r in got], [url])

    def test_an_accepts_own_resource_still_wins(self):
        # Mutation: overwriting the accept's resource with the source URL.
        # A challenge that names its own resource is authoritative.
        own = "https://seller/real-resource"
        accepts = [dict(self.ACCEPTS[0], resource=own)]

        def fetch(_):
            raise self._http_error(headers=self._hdr(accepts))

        got = D.crawl(["https://seller/probed-here"], fetch=fetch)
        self.assertEqual([r["resource"] for r in got], [own])

    def test_a_real_failure_is_still_skipped_not_fatal(self):
        # Mutation: letting the new branch re-raise. A dead host, a 500, and a
        # 402 with nothing readable must all stay non-fatal for the crawl.
        def dead(url):
            raise OSError("connection refused")

        def five_hundred(url):
            raise self._http_error(code=500)

        def opaque(url):
            raise self._http_error(body=b"<html>402</html>")

        for bad in (dead, five_hundred, opaque):
            self.assertEqual(D.crawl(["https://x/"], fetch=bad), [], bad.__name__)

    def test_one_bad_source_does_not_truncate_the_crawl(self):
        # Mutation: `continue` becoming `break`.
        def mixed(url):
            if url.endswith("bad"):
                raise OSError("nope")
            raise self._http_error(headers=self._hdr(self.ACCEPTS))

        got = D.crawl(["https://x/bad", "https://x/good"], fetch=mixed)
        self.assertEqual(len(got), 1)

if __name__ == "__main__":
    unittest.main()
