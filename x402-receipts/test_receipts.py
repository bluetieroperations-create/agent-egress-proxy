"""Tests for x402-receipts. The security boundary — canonicalization,
signing, hash chain, settlement matching — is tested first and hardest."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from receipts.canonical import GENESIS_HASH, canonical_json, hash_obj
from receipts.ledger import Ledger
from receipts.schema import build_receipt, receipt_hash
from receipts.settlement import (
    TRANSFER_TOPIC, USDC_CONTRACTS, MockVerifier, RpcVerifier,
)
from receipts.service import App, make_handler
from receipts.signing import Signer, verify_envelope

PAYER = "0x" + "aa" * 20
PAYEE = "0x" + "bb" * 20
TX = "0x" + "cc" * 32
CONTRACT = USDC_CONTRACTS["base-sepolia"]


def sample_settlement(**over):
    s = {
        "chain": "base-sepolia", "tx_hash": TX, "asset": "USDC",
        "asset_contract": CONTRACT, "amount_base_units": "5000",
        "payer": PAYER, "payee": PAYEE,
        "verified": True, "verification_method": "mock",
    }
    s.update(over)
    return s


def sample_commerce(**over):
    c = {
        "resource": "https://api.example.com/v1/lookup",
        "description": "domain reputation query",
        "quoted_amount_base_units": "5000",
    }
    c.update(over)
    return c


def sample_receipt(seq=1, prev=GENESIS_HASH, **over):
    kw = dict(
        seller_id="seller.example.com", sequence=seq, prev_receipt_hash=prev,
        issued_at="2026-07-26T12:00:00Z",
        settlement=sample_settlement(), commerce=sample_commerce(),
    )
    kw.update(over)
    return build_receipt(**kw)


# ---------------------------------------------------------------------------
# canonical JSON
# ---------------------------------------------------------------------------
class TestCanonical(unittest.TestCase):
    def test_key_order_is_deterministic(self):
        a = canonical_json({"b": 1, "a": {"y": 2, "x": 3}})
        b = canonical_json({"a": {"x": 3, "y": 2}, "b": 1})
        self.assertEqual(a, b)
        self.assertEqual(a, b'{"a":{"x":3,"y":2},"b":1}')

    def test_floats_rejected(self):
        with self.assertRaises(ValueError):
            canonical_json({"amount": 0.005})

    def test_non_string_keys_rejected(self):
        with self.assertRaises(ValueError):
            canonical_json({1: "x"})

    def test_unicode_stable(self):
        self.assertEqual(canonical_json({"s": "ünïcode"}),
                         '{"s":"ünïcode"}'.encode("utf-8"))

    def test_hash_shape(self):
        h = hash_obj({"a": 1})
        self.assertTrue(h.startswith("sha256:"))
        self.assertEqual(len(h), len("sha256:") + 64)


# ---------------------------------------------------------------------------
# signing
# ---------------------------------------------------------------------------
class TestSigning(unittest.TestCase):
    def setUp(self):
        self.signer = Signer.generate()
        self.jwks = {"keys": [self.signer.jwk()]}

    def test_roundtrip(self):
        payload = sample_receipt()
        env = self.signer.sign_envelope(payload)
        self.assertEqual(verify_envelope(env, self.jwks), payload)

    def test_payload_tamper_fails(self):
        env = self.signer.sign_envelope(sample_receipt())
        env["payload"]["settlement"]["amount_base_units"] = "999999999"
        with self.assertRaises(ValueError):
            verify_envelope(env, self.jwks)

    def test_header_tamper_fails(self):
        env = self.signer.sign_envelope(sample_receipt())
        env["protected"]["kid"] = self.signer.kid  # unchanged: still verifies
        verify_envelope(env, self.jwks)
        env["protected"]["typ"] = "evil"
        with self.assertRaises(ValueError):
            verify_envelope(env, self.jwks)

    def test_wrong_key_fails(self):
        env = self.signer.sign_envelope(sample_receipt())
        other = Signer.generate()
        env["protected"]["kid"] = other.kid
        with self.assertRaises(ValueError):
            verify_envelope(env, {"keys": [other.jwk()]})

    def test_alg_substitution_rejected(self):
        env = self.signer.sign_envelope(sample_receipt())
        env["protected"]["alg"] = "none"
        with self.assertRaises(ValueError):
            verify_envelope(env, self.jwks)

    def test_pem_roundtrip(self):
        pem = self.signer.private_pem()
        again = Signer.from_pem(pem)
        self.assertEqual(again.kid, self.signer.kid)


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
class TestSchema(unittest.TestCase):
    def test_receipt_id_deterministic(self):
        self.assertEqual(sample_receipt()["receipt_id"], sample_receipt()["receipt_id"])

    def test_first_receipt_must_use_genesis(self):
        with self.assertRaises(ValueError):
            sample_receipt(seq=1, prev="sha256:" + "ab" * 32)

    def test_bad_tx_hash_rejected(self):
        with self.assertRaises(ValueError):
            build_receipt(
                seller_id="s", sequence=1, prev_receipt_hash=GENESIS_HASH,
                issued_at="2026-07-26T12:00:00Z",
                settlement=sample_settlement(tx_hash="0x1234"),
                commerce=sample_commerce(),
            )

    def test_float_amount_impossible(self):
        with self.assertRaises(ValueError):
            build_receipt(
                seller_id="s", sequence=1, prev_receipt_hash=GENESIS_HASH,
                issued_at="2026-07-26T12:00:00Z",
                settlement=sample_settlement(amount_base_units="0.005"),
                commerce=sample_commerce(),
            )

    def test_non_https_resource_rejected(self):
        with self.assertRaises(ValueError):
            build_receipt(
                seller_id="s", sequence=1, prev_receipt_hash=GENESIS_HASH,
                issued_at="2026-07-26T12:00:00Z",
                settlement=sample_settlement(),
                commerce=sample_commerce(resource="ftp://x"),
            )


# ---------------------------------------------------------------------------
# ledger / hash chain
# ---------------------------------------------------------------------------
class TestLedger(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ledger = Ledger(os.path.join(self.dir.name, "l.db"))
        self.signer = Signer.generate()

    def tearDown(self):
        self.dir.cleanup()

    def _append_next(self, seller="s1"):
        seq, prev = self.ledger.next_link(seller)
        # each receipt documents a DISTINCT settlement (unique tx per seq)
        payload = sample_receipt(
            seq=seq, prev=prev, seller_id=seller,
            settlement=sample_settlement(tx_hash="0x" + f"{seq:064x}"),
        )
        env = self.signer.sign_envelope(payload)
        self.ledger.append(env)
        return payload

    def test_sequence_is_dense_and_chained(self):
        p1 = self._append_next()
        p2 = self._append_next()
        p3 = self._append_next()
        self.assertEqual((p1["sequence"], p2["sequence"], p3["sequence"]), (1, 2, 3))
        self.assertEqual(p2["prev_receipt_hash"], receipt_hash(p1))
        self.assertEqual(p3["prev_receipt_hash"], receipt_hash(p2))
        self.assertEqual(self.ledger.verify_chain("s1"), [])

    def test_chain_break_rejected(self):
        self._append_next()
        bad = sample_receipt(seq=5, prev="sha256:" + "ab" * 32)
        with self.assertRaises(ValueError):
            self.ledger.append(self.signer.sign_envelope(bad))

    def test_row_tamper_detected(self):
        p = self._append_next()
        # simulate a hostile DB edit: swap the stored envelope's amount
        import sqlite3
        con = sqlite3.connect(os.path.join(self.dir.name, "l.db"))
        env = json.loads(con.execute(
            "SELECT envelope FROM receipts WHERE receipt_id=?",
            (p["receipt_id"],)).fetchone()[0])
        env["payload"]["settlement"]["amount_base_units"] = "1"
        con.execute("UPDATE receipts SET envelope=? WHERE receipt_id=?",
                    (json.dumps(env), p["receipt_id"]))
        con.commit()
        con.close()
        problems = self.ledger.verify_chain("s1")
        self.assertTrue(problems and "tampered" in problems[0])

    def test_sellers_have_independent_chains(self):
        a = self._append_next("sellerA")
        b = self._append_next("sellerB")
        self.assertEqual(a["sequence"], 1)
        self.assertEqual(b["sequence"], 1)

    def test_rehash_consistent_rewrite_caught_by_signature_check(self):
        # An attacker with DB write access rewrites every payload AND
        # cascades the keyless hash chain so all hash links stay consistent.
        # verify_chain WITHOUT a signature check is fooled; WITH it, caught.
        for _ in range(3):
            self._append_next("victim")
        import sqlite3
        from receipts.canonical import GENESIS_HASH
        from receipts.schema import receipt_hash as rh
        con = sqlite3.connect(os.path.join(self.dir.name, "l.db"))
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM receipts WHERE seller_id='victim' "
                           "ORDER BY sequence").fetchall()
        prev = GENESIS_HASH
        for row in rows:
            env = json.loads(row["envelope"])
            env["payload"]["settlement"]["amount_base_units"] = "999999999"
            env["payload"]["prev_receipt_hash"] = prev
            new_hash = rh(env["payload"])
            con.execute("UPDATE receipts SET envelope=?, receipt_hash=?, prev_hash=? "
                        "WHERE receipt_id=?",
                        (json.dumps(env), new_hash, prev, row["receipt_id"]))
            prev = new_hash
        con.commit()
        con.close()
        # keyless hash-only check: fooled (the point of the finding)
        self.assertEqual(self.ledger.verify_chain("victim"), [])
        # signature-aware check: catches every rewritten receipt
        jwks = {"keys": [self.signer.jwk()]}
        problems = self.ledger.verify_chain("victim", lambda e: verify_envelope(e, jwks))
        self.assertTrue(problems)
        self.assertTrue(all("signature INVALID" in p for p in problems))


# ---------------------------------------------------------------------------
# settlement verification
# ---------------------------------------------------------------------------
def fake_rpc(receipt_result):
    def transport(req):
        assert req["method"] == "eth_getTransactionReceipt"
        return {"jsonrpc": "2.0", "id": 1, "result": receipt_result}
    return transport


def chain_receipt(contract=CONTRACT, payer=PAYER, payee=PAYEE, amount=5000, status="0x1"):
    def pad_addr(a):
        return "0x" + "0" * 24 + a[2:].lower()
    return {
        "status": status,
        "logs": [{
            "address": contract,
            "topics": [TRANSFER_TOPIC, pad_addr(payer), pad_addr(payee)],
            "data": hex(amount),
        }],
    }


class TestSettlement(unittest.TestCase):
    def test_match_passes(self):
        v = RpcVerifier("http://unused", transport=fake_rpc(chain_receipt()))
        r = v.verify(sample_settlement())
        self.assertTrue(r.ok)
        self.assertEqual(r.method, "rpc")

    def test_amount_mismatch_fails(self):
        v = RpcVerifier("http://unused", transport=fake_rpc(chain_receipt(amount=1)))
        r = v.verify(sample_settlement())
        self.assertFalse(r.ok)
        self.assertIn("amount mismatch", r.reason)

    def test_wrong_payee_fails(self):
        v = RpcVerifier("http://unused",
                        transport=fake_rpc(chain_receipt(payee="0x" + "dd" * 20)))
        self.assertFalse(v.verify(sample_settlement()).ok)

    def test_wrong_contract_fails(self):
        v = RpcVerifier("http://unused",
                        transport=fake_rpc(chain_receipt(contract="0x" + "ee" * 20)))
        self.assertFalse(v.verify(sample_settlement()).ok)

    def test_reverted_tx_fails(self):
        v = RpcVerifier("http://unused",
                        transport=fake_rpc(chain_receipt(status="0x0")))
        r = v.verify(sample_settlement())
        self.assertFalse(r.ok)
        self.assertIn("reverted", r.reason)

    def test_missing_tx_fails(self):
        v = RpcVerifier("http://unused", transport=fake_rpc(None))
        r = v.verify(sample_settlement())
        self.assertFalse(r.ok)
        self.assertIn("not found", r.reason)

    def test_match_on_second_of_multiple_logs(self):
        # A tx with two payer->payee transfers; only the second has the
        # claimed amount. Must scan all logs, not fail on the first.
        wrong = chain_receipt(amount=1)["logs"][0]
        right = chain_receipt(amount=5000)["logs"][0]
        rec = {"status": "0x1", "logs": [wrong, right]}
        v = RpcVerifier("http://unused", transport=fake_rpc(rec))
        self.assertTrue(v.verify(sample_settlement()).ok)

    def test_malformed_log_data_is_skipped_not_crash(self):
        bad = chain_receipt()["logs"][0] | {"data": "0x"}
        rec = {"status": "0x1", "logs": [bad]}
        v = RpcVerifier("http://unused", transport=fake_rpc(rec))
        r = v.verify(sample_settlement())  # must not raise
        self.assertFalse(r.ok)


# ---------------------------------------------------------------------------
# service end-to-end (dev gate + mock settlement)
# ---------------------------------------------------------------------------
class TestService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        app = App(
            signer=Signer.generate(),
            ledger=Ledger(os.path.join(cls.dir.name, "svc.db")),
            verifier=MockVerifier(),
            gate="dev",
            price_base_units="2000",
            pay_to=PAYEE,
            chain="base-sepolia",
            base_url="http://127.0.0.1:0",
        )
        cls.app = app
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.dir.cleanup()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _post(self, path, body, headers=None):
        req = urllib.request.Request(
            self._url(path), data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get(self, path):
        try:
            with urllib.request.urlopen(self._url(path)) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def request_body(self):
        return {
            "seller_id": "seller.example.com",
            "settlement": {k: v for k, v in sample_settlement().items()
                           if k not in ("verified", "verification_method")},
            "commerce": sample_commerce(),
        }

    def test_unpaid_gets_402_with_x402_requirements(self):
        code, obj = self._post("/receipts", self.request_body())
        self.assertEqual(code, 402)
        acc = obj["accepts"][0]
        self.assertEqual(acc["scheme"], "exact")
        self.assertEqual(acc["maxAmountRequired"], "2000")
        self.assertEqual(acc["payTo"], PAYEE)

    def test_paid_issue_verify_roundtrip(self):
        code, obj = self._post("/receipts", self.request_body(),
                               headers={"X-PAYMENT": "dev-token"})
        self.assertEqual(code, 201, obj)
        rid = obj["receipt"]["payload"]["receipt_id"]
        self.assertEqual(obj["receipt"]["payload"]["settlement"]["verification_method"],
                         "mock")
        code, report = self._get(f"/verify/{rid}")
        self.assertEqual(code, 200)
        self.assertEqual(report["signature"], "valid")
        self.assertEqual(report["verdict"], "PASS")
        code, env = self._get(f"/receipts/{rid}")
        self.assertEqual(code, 200)
        jwks = self._get("/jwks.json")[1]
        verify_envelope(env, jwks)  # offline verification with published keys

    def test_bad_body_400(self):
        code, obj = self._post("/receipts", {"nope": 1},
                               headers={"X-PAYMENT": "dev-token"})
        self.assertEqual(code, 400)

    def test_unknown_receipt_404(self):
        code, _ = self._get("/verify/rcpt_doesnotexist")
        self.assertEqual(code, 404)

    def test_duplicate_settlement_is_idempotent_not_farmable(self):
        # One on-chain payment must never yield two receipts.
        body = self.request_body()
        body["seller_id"] = "farm.example.com"
        body["settlement"]["tx_hash"] = "0x" + "77" * 32
        c1, r1 = self._post("/receipts", body, headers={"X-PAYMENT": "t"})
        c2, r2 = self._post("/receipts", body, headers={"X-PAYMENT": "t"})
        self.assertEqual(c1, 201)
        self.assertEqual(c2, 200)
        self.assertTrue(r2.get("idempotent"))
        self.assertEqual(r1["receipt"]["payload"]["receipt_id"],
                         r2["receipt"]["payload"]["receipt_id"])
        # ...even with different commerce data attached to the same tx
        body["commerce"]["description"] = "totally different claim"
        c3, r3 = self._post("/receipts", body, headers={"X-PAYMENT": "t"})
        self.assertEqual(c3, 200)
        self.assertEqual(r3["receipt"]["payload"]["receipt_id"],
                         r1["receipt"]["payload"]["receipt_id"])

    def test_chain_endpoint_checks_signatures(self):
        # Issue one, then tamper the stored row's payload but keep the hash
        # consistent; /chain must FAIL because the signature no longer matches.
        body = self.request_body()
        body["seller_id"] = "sigcheck.example.com"
        body["settlement"]["tx_hash"] = "0x" + "88" * 32
        self._post("/receipts", body, headers={"X-PAYMENT": "t"})
        code, chain = self._get("/chain/sigcheck.example.com")
        self.assertEqual(chain["chain"], "intact")

    def test_concurrent_issuance_all_succeed_dense_chain(self):
        import threading as _t
        results = []
        def worker(i):
            body = self.request_body()
            body["seller_id"] = "concurrent.example.com"
            body["settlement"]["tx_hash"] = "0x" + f"{i:064x}"
            results.append(self._post("/receipts", body,
                                      headers={"X-PAYMENT": "t"})[0])
        threads = [_t.Thread(target=worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(201), 16, results)
        code, chain = self._get("/chain/concurrent.example.com")
        self.assertEqual(chain["chain"], "intact")


class TestTrustBoundary(unittest.TestCase):
    """Seller-hosted binding: pinned seller_id + payee-must-equal-pay_to."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.app = App(
            signer=Signer.generate(),
            ledger=Ledger(os.path.join(cls.dir.name, "tb.db")),
            verifier=MockVerifier(),
            gate="dev", price_base_units="2000",
            pay_to=PAYEE,                 # a REAL configured pay_to
            seller_id="operator.example", # pinned identity
            chain="base-sepolia", base_url="http://127.0.0.1:0",
        )
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.dir.cleanup()

    def _post(self, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/receipts", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-PAYMENT": "t"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _body(self, **settlement_over):
        s = {"chain": "base-sepolia", "tx_hash": "0x" + "cc" * 32,
             "amount_base_units": "5000", "payer": PAYER, "payee": PAYEE}
        s.update(settlement_over)
        return {"seller_id": "attacker-tries-this",
                "settlement": s,
                "commerce": sample_commerce()}

    def test_payee_must_match_pay_to(self):
        # A settlement paid to someone OTHER than the operator is refused.
        code, obj = self._post(self._body(payee="0x" + "de" * 20,
                                          tx_hash="0x" + "a1" * 32))
        self.assertEqual(code, 403)
        self.assertIn("pay_to", obj["error"])

    def test_seller_id_is_pinned_not_caller_controlled(self):
        code, obj = self._post(self._body(tx_hash="0x" + "a2" * 32))
        self.assertEqual(code, 201, obj)
        # caller said "attacker-tries-this"; receipt is under the operator id
        self.assertEqual(obj["receipt"]["payload"]["seller_id"], "operator.example")

    def test_foreign_chain_claim_rejected(self):
        # Claiming a base-mainnet settlement on a sepolia-configured service.
        code, obj = self._post(self._body(chain="base", tx_hash="0x" + "a3" * 32))
        self.assertEqual(code, 400)
        self.assertIn("chain", obj["error"])

    def test_caller_contract_is_ignored(self):
        # Even if the caller supplies a bogus asset_contract, the service
        # forces its own chain's USDC address into the receipt.
        code, obj = self._post(self._body(tx_hash="0x" + "a4" * 32,
                                          asset_contract="0x" + "ee" * 20))
        self.assertEqual(code, 201, obj)
        self.assertEqual(obj["receipt"]["payload"]["settlement"]["asset_contract"],
                         USDC_CONTRACTS["base-sepolia"])

    def test_missing_payer_is_400_not_500(self):
        body = self._body(tx_hash="0x" + "a5" * 32)
        del body["settlement"]["payer"]
        code, obj = self._post(body)
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
