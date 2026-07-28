"""
Tests for traceipt_pull.py -- pull signed Traceipt receipts into reputation.

Each test states the mutation it kills. Envelopes are signed with cdp_auth's
pure-Python Ed25519 (via test_traceipt_verify.make_signed_envelope), so no
`cryptography` binding is needed and the signatures are genuine.
"""
import copy
import tempfile
import unittest

import traceipt_pull as PULL
from test_traceipt_verify import make_signed_envelope


def _receipt(*, payee, payer, tx, amount="90000", kind="payment", issued="2026-07-01T00:00:00Z"):
    return {
        "receipt_id": "rcpt_" + "a" * 20, "kind": kind, "issued_at": issued,
        "settlement": {"chain": "base", "tx_hash": tx, "asset": "USDC",
                       "asset_contract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                       "amount_base_units": amount, "payer": payer, "payee": payee,
                       "verification_method": "rpc", "verified": True},
    }


class _FakeTraceipt:
    """A fake Traceipt HTTP surface: serves signed envelopes by id + a JWKS,
    all under ONE issuer key so signatures verify. 404s unknown ids."""

    def __init__(self):
        self.jwks = None
        self.envelopes = {}   # receipt_id -> envelope
        self.hits = []

    def add(self, receipt_id, payload, *, tamper=False, seed=b"\x11" * 32):
        env, jwks = make_signed_envelope(payload, seed=seed)
        self.jwks = self.jwks or jwks       # first (genuine issuer) key becomes THE jwks
        if tamper:
            env = copy.deepcopy(env)
            env["payload"]["settlement"]["amount_base_units"] = "999999999"
        self.envelopes[receipt_id] = env
        return env

    def get(self, url, timeout):
        self.hits.append(url)
        rid = url.rsplit("/receipts/", 1)[-1]
        env = self.envelopes.get(rid)
        return (200, env) if env is not None else (404, {"error": "unknown receipt_id"})

    def fetch_jwks(self, base_url, *, timeout=10):
        return self.jwks


def _store():
    from reputation_store import ReputationStore
    d = tempfile.mkdtemp()
    return ReputationStore(d + "/rep.db")


class TestPullAndIngest(unittest.TestCase):
    """
    Mutation notes:
      - ingest WITHOUT the signature gate -> test_forged_not_ingested FAILS.
      - count fetched as verified -> test_summary_stage_counts FAILS.
      - don't fetch JWKS / fail-open on missing JWKS -> test_no_jwks_ingests_nothing FAILS.
    """
    def test_genuine_receipts_feed_reputation(self):
        fake = _FakeTraceipt()
        payee = "0x" + "a" * 40
        fake.add("r1", _receipt(payee=payee, payer="0x" + "b" * 40, tx="0x" + "1" * 64))
        fake.add("r2", _receipt(payee=payee, payer="0x" + "c" * 40, tx="0x" + "2" * 64))
        store = _store()
        summary = PULL.pull_and_ingest(store, "https://api.traceipt.xyz",
                                       ["r1", "r2"], _get=fake.get,
                                       _fetch_jwks=fake.fetch_jwks)
        self.assertEqual(summary["ingested"], 2)
        rec = store.lookup(payee)
        self.assertGreaterEqual(rec.get("settlement_count", 0), 2)
        self.assertTrue(any(str(o.get("amount")) == "0.09"
                            for o in (rec.get("price_observations") or [])))

    def test_forged_receipt_not_ingested(self):
        # a tampered envelope is fetched but its signature no longer verifies
        fake = _FakeTraceipt()
        fake.add("good", _receipt(payee="0x" + "a" * 40, payer="0x" + "b" * 40,
                                  tx="0x" + "1" * 64))
        fake.add("forged", _receipt(payee="0x" + "e" * 40, payer="0x" + "f" * 40,
                                    tx="0x" + "9" * 64), tamper=True)
        store = _store()
        summary = PULL.pull_and_ingest(store, "https://x", ["good", "forged"],
                                       _get=fake.get, _fetch_jwks=fake.fetch_jwks)
        self.assertEqual(summary["fetched"], 2)
        self.assertEqual(summary["verified"], 1)      # forged dropped
        self.assertEqual(summary["ingested"], 1)
        self.assertEqual(store.lookup("0x" + "e" * 40).get("settlement_count", 0), 0)

    def test_summary_stage_counts(self):
        # 3 requested, 1 is a 404 -> fetched 2; one of those is a credit note
        # (authentic but not a payment) -> verified 1
        fake = _FakeTraceipt()
        fake.add("pay", _receipt(payee="0x" + "a" * 40, payer="0x" + "b" * 40,
                                 tx="0x" + "1" * 64))
        fake.add("credit", _receipt(payee="0x" + "a" * 40, payer="0x" + "b" * 40,
                                    tx="0x" + "2" * 64, kind="credit"))
        summary = PULL.pull_and_ingest(_store(), "https://x",
                                       ["pay", "credit", "missing"],
                                       _get=fake.get, _fetch_jwks=fake.fetch_jwks)
        self.assertEqual(summary["requested"], 3)
        self.assertEqual(summary["fetched"], 2)       # 'missing' 404'd
        self.assertEqual(summary["verified"], 1)      # credit note not a payment
        self.assertEqual(summary["ingested"], 1)

    def test_no_jwks_ingests_nothing(self):
        # JWKS fetch fails -> nothing can be authenticated -> ingest nothing
        fake = _FakeTraceipt()
        fake.add("r1", _receipt(payee="0x" + "a" * 40, payer="0x" + "b" * 40,
                                tx="0x" + "1" * 64))
        summary = PULL.pull_and_ingest(_store(), "https://x", ["r1"],
                                       _get=fake.get,
                                       _fetch_jwks=lambda u, timeout=10: None)
        self.assertEqual(summary["ingested"], 0)
        self.assertEqual(summary["reason"], "jwks_unavailable")

    def test_idempotent_repull(self):
        # pulling the same receipts twice ingests them once (idempotent on tx_hash)
        fake = _FakeTraceipt()
        fake.add("r1", _receipt(payee="0x" + "a" * 40, payer="0x" + "b" * 40,
                                tx="0x" + "1" * 64))
        store = _store()
        first = PULL.pull_and_ingest(store, "https://x", ["r1"],
                                     _get=fake.get, _fetch_jwks=fake.fetch_jwks)
        second = PULL.pull_and_ingest(store, "https://x", ["r1"],
                                      _get=fake.get, _fetch_jwks=fake.fetch_jwks)
        self.assertEqual(first["ingested"], 1)
        self.assertEqual(second["ingested"], 0)       # already known


class TestFetchEnvelope(unittest.TestCase):
    """Mutation note: treat a non-200 as a valid envelope -> test_404_is_none FAILS."""

    def test_200_returns_envelope(self):
        fake = _FakeTraceipt()
        fake.add("r1", _receipt(payee="0x" + "a" * 40, payer="0x" + "b" * 40,
                                tx="0x" + "1" * 64))
        env = PULL.fetch_envelope("https://x", "r1", _get=fake.get)
        self.assertIn("signature", env)
        self.assertIn("/receipts/r1", fake.hits[0])

    def test_404_is_none(self):
        fake = _FakeTraceipt()
        self.assertIsNone(PULL.fetch_envelope("https://x", "nope", _get=fake.get))

    def test_get_raising_is_none(self):
        def boom(url, timeout):
            raise ConnectionError("down")
        self.assertIsNone(PULL.fetch_envelope("https://x", "r1", _get=boom))

    def test_id_is_url_quoted(self):
        fake = _FakeTraceipt()
        PULL.fetch_envelope("https://x", "a/b?c", _get=fake.get)
        self.assertIn("/receipts/a%2Fb%3Fc", fake.hits[0])  # path-injection safe


if __name__ == "__main__":
    unittest.main()
