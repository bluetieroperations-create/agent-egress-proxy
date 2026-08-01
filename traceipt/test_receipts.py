"""Tests for Traceipt. The security boundary — canonicalization,
signing, hash chain, settlement matching — is tested first and hardest."""
import copy
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from traceipt.canonical import GENESIS_HASH, canonical_json, hash_obj
from traceipt.completeness import verify_completeness
from traceipt.disclosure import verify_disclosure
from traceipt.ledger import Ledger
from traceipt.screening import (
    build_screening, verdict_digest, verify_anchored_before, verify_screening,
)
from traceipt.schema import build_receipt, receipt_hash
from traceipt.settlement import (
    TRANSFER_TOPIC, USDC_CONTRACTS, MockVerifier, RpcVerifier,
)
from traceipt.service import App, make_handler
from traceipt.signing import Signer, verify_envelope
from traceipt.x402_gate import (
    Facilitator, decode_payment_header, encode_payment_response,
    gate_verify, payer_from_payload,
)
import base64 as _b64

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

    def test_anchor_seals_batch_and_proofs_verify(self):
        ids = [self._append_next("s1")["receipt_id"] for _ in range(5)]
        anchor = self.ledger.create_anchor("2026-07-26T00:00:00Z")
        self.assertEqual(anchor["leaf_count"], 5)
        for rid in ids:
            pr = self.ledger.inclusion_for(rid)
            self.assertIsNotNone(pr)
            self.assertTrue(verify_inclusion(
                pr["leaf_index"], pr["tree_size"], pr["leaf_data"].encode(),
                [bytes.fromhex(h) for h in pr["audit_path"]],
                bytes.fromhex(pr["root"])))

    def test_anchor_is_incremental_and_empty_returns_none(self):
        self._append_next("s1")
        a1 = self.ledger.create_anchor("t1")
        self.assertEqual(a1["leaf_count"], 1)
        # nothing new to anchor
        self.assertIsNone(self.ledger.create_anchor("t2"))
        # new receipt -> next anchor picks up only it
        self._append_next("s1")
        a2 = self.ledger.create_anchor("t3")
        self.assertEqual(a2["leaf_count"], 1)
        self.assertNotEqual(a1["anchor_id"], a2["anchor_id"])

    def test_onchain_publisher_hook_records_tx(self):
        self._append_next("s1")
        anchor = self.ledger.create_anchor(
            "t1", publisher=lambda root_hex: ("base", "0x" + "ab" * 32))
        self.assertEqual(anchor["onchain_tx"], "0x" + "ab" * 32)
        self.assertEqual(anchor["onchain_network"], "base")

    def test_rehash_consistent_rewrite_caught_by_signature_check(self):
        # An attacker with DB write access rewrites every payload AND
        # cascades the keyless hash chain so all hash links stay consistent.
        # verify_chain WITHOUT a signature check is fooled; WITH it, caught.
        for _ in range(3):
            self._append_next("victim")
        import sqlite3
        from traceipt.canonical import GENESIS_HASH
        from traceipt.schema import receipt_hash as rh
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


def chain_receipt(contract=CONTRACT, payer=PAYER, payee=PAYEE, amount=5000,
                  status="0x1", block=None):
    def pad_addr(a):
        return "0x" + "0" * 24 + a[2:].lower()
    rec = {
        "status": status,
        "logs": [{
            "address": contract,
            "topics": [TRANSFER_TOPIC, pad_addr(payer), pad_addr(payee)],
            "data": hex(amount),
        }],
    }
    if block is not None:
        rec["blockNumber"] = hex(block)
    return rec


def fake_rpc_with_head(receipt_result, head_block):
    """Transport answering BOTH eth_getTransactionReceipt and eth_blockNumber,
    for the confirmation-depth guard."""
    def transport(req):
        if req["method"] == "eth_getTransactionReceipt":
            return {"result": receipt_result}
        if req["method"] == "eth_blockNumber":
            return {"result": hex(head_block)}
        raise AssertionError("unexpected method " + req["method"])
    return transport


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

    def test_confirmations_zero_makes_no_head_call(self):
        # Guard off: never calls eth_blockNumber (fake_rpc asserts the method),
        # so the default path costs no extra RPC round-trip.
        v = RpcVerifier("http://unused", transport=fake_rpc(chain_receipt()),
                        min_confirmations=0)
        self.assertTrue(v.verify(sample_settlement()).ok)

    def test_confirmations_insufficient_fails(self):
        rec = chain_receipt(block=100)
        # head 101 -> confirmations = 101-100+1 = 2; require 5 -> fail
        v = RpcVerifier("http://unused", transport=fake_rpc_with_head(rec, 101),
                        min_confirmations=5)
        r = v.verify(sample_settlement())
        self.assertFalse(r.ok)
        self.assertIn("insufficient confirmations", r.reason)

    def test_confirmations_sufficient_passes(self):
        rec = chain_receipt(block=100)
        # head 110 -> confirmations = 11; require 5 -> pass
        v = RpcVerifier("http://unused", transport=fake_rpc_with_head(rec, 110),
                        min_confirmations=5)
        self.assertTrue(v.verify(sample_settlement()).ok)

    def test_confirmations_unreadable_head_fails_safe(self):
        rec = chain_receipt(block=100)

        def transport(req):
            if req["method"] == "eth_getTransactionReceipt":
                return {"result": rec}
            raise RuntimeError("rpc head unreadable")

        v = RpcVerifier("http://unused", transport=transport, min_confirmations=3)
        r = v.verify(sample_settlement())
        self.assertFalse(r.ok)   # never attest on an unverifiable depth
        self.assertIn("confirmations", r.reason)

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
        self.assertEqual(obj["x402Version"], 2)          # x402 v2 wire format
        acc = obj["accepts"][0]
        self.assertEqual(acc["scheme"], "exact")
        self.assertEqual(acc["amount"], "2000")          # v2: amount, not maxAmountRequired
        self.assertNotIn("maxAmountRequired", acc)
        self.assertEqual(acc["network"], "eip155:84532")  # v2: CAIP-2 network
        self.assertEqual(acc["extra"], {"name": "USDC", "version": "2"})  # EIP-712 domain
        self.assertEqual(acc["payTo"], PAYEE)
        # v2: resource/description move to a top-level ResourceInfo.
        self.assertEqual(obj["resource"]["mimeType"], "application/json")

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

    def test_invoice_pdf_endpoint(self):
        body = self.request_body()
        body["settlement"]["tx_hash"] = "0x" + "1a" * 32  # unique to this test
        code, obj = self._post("/receipts", body, headers={"X-PAYMENT": "dev-token"})
        self.assertIn(code, (200, 201), obj)
        rid = obj["receipt"]["payload"]["receipt_id"]
        with urllib.request.urlopen(self._url(f"/receipts/{rid}/invoice.pdf")) as r:
            self.assertEqual(r.headers["Content-Type"], "application/pdf")
            data = r.read()
        assert_valid_pdf(self, data)
        self.assertIn(rid.encode(), data)

    def test_invoice_pdf_unknown_404(self):
        try:
            urllib.request.urlopen(self._url("/receipts/rcpt_nope/invoice.pdf"))
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_anchor_flow_and_verify_reports_inclusion(self):
        body = self.request_body()
        body["seller_id"] = "anchor.example.com"
        body["settlement"]["tx_hash"] = "0x" + "5a" * 32
        code, obj = self._post("/receipts", body, headers={"X-PAYMENT": "t"})
        self.assertEqual(code, 201, obj)
        rid = obj["receipt"]["payload"]["receipt_id"]

        # before anchoring, verify reports "not yet anchored"
        _, report = self._get(f"/verify/{rid}")
        self.assertEqual(report["anchor"], "not yet anchored")

        # seal a batch
        code, aobj = self._post("/anchor", {}, headers={})
        self.assertEqual(code, 201, aobj)
        self.assertGreaterEqual(aobj["anchored"], 1)

        # now verify reports a verified inclusion proof
        _, report = self._get(f"/verify/{rid}")
        self.assertEqual(report["anchor"]["inclusion"], "verified")
        self.assertEqual(report["verdict"], "PASS")

        # and the standalone proof endpoint returns a checkable proof
        _, proof = self._get(f"/receipts/{rid}/proof")
        from traceipt.merkle import verify_inclusion as _vi
        self.assertTrue(_vi(proof["leaf_index"], proof["tree_size"],
                            proof["leaf_data"].encode(),
                            [bytes.fromhex(h) for h in proof["audit_path"]],
                            bytes.fromhex(proof["root"])))

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


from traceipt.invoice import render_invoice, usdc, vat_breakdown
from traceipt.merkle import (
    inclusion_proof, merkle_root, verify_inclusion,
)


class TestMerkle(unittest.TestCase):
    def test_root_stable_and_proofs_verify_across_sizes(self):
        for n in (1, 2, 3, 5, 8, 13, 100):
            leaves = [f"r{i}".encode() for i in range(n)]
            root = merkle_root(leaves)
            for m in range(n):
                proof = inclusion_proof(leaves, m)
                self.assertTrue(verify_inclusion(m, n, leaves[m], proof, root),
                                f"n={n} m={m}")

    def test_tampered_leaf_fails(self):
        leaves = [f"r{i}".encode() for i in range(10)]
        root = merkle_root(leaves)
        proof = inclusion_proof(leaves, 3)
        self.assertFalse(verify_inclusion(3, 10, b"forged", proof, root))

    def test_tampered_proof_fails(self):
        leaves = [f"r{i}".encode() for i in range(10)]
        root = merkle_root(leaves)
        proof = inclusion_proof(leaves, 3)
        proof[0] = bytes(b ^ 1 for b in proof[0])
        self.assertFalse(verify_inclusion(3, 10, leaves[3], proof, root))

    def test_domain_separation_leaf_vs_node(self):
        # a single-leaf root must not equal a two-leaf root of the same bytes
        self.assertNotEqual(merkle_root([b"a" + b"b"]), merkle_root([b"a", b"b"]))


def assert_valid_pdf(testcase, data: bytes):
    """Structural PDF check without a PDF library: header, trailer, and every
    xref offset must land on an 'N 0 obj' header."""
    testcase.assertTrue(data.startswith(b"%PDF-1."), "missing PDF header")
    testcase.assertIn(b"%%EOF", data)
    idx = data.rfind(b"startxref")
    testcase.assertNotEqual(idx, -1, "no startxref")
    xref_off = int(data[idx + len("startxref"):].split()[0])
    testcase.assertEqual(data[xref_off:xref_off + 4], b"xref", "startxref mispoints")
    # parse the xref table entries and confirm each in-use offset points at 'obj'
    body = data[xref_off:].split(b"\n")
    # body[0]='xref', body[1]='0 N', then N entries of 20 bytes each
    count = int(body[1].split()[1])
    entries = body[2:2 + count]
    used = 0
    for e in entries:
        parts = e.split()
        if len(parts) >= 3 and parts[2] == b"n":
            off = int(parts[0])
            seg = data[off:off + 24]
            testcase.assertRegex(seg, rb"^\d+ 0 obj", f"xref offset {off} not an object")
            used += 1
    testcase.assertGreaterEqual(used, 5)


class TestQR(unittest.TestCase):
    """The QR encoder is validated against the `segno` reference library when
    it is importable (test-only). Where segno is absent, structural checks
    still run. segno is never a runtime dependency."""

    def _segno(self):
        try:
            import segno  # noqa: F401
            return segno
        except ImportError:
            return None

    def test_matches_segno_across_versions_and_masks(self):
        from traceipt import qr
        segno = self._segno()
        if segno is None:
            self.skipTest("segno not installed (oracle unavailable)")
        cases = ["hello", "x402",
                 "https://receipts.example.com/verify/rcpt_9be51ff385fc77069cf1"]
        # add lengths that push into each supported version
        for n in (10, 30, 55, 80, 100):
            cases.append("https://x.io/v/" + "a" * n)
        checked = 0
        for data in cases:
            try:
                v = qr._choose_version(len(data.encode()))
            except ValueError:
                continue  # beyond v6 capacity — not in scope
            cw = qr._interleave(qr._encode_data_codewords(data.encode(), v), v)
            bits = []
            for c in cw:
                bits.extend(int(b) for b in format(c, "08b"))
            for mask in range(8):
                base = qr._new_matrix(17 + 4 * v)
                qr._place_function_patterns(base, v)
                res = qr._reserved(base, v)
                qr._place_data(base, res, bits)
                m = qr._apply_mask(base, res, mask)
                qr._place_format(m, mask)
                mine = [[0 if x is None else x for x in row] for row in m]
                ref = [list(r) for r in segno.make(
                    data, error="m", version=v, mask=mask, boost_error=False).matrix]
                self.assertEqual(mine, ref, f"data={data[:20]!r} v={v} mask={mask}")
                checked += 1
        self.assertGreater(checked, 0)

    def test_encode_output_is_square_binary(self):
        from traceipt import qr
        m = qr.encode("https://r.example/verify/rcpt_x")
        self.assertTrue(all(len(row) == len(m) for row in m))
        self.assertTrue(all(v in (0, 1) for row in m for v in row))

    def test_too_long_raises(self):
        from traceipt import qr
        with self.assertRaises(ValueError):
            qr.encode("x" * 200)


class TestInvoicePDF(unittest.TestCase):
    def test_usdc_formatting_no_float(self):
        self.assertEqual(usdc("5000"), "0.005000")
        self.assertEqual(usdc("1"), "0.000001")
        self.assertEqual(usdc("1000000"), "1.000000")

    def test_vat_breakdown_inclusive(self):
        net, vat, gross = vat_breakdown("1190000", "19")  # 1.19 USDC incl 19%
        self.assertEqual(gross, "1.190000")
        self.assertEqual(net, "1.000000")
        self.assertEqual(vat, "0.190000")

    def test_vat_breakdown_bad_rate_none(self):
        self.assertIsNone(vat_breakdown("1000000", "not-a-number"))

    def test_render_is_valid_pdf(self):
        pdf = render_invoice(sample_receipt(), verify_url="https://r/verify/x",
                             issuer_kid="deadbeef")
        assert_valid_pdf(self, pdf)

    def test_pdf_contains_key_fields_as_text(self):
        p = sample_receipt(seq=7)
        pdf = render_invoice(p, verify_url="https://r/verify/x")
        # content stream text is present as literal PDF strings
        self.assertIn(p["receipt_id"].encode(), pdf)
        self.assertIn(p["seller_id"].encode(), pdf)
        self.assertIn(p["settlement"]["tx_hash"].encode(), pdf)
        self.assertIn(b"0.005000 USDC", pdf)  # 5000 base units formatted

    def test_render_with_seller_entity_and_vat(self):
        p = sample_receipt(commerce=sample_commerce(seller_entity={
            "name": "Example GmbH", "vat_id": "DE123456789",
            "country": "DE", "vat_rate": "19"}))
        pdf = render_invoice(p)
        assert_valid_pdf(self, pdf)
        self.assertIn(b"Example GmbH", pdf)
        self.assertIn(b"DE123456789", pdf)

    def test_parens_in_description_are_escaped(self):
        # unescaped ( ) would corrupt the PDF string syntax
        p = sample_receipt(commerce=sample_commerce(
            description="query (batch) for prices (v2)"))
        pdf = render_invoice(p)
        assert_valid_pdf(self, pdf)
        self.assertIn(rb"query \(batch\) for prices \(v2\)", pdf)


def make_xpayment(payer, value="2000", network="base-sepolia"):
    """A well-formed X-PAYMENT header (base64 JSON) for the exact scheme."""
    payload = {
        "x402Version": 1, "scheme": "exact", "network": network,
        "payload": {"signature": "0x" + "11" * 65, "authorization": {
            "from": payer, "to": PAYEE, "value": value,
            "validAfter": "0", "validBefore": "9999999999",
            "nonce": "0x" + "22" * 32}},
    }
    return _b64.b64encode(json.dumps(payload).encode()).decode()


def make_xpayment_v2(payer, value="2000", network="eip155:84532"):
    """The exact x402 v2 X-PAYMENT envelope Black_Wall's client (clients/
    x402_pay.py::build_payment) produces: x402Version 2, the chosen
    requirements under `accepted`, EIP-3009 authorization under
    payload.authorization (no top-level scheme/network)."""
    payload = {
        "x402Version": 2,
        "accepted": {"scheme": "exact", "network": network, "amount": value,
                     "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
                     "payTo": PAYEE},
        "payload": {"signature": "0x" + "11" * 65, "authorization": {
            "from": payer, "to": PAYEE, "value": value,
            "validAfter": "0", "validBefore": "9999999999",
            "nonce": "0x" + "22" * 32}},
    }
    return _b64.b64encode(json.dumps(payload).encode()).decode()


class FakeFacilitator:
    """Stand-in for a real x402 facilitator. verify() approves iff asked;
    records settle() calls so tests can assert money moved (or didn't)."""

    def __init__(self, valid=True, invalid_reason="insufficient funds",
                 settle_ok=True):
        self.valid = valid
        self.invalid_reason = invalid_reason
        self.settle_ok = settle_ok
        self.verify_calls = 0
        self.settle_calls = 0

    def verify(self, payment, requirements):
        self.verify_calls += 1
        if not self.valid:
            return {"isValid": False, "invalidReason": self.invalid_reason}
        return {"isValid": True, "payer": payer_from_payload(payment)}

    def settle(self, payment, requirements):
        self.settle_calls += 1
        if not self.settle_ok:
            return {"success": False, "errorReason": "settle reverted"}
        return {"success": True, "transaction": "0x" + "fe" * 32,
                "network": requirements["network"],
                "payer": payer_from_payload(payment)}


class TestX402GateUnit(unittest.TestCase):
    def test_decode_rejects_garbage(self):
        for bad in ["", "!!!notbase64!!!",
                    _b64.b64encode(b"not json").decode(),
                    _b64.b64encode(b'{"x402Version":2}').decode(),
                    _b64.b64encode(b'{"x402Version":1}').decode()]:
            with self.assertRaises(ValueError):
                decode_payment_header(bad)

    def test_decode_and_payer_extraction(self):
        h = make_xpayment("0x" + "Ab" * 20)
        payload = decode_payment_header(h)
        self.assertEqual(payload["scheme"], "exact")
        self.assertEqual(payer_from_payload(payload), "0x" + "ab" * 20)

    def test_decode_accepts_v2_envelope(self):
        # The exact envelope Black_Wall's client builds must decode, and the
        # payer must be recoverable from payload.authorization.from.
        h = make_xpayment_v2("0x" + "Ab" * 20)
        payload = decode_payment_header(h)
        self.assertEqual(payload["x402Version"], 2)
        self.assertEqual(payload["accepted"]["scheme"], "exact")
        self.assertEqual(payer_from_payload(payload), "0x" + "ab" * 20)

    def test_decode_rejects_v2_without_accepted(self):
        bad = _b64.b64encode(json.dumps({
            "x402Version": 2, "payload": {"authorization": {"from": PAYER}},
        }).encode()).decode()
        with self.assertRaises(ValueError):
            decode_payment_header(bad)

    def test_gate_verify_ok_with_v2_payment(self):
        fac = FakeFacilitator()
        d = gate_verify(fac, {"X-PAYMENT": make_xpayment_v2(PAYER)},
                        {"network": "eip155:84532"}, {"error": "x"})
        self.assertTrue(d.ok)
        self.assertEqual(d.payer, PAYER.lower())

    def test_facilitator_forwards_v2_envelope(self):
        captured = {}

        def fake_transport(path, body):
            captured["path"] = path
            captured["body"] = body
            return {"isValid": True}

        fac = Facilitator("https://f.example", transport=fake_transport)
        fac.verify({"x402Version": 2}, {"scheme": "exact"})
        self.assertEqual(captured["body"]["x402Version"], 2)  # v2 forwarded
        self.assertIn("paymentPayload", captured["body"])
        self.assertIn("paymentRequirements", captured["body"])

    def test_gate_verify_absent_payment_402(self):
        d = gate_verify(FakeFacilitator(), {}, {"network": "base-sepolia"},
                        {"error": "pay up"})
        self.assertFalse(d.ok)
        self.assertEqual(d.code, 402)

    def test_gate_verify_invalid_payment_402(self):
        fac = FakeFacilitator(valid=False)
        d = gate_verify(fac, {"X-PAYMENT": make_xpayment(PAYER)},
                        {"network": "base-sepolia"}, {"error": "pay up"})
        self.assertFalse(d.ok)
        self.assertEqual(d.code, 402)
        self.assertIn("insufficient funds", d.body["error"])

    def test_gate_verify_ok_returns_payer_without_settling(self):
        fac = FakeFacilitator()
        d = gate_verify(fac, {"X-PAYMENT": make_xpayment(PAYER)},
                        {"network": "base-sepolia"}, {"error": "x"})
        self.assertTrue(d.ok)
        self.assertEqual(d.payer, PAYER.lower())
        self.assertEqual(fac.settle_calls, 0)  # verify does not move money

    def test_payment_response_roundtrips(self):
        s = {"success": True, "transaction": "0xabc"}
        decoded = json.loads(_b64.b64decode(encode_payment_response(s)))
        self.assertEqual(decoded, s)

    def test_gate_rejects_wrong_recipient_without_facilitator(self):
        # Defense-in-depth: a payment to the WRONG address is rejected locally,
        # never reaching the facilitator (which we don't trust to enforce it).
        fac = FakeFacilitator()
        req = {"scheme": "exact", "network": "eip155:84532",
               "amount": "2000", "payTo": PAYEE}
        # authorization pays a DIFFERENT address than payTo
        bad = make_xpayment(PAYER)  # to=PAYEE... build one paying elsewhere:
        payload = json.loads(_b64.b64decode(bad))
        payload["payload"]["authorization"]["to"] = "0x" + "99" * 20
        bad = _b64.b64encode(json.dumps(payload).encode()).decode()
        d = gate_verify(fac, {"X-PAYMENT": bad}, req, {"error": "x"})
        self.assertFalse(d.ok)
        self.assertIn("payTo", d.body["error"])
        self.assertEqual(fac.verify_calls, 0)   # never consulted the facilitator

    def test_gate_rejects_wrong_amount_without_facilitator(self):
        fac = FakeFacilitator()
        req = {"scheme": "exact", "network": "eip155:84532",
               "amount": "2000", "payTo": PAYEE}
        d = gate_verify(fac, {"X-PAYMENT": make_xpayment(PAYER, value="1")},
                        req, {"error": "x"})
        self.assertFalse(d.ok)
        self.assertIn("underpaid", d.body["error"])
        self.assertEqual(fac.verify_calls, 0)


class TestFacilitatorService(unittest.TestCase):
    """End-to-end through the HTTP service with a fake facilitator + payer
    binding — the security property the real gate exists to provide."""

    def _make(self, **over):
        d = tempfile.mkdtemp()
        fac = over.pop("facilitator", FakeFacilitator())
        cfg = dict(signer=Signer.generate(), ledger=Ledger(os.path.join(d, "f.db")),
                   verifier=MockVerifier(), gate="facilitator", price_base_units="2000",
                   pay_to=PAYEE, seller_id="operator.example", chain="base-sepolia",
                   base_url="http://x", facilitator=fac, bind_payer=True)
        cfg.update(over)
        app = App(**cfg)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return app, fac, port

    def _post(self, port, body, xpayment=None):
        headers = {"Content-Type": "application/json"}
        if xpayment is not None:
            headers["X-PAYMENT"] = xpayment
        req = urllib.request.Request(f"http://127.0.0.1:{port}/receipts",
                                     data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read()), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), dict(e.headers)

    def _body(self, payer, tx):
        return {"settlement": {"chain": "base-sepolia", "tx_hash": tx,
                "amount_base_units": "5000", "payer": payer, "payee": PAYEE},
                "commerce": sample_commerce()}

    def test_no_payment_gets_402(self):
        _, fac, port = self._make()
        code, obj, _ = self._post(port, self._body(PAYER, "0x" + "b1" * 32))
        self.assertEqual(code, 402)
        self.assertEqual(fac.settle_calls, 0)

    def test_paid_and_payer_matches_issues_and_settles(self):
        _, fac, port = self._make()
        code, obj, hdrs = self._post(port, self._body(PAYER, "0x" + "b2" * 32),
                                     xpayment=make_xpayment(PAYER))
        self.assertEqual(code, 201, obj)
        self.assertEqual(fac.settle_calls, 1)              # money moved
        self.assertIn("X-PAYMENT-RESPONSE", hdrs)          # settle echoed back

    def test_payer_binding_blocks_third_party_claim(self):
        # THE fraud the gate closes: someone pays from their OWN wallet but
        # tries to document a settlement whose payer is a DIFFERENT address.
        _, fac, port = self._make()
        attacker = "0x" + "99" * 20
        code, obj, _ = self._post(
            port, self._body(PAYER, "0x" + "b3" * 32),  # settlement.payer = victim
            xpayment=make_xpayment(attacker))            # but attacker pays
        self.assertEqual(code, 403, obj)
        self.assertIn("payer binding", obj["error"])
        self.assertEqual(fac.settle_calls, 0)  # no charge for a rejected claim

    def test_issue_failure_does_not_settle(self):
        # A malformed settlement must NOT charge the caller.
        _, fac, port = self._make()
        body = self._body(PAYER, "0xshort")  # invalid tx hash
        code, obj, _ = self._post(port, body, xpayment=make_xpayment(PAYER))
        self.assertEqual(code, 400)
        self.assertEqual(fac.settle_calls, 0)

    def test_settle_failure_issues_no_receipt(self):
        # "No settled fee => no receipt": if the fee fails to settle, the
        # caller gets an error and NOTHING is persisted -- so they can't
        # obtain a valid signed receipt without paying.
        app, fac, port = self._make(facilitator=FakeFacilitator(settle_ok=False))
        tx = "0x" + "b4" * 32
        code, obj, _ = self._post(port, self._body(PAYER, tx),
                                  xpayment=make_xpayment(PAYER))
        self.assertEqual(code, 402, obj)          # not 201 + warning
        self.assertNotIn("receipt", obj)
        self.assertEqual(fac.settle_calls, 1)     # settle was attempted
        # And no receipt persisted: a retry (settle now succeeds) issues a
        # FRESH receipt rather than returning a leaked one.
        self.assertIsNone(app.ledger.find_by_settlement("operator.example", tx))

    def test_settle_failure_then_retry_succeeds(self):
        # A transient settle failure must not burn the settlement: once the
        # facilitator recovers, the same request issues a receipt.
        fac = FakeFacilitator(settle_ok=False)
        app, _, port = self._make(facilitator=fac)
        tx = "0x" + "b5" * 32
        code, _, _ = self._post(port, self._body(PAYER, tx),
                                xpayment=make_xpayment(PAYER))
        self.assertEqual(code, 402)
        fac.settle_ok = True                      # facilitator recovers
        code, obj, hdrs = self._post(port, self._body(PAYER, tx),
                                     xpayment=make_xpayment(PAYER))
        self.assertEqual(code, 201, obj)
        self.assertIn("X-PAYMENT-RESPONSE", hdrs)
        self.assertNotIn("_payment_response", obj)  # header only, never in body


class TestV02Schema(unittest.TestCase):
    """v0.2 additions: evidence links, principal, authorization, credit notes."""

    def test_payment_receipts_carry_kind(self):
        self.assertEqual(sample_receipt()["kind"], "payment")
        self.assertEqual(sample_receipt()["spec"], "x402-receipt/v0.2")

    def test_v01_style_build_still_works(self):
        # a caller passing only the original fields gets a valid receipt
        p = build_receipt(
            seller_id="s", sequence=1, prev_receipt_hash=GENESIS_HASH,
            issued_at="2026-07-27T12:00:00Z",
            settlement=sample_settlement(), commerce=sample_commerce())
        self.assertEqual(p["kind"], "payment")

    def test_evidence_links_valid(self):
        ev = [{"type": "aar-receipt", "hash": "sha256:" + "ab" * 32,
               "ref": "https://example.com/aar/123"},
              {"type": "acta-receipt", "hash": "sha256:" + "cd" * 32}]
        p = sample_receipt(commerce=sample_commerce(evidence=ev))
        self.assertEqual(len(p["commerce"]["evidence"]), 2)

    def test_evidence_bad_hash_rejected(self):
        with self.assertRaises(ValueError):
            sample_receipt(commerce=sample_commerce(
                evidence=[{"type": "aar-receipt", "hash": "0xnothash"}]))

    def test_evidence_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            sample_receipt(commerce=sample_commerce(
                evidence=[{"type": "x", "hash": "sha256:" + "ab" * 32,
                           "payload": "smuggled"}]))

    def test_evidence_list_cap(self):
        ev = [{"type": "x", "hash": "sha256:" + "ab" * 32}] * 17
        with self.assertRaises(ValueError):
            sample_receipt(commerce=sample_commerce(evidence=ev))

    def test_principal_and_authorization_valid(self):
        p = sample_receipt(commerce=sample_commerce(
            principal={"id_hash": "sha256:" + "11" * 32, "type": "org"},
            authorization={"scopes": ["spend:usdc"], "grant_hash":
                           "sha256:" + "22" * 32, "delegation_depth": 1}))
        self.assertEqual(p["commerce"]["principal"]["type"], "org")

    def test_principal_needs_id_or_hash(self):
        with self.assertRaises(ValueError):
            sample_receipt(commerce=sample_commerce(principal={"type": "org"}))

    def test_credit_receipt_build(self):
        p = build_receipt(
            seller_id="s", sequence=2, prev_receipt_hash="sha256:" + "ab" * 32,
            issued_at="2026-07-27T12:00:00Z", settlement=sample_settlement(),
            kind="credit",
            credit={"credits_receipt_id": "rcpt_" + "a" * 20, "reason": "refund"})
        self.assertEqual(p["kind"], "credit")
        self.assertNotIn("commerce", p)

    def test_credit_requires_credit_block_and_no_commerce(self):
        with self.assertRaises(ValueError):
            build_receipt(seller_id="s", sequence=1,
                          prev_receipt_hash=GENESIS_HASH,
                          issued_at="2026-07-27T12:00:00Z",
                          settlement=sample_settlement(), kind="credit")
        with self.assertRaises(ValueError):
            build_receipt(seller_id="s", sequence=1,
                          prev_receipt_hash=GENESIS_HASH,
                          issued_at="2026-07-27T12:00:00Z",
                          settlement=sample_settlement(), kind="credit",
                          commerce=sample_commerce(),
                          credit={"credits_receipt_id": "rcpt_" + "a" * 20,
                                  "reason": "r"})


class TestCreditsService(unittest.TestCase):
    """Credit-note lifecycle through the HTTP service."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.app = App(signer=Signer.generate(),
                      ledger=Ledger(os.path.join(cls.dir.name, "c.db")),
                      verifier=MockVerifier(), gate="dev",
                      price_base_units="2000", pay_to=PAYEE,
                      seller_id="op.example", chain="base-sepolia",
                      base_url="http://x")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.dir.cleanup()

    def _post(self, path, body, headers=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _issue_payment(self, tx):
        body = {"settlement": {"chain": "base-sepolia", "tx_hash": tx,
                               "amount_base_units": "5000", "payer": PAYER,
                               "payee": PAYEE},
                "commerce": sample_commerce()}
        code, obj = self._post("/receipts", body, {"X-PAYMENT": "t"})
        assert code == 201, obj
        return obj["receipt"]["payload"]["receipt_id"]

    def _credit(self, rid, amount, tx, payer=PAYEE, payee=PAYER, reason="refund"):
        return self._post("/credits", {
            "credits_receipt_id": rid, "reason": reason,
            "settlement": {"chain": "base-sepolia", "tx_hash": tx,
                           "amount_base_units": amount,
                           "payer": payer, "payee": payee}})

    def test_full_credit_lifecycle(self):
        rid = self._issue_payment("0x" + "c1" * 32)
        # partial credit 2000
        code, obj = self._credit(rid, "2000", "0x" + "d1" * 32)
        self.assertEqual(code, 201, obj)
        credit_payload = obj["receipt"]["payload"]
        self.assertEqual(credit_payload["kind"], "credit")
        self.assertEqual(credit_payload["credit"]["credits_receipt_id"], rid)
        # original verify now reports credited amount
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/verify/{rid}") as r:
            rep = json.loads(r.read())
        self.assertEqual(rep["credited_base_units"], "2000")
        self.assertEqual(rep["verdict"], "PASS")
        # over-credit rejected (only 3000 left)
        code, obj = self._credit(rid, "4000", "0x" + "d2" * 32)
        self.assertEqual(code, 409)
        # exact remaining passes; then nothing left
        code, _ = self._credit(rid, "3000", "0x" + "d3" * 32)
        self.assertEqual(code, 201)
        code, _ = self._credit(rid, "1", "0x" + "d4" * 32)
        self.assertEqual(code, 409)
        # chain (payments + credits interleaved) verifies intact
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/chain/op.example") as r:
            self.assertEqual(json.loads(r.read())["chain"], "intact")

    def test_wrong_refund_direction_403(self):
        rid = self._issue_payment("0x" + "c2" * 32)
        code, obj = self._credit(rid, "1000", "0x" + "d5" * 32,
                                 payer=PAYER, payee=PAYEE)  # NOT reversed
        self.assertEqual(code, 403)
        self.assertIn("reverse", obj["error"].replace("back to", "reverse"))

    def test_unknown_receipt_404_and_crediting_a_credit_400(self):
        code, _ = self._credit("rcpt_" + "0" * 20, "1", "0x" + "d6" * 32)
        self.assertEqual(code, 404)
        rid = self._issue_payment("0x" + "c3" * 32)
        code, obj = self._credit(rid, "1000", "0x" + "d7" * 32)
        self.assertEqual(code, 201)
        credit_id = obj["receipt"]["payload"]["receipt_id"]
        code, obj = self._credit(credit_id, "1", "0x" + "d8" * 32)
        self.assertEqual(code, 400)

    def test_refund_tx_reuse_is_idempotent(self):
        rid = self._issue_payment("0x" + "c4" * 32)
        code1, obj1 = self._credit(rid, "500", "0x" + "d9" * 32)
        code2, obj2 = self._credit(rid, "500", "0x" + "d9" * 32)
        self.assertEqual(code1, 201)
        self.assertEqual(code2, 200)
        self.assertTrue(obj2.get("idempotent"))
        self.assertEqual(obj1["receipt"]["payload"]["receipt_id"],
                         obj2["receipt"]["payload"]["receipt_id"])

    def test_credit_note_invoice_pdf(self):
        rid = self._issue_payment("0x" + "c5" * 32)
        code, obj = self._credit(rid, "1000", "0x" + "da" * 32)
        cid = obj["receipt"]["payload"]["receipt_id"]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/receipts/{cid}/invoice.pdf") as r:
            data = r.read()
        assert_valid_pdf(self, data)
        self.assertIn(b"CREDIT NOTE", data)
        self.assertIn(b"Total credited", data)

    def test_admin_token_gates_credits(self):
        d = tempfile.mkdtemp()
        app = App(signer=Signer.generate(), ledger=Ledger(os.path.join(d, "g.db")),
                  verifier=MockVerifier(), gate="dev", price_base_units="2000",
                  pay_to=PAYEE, seller_id="op", chain="base-sepolia",
                  base_url="http://x", admin_token="sekrit")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/credits", data=b"{}",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            self.fail("expected 403")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_evidence_principal_flow_through_issue(self):
        body = {"settlement": {"chain": "base-sepolia", "tx_hash": "0x" + "c6" * 32,
                               "amount_base_units": "5000", "payer": PAYER,
                               "payee": PAYEE},
                "commerce": sample_commerce(
                    evidence=[{"type": "aar-receipt",
                               "hash": "sha256:" + "ef" * 32}],
                    principal={"id_hash": "sha256:" + "aa" * 32, "type": "org"},
                    authorization={"scopes": ["spend:usdc:testnet"]})}
        code, obj = self._post("/receipts", body, {"X-PAYMENT": "t"})
        self.assertEqual(code, 201, obj)
        c = obj["receipt"]["payload"]["commerce"]
        self.assertEqual(c["evidence"][0]["type"], "aar-receipt")
        self.assertEqual(c["principal"]["type"], "org")
        # invalid evidence rejected cleanly
        body["settlement"]["tx_hash"] = "0x" + "c7" * 32
        body["commerce"]["evidence"] = [{"type": "x", "hash": "junk"}]
        code, obj = self._post("/receipts", body, {"X-PAYMENT": "t"})
        self.assertEqual(code, 400)


class TestAttestLedger(unittest.TestCase):
    """Anchoring-as-a-service at the ledger layer: external digests share
    Merkle batches with receipts, and both sides' proofs verify."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ledger = Ledger(os.path.join(self.dir.name, "a.db"))
        self.signer = Signer.generate()

    def tearDown(self):
        self.dir.cleanup()

    def _append_receipt(self, seq_tx):
        seq, prev = self.ledger.next_link("s1")
        payload = sample_receipt(seq=seq, prev=prev, seller_id="s1",
                                 settlement=sample_settlement(
                                     tx_hash="0x" + f"{seq_tx:064x}"))
        self.ledger.append(self.signer.sign_envelope(payload))
        return payload["receipt_id"]

    def test_mixed_anchor_proofs_verify_for_both_kinds(self):
        rids = [self._append_receipt(i + 1) for i in range(2)]
        a1, _ = self.ledger.submit_attestation(
            digest="sha256:" + "11" * 32, type_="aar-chain-head",
            ref="https://x/aar", submitted_at="2026-07-27T00:00:00Z")
        a2, _ = self.ledger.submit_attestation(
            digest="sha256:" + "22" * 32, type_="acta-receipt",
            ref=None, submitted_at="2026-07-27T00:00:01Z")
        anchor = self.ledger.create_anchor("2026-07-27T00:01:00Z")
        self.assertEqual(anchor["leaf_count"], 4)
        self.assertEqual(anchor["receipts"], 2)
        self.assertEqual(anchor["attestations"], 2)
        root = bytes.fromhex(anchor["root"])
        # receipt proofs verify against the mixed tree
        for rid in rids:
            pr = self.ledger.inclusion_for(rid)
            self.assertTrue(verify_inclusion(
                pr["leaf_index"], pr["tree_size"], pr["leaf_data"].encode(),
                [bytes.fromhex(h) for h in pr["audit_path"]], root))
        # attestation proofs verify against the same root
        seen_positions = set()
        for att in (a1, a2):
            pr = self.ledger.attestation_inclusion(att["attestation_id"])
            self.assertTrue(verify_inclusion(
                pr["leaf_index"], pr["tree_size"], pr["leaf_data"].encode(),
                [bytes.fromhex(h) for h in pr["audit_path"]], root))
            seen_positions.add(pr["leaf_index"])
        self.assertEqual(len(seen_positions), 2)

    def test_attestation_only_anchor(self):
        self.ledger.submit_attestation(
            digest="sha256:" + "33" * 32, type_="digest", ref=None,
            submitted_at="2026-07-27T00:00:00Z")
        anchor = self.ledger.create_anchor("t")
        self.assertEqual(anchor["leaf_count"], 1)
        self.assertEqual(anchor["attestations"], 1)

    def test_idempotent_while_pending_new_after_anchor(self):
        d = "sha256:" + "44" * 32
        r1, idem1 = self.ledger.submit_attestation(
            digest=d, type_="digest", ref=None, submitted_at="t1")
        r2, idem2 = self.ledger.submit_attestation(
            digest=d, type_="digest", ref=None, submitted_at="t2")
        self.assertFalse(idem1)
        self.assertTrue(idem2)
        self.assertEqual(r1["attestation_id"], r2["attestation_id"])
        self.ledger.create_anchor("t3")
        # once anchored, the same digest may be attested again (re-anchoring)
        r3, idem3 = self.ledger.submit_attestation(
            digest=d, type_="digest", ref=None, submitted_at="t4")
        self.assertFalse(idem3)
        self.assertNotEqual(r1["attestation_id"], r3["attestation_id"])


class TestAttestService(unittest.TestCase):
    """POST /attest through the HTTP service, dev gate."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.app = App(signer=Signer.generate(),
                      ledger=Ledger(os.path.join(cls.dir.name, "s.db")),
                      verifier=MockVerifier(), gate="dev",
                      price_base_units="1000", pay_to=PAYEE,
                      seller_id="op.example", chain="base-sepolia",
                      base_url="http://x")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.dir.cleanup()

    def _req(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_attest_requires_payment(self):
        code, obj = self._req("POST", "/attest",
                              {"hash": "sha256:" + "aa" * 32})
        self.assertEqual(code, 402)
        self.assertEqual(obj["x402Version"], 2)
        self.assertEqual(obj["resource"]["url"], "http://x/attest")  # v2 ResourceInfo

    def test_bad_hash_400(self):
        code, _ = self._req("POST", "/attest", {"hash": "0xnope"},
                            {"X-PAYMENT": "t"})
        self.assertEqual(code, 400)

    def test_full_attest_lifecycle(self):
        digest = "sha256:" + "bb" * 32
        code, obj = self._req("POST", "/attest",
                              {"hash": digest, "type": "aar-chain-head",
                               "ref": "https://example.com/chain"},
                              {"X-PAYMENT": "t"})
        self.assertEqual(code, 201, obj)
        aid = obj["attestation"]["attestation_id"]
        self.assertEqual(obj["attestation"]["status"], "pending")
        # proof not available yet -> 409 with a helpful message
        code, obj2 = self._req("GET", f"/attest/{aid}/proof")
        self.assertEqual(code, 409)
        # also issue a receipt so the batch is mixed
        code, obj3 = self._req("POST", "/receipts", {
            "settlement": {"chain": "base-sepolia", "tx_hash": "0x" + "e1" * 32,
                           "amount_base_units": "5000", "payer": PAYER,
                           "payee": PAYEE},
            "commerce": sample_commerce()}, {"X-PAYMENT": "t"})
        self.assertEqual(code, 201)
        rid = obj3["receipt"]["payload"]["receipt_id"]
        # seal the batch
        code, aobj = self._req("POST", "/anchor", {})
        self.assertEqual(code, 201)
        self.assertEqual(aobj["anchor"]["attestations"], 1)
        # attestation proof now verifies OFFLINE against the anchor root
        code, proof = self._req("GET", f"/attest/{aid}/proof")
        self.assertEqual(code, 200)
        self.assertEqual(proof["leaf_data"], digest)
        self.assertTrue(verify_inclusion(
            proof["leaf_index"], proof["tree_size"],
            proof["leaf_data"].encode(),
            [bytes.fromhex(h) for h in proof["audit_path"]],
            bytes.fromhex(proof["root"])))
        # the receipt's proof verifies against the SAME root
        code, rproof = self._req("GET", f"/receipts/{rid}/proof")
        self.assertEqual(code, 200)
        self.assertEqual(rproof["root"], proof["root"])
        self.assertTrue(verify_inclusion(
            rproof["leaf_index"], rproof["tree_size"],
            rproof["leaf_data"].encode(),
            [bytes.fromhex(h) for h in rproof["audit_path"]],
            bytes.fromhex(rproof["root"])))
        # record endpoint reflects anchored status
        code, rec = self._req("GET", f"/attest/{aid}")
        self.assertEqual(rec["status"], "anchored")

    def test_unknown_attestation_404(self):
        code, _ = self._req("GET", "/attest/att_doesnotexist")
        self.assertEqual(code, 404)
        code, _ = self._req("GET", "/attest/att_doesnotexist/proof")
        self.assertEqual(code, 404)


class TestAttestFacilitator(unittest.TestCase):
    """Money-fairness of /attest under the real gate: settle only on 201."""

    def _make(self, fac):
        d = tempfile.mkdtemp()
        app = App(signer=Signer.generate(), ledger=Ledger(os.path.join(d, "f.db")),
                  verifier=MockVerifier(), gate="facilitator",
                  price_base_units="2000", pay_to=PAYEE, seller_id="op",
                  chain="base-sepolia", base_url="http://x",
                  facilitator=fac, bind_payer=True)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def _post(self, port, body, xpayment=None):
        headers = {"Content-Type": "application/json"}
        if xpayment:
            headers["X-PAYMENT"] = xpayment
        req = urllib.request.Request(f"http://127.0.0.1:{port}/attest",
                                     data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_valid_attest_settles(self):
        fac = FakeFacilitator()
        port = self._make(fac)
        code, _ = self._post(port, {"hash": "sha256:" + "cc" * 32},
                             xpayment=make_xpayment(PAYER))
        self.assertEqual(code, 201)
        self.assertEqual(fac.settle_calls, 1)

    def test_invalid_body_does_not_settle(self):
        fac = FakeFacilitator()
        port = self._make(fac)
        code, _ = self._post(port, {"hash": "garbage"},
                             xpayment=make_xpayment(PAYER))
        self.assertEqual(code, 400)
        self.assertEqual(fac.settle_calls, 0)

    def test_idempotent_resubmit_does_not_settle_twice(self):
        fac = FakeFacilitator()
        port = self._make(fac)
        d = {"hash": "sha256:" + "dd" * 32}
        c1, _ = self._post(port, d, xpayment=make_xpayment(PAYER))
        c2, obj2 = self._post(port, d, xpayment=make_xpayment(PAYER))
        self.assertEqual((c1, c2), (201, 200))
        self.assertTrue(obj2.get("idempotent"))
        self.assertEqual(fac.settle_calls, 1)

    def test_settle_failure_queues_no_attestation(self):
        # "No settled fee => no attestation": a settle failure yields an error
        # and NOTHING is queued, so a retry (settle OK) is a fresh submission.
        fac = FakeFacilitator(settle_ok=False)
        port = self._make(fac)
        d = {"hash": "sha256:" + "ee" * 32}
        code, obj = self._post(port, d, xpayment=make_xpayment(PAYER))
        self.assertEqual(code, 402, obj)
        self.assertNotIn("attestation", obj)
        fac.settle_ok = True
        code, obj = self._post(port, d, xpayment=make_xpayment(PAYER))
        self.assertEqual(code, 201, obj)          # fresh submission, not idempotent
        self.assertNotIn("idempotent", obj)


class TestPublisher(unittest.TestCase):
    """On-chain anchor publisher: offline signing + broadcast via fake RPC."""

    def test_off_returns_none(self):
        from traceipt.publisher import make_publisher
        self.assertIsNone(make_publisher("off", "base-sepolia"))

    def test_mock_publisher_shape(self):
        from traceipt.publisher import make_publisher
        pub = make_publisher("mock", "base-sepolia")
        net, tx = pub("ab" * 32)
        self.assertEqual(net, "mock")
        self.assertTrue(tx.startswith("0x") and len(tx) == 66)

    def test_onchain_signs_and_broadcasts(self):
        from traceipt.publisher import make_publisher
        calls = {}
        def fake_rpc(method, params):
            calls.setdefault(method, []).append(params)
            if method == "eth_getTransactionCount":
                return "0x5"
            if method == "eth_gasPrice":
                return "0x3b9aca00"
            if method == "eth_sendRawTransaction":
                # a real 0x-prefixed signed tx blob was passed
                assert params[0].startswith("0x") and len(params[0]) > 100
                return "0x" + "fe" * 32
            raise AssertionError(method)
        pub = make_publisher("onchain", "base-sepolia",
                             private_key="0x" + "11" * 32, transport=fake_rpc)
        net, tx = pub("ab" * 32)
        self.assertEqual(net, "base-sepolia")
        self.assertEqual(tx, "0x" + "fe" * 32)
        # it read nonce + gas, then broadcast exactly once
        self.assertEqual(len(calls["eth_sendRawTransaction"]), 1)
        self.assertIn("eth_getTransactionCount", calls)

    def test_onchain_requires_key(self):
        from traceipt.publisher import make_publisher
        with self.assertRaises(ValueError):
            make_publisher("onchain", "base-sepolia")

    def test_anchor_records_publisher_tx(self):
        # the ledger anchor path stamps the publisher's (network, tx)
        d = tempfile.mkdtemp()
        led = Ledger(os.path.join(d, "p.db"))
        signer = Signer.generate()
        seq, prev = led.next_link("s1")
        led.append(signer.sign_envelope(sample_receipt(seq=seq, prev=prev, seller_id="s1")))
        anchor = led.create_anchor("t", publisher=lambda root: ("base", "0x" + "cc" * 32))
        self.assertEqual(anchor["onchain_network"], "base")
        self.assertEqual(anchor["onchain_tx"], "0x" + "cc" * 32)
        # and the receipt's inclusion proof carries that tx through
        con_rid = None
        import sqlite3
        con = sqlite3.connect(os.path.join(d, "p.db"))
        con_rid = con.execute("SELECT receipt_id FROM receipts").fetchone()[0]
        con.close()
        pr = led.inclusion_for(con_rid)
        self.assertEqual(pr["onchain_tx"], "0x" + "cc" * 32)

    def test_failed_publisher_does_not_seal_batch(self):
        # if the publisher raises, nothing is committed and it can retry
        d = tempfile.mkdtemp()
        led = Ledger(os.path.join(d, "q.db"))
        signer = Signer.generate()
        seq, prev = led.next_link("s1")
        led.append(signer.sign_envelope(sample_receipt(seq=seq, prev=prev, seller_id="s1")))
        def boom(root):
            raise RuntimeError("rpc down")
        with self.assertRaises(RuntimeError):
            led.create_anchor("t", publisher=boom)
        # batch NOT sealed -> a retry with a working publisher still finds it
        anchor = led.create_anchor("t2", publisher=lambda r: ("base", "0x" + "dd" * 32))
        self.assertEqual(anchor["leaf_count"], 1)
        self.assertEqual(anchor["onchain_tx"], "0x" + "dd" * 32)


class TestKeyRotation(unittest.TestCase):
    """A receipt signed by a now-retired key must still verify after the
    operator rotates to a new active key, as long as the old PUBLIC key is
    kept in the published JWKS history."""

    def test_retired_key_receipt_still_verifies(self):
        d = tempfile.mkdtemp()
        old = Signer.generate()
        # issue a receipt under the OLD key
        app_old = App(signer=old, ledger=Ledger(os.path.join(d, "k.db")),
                      verifier=MockVerifier(), gate="dev", price_base_units="2000",
                      pay_to=PAYEE, seller_id="op", chain="base-sepolia",
                      base_url="http://x")
        code, obj = app_old.issue({
            "settlement": {k: v for k, v in sample_settlement().items()
                           if k not in ("verified", "verification_method")},
            "commerce": sample_commerce()})
        self.assertEqual(code, 201)
        rid = obj["receipt"]["payload"]["receipt_id"]

        # ROTATE: new active key, old key's PUBLIC jwk kept in history,
        # same ledger/db.
        new = Signer.generate()
        app_new = App(signer=new, ledger=Ledger(os.path.join(d, "k.db")),
                      verifier=MockVerifier(), gate="dev", price_base_units="2000",
                      pay_to=PAYEE, seller_id="op", chain="base-sepolia",
                      base_url="http://x", extra_public_jwks=[old.jwk()])

        # jwks now publishes both keys
        kids = {k["kid"] for k in app_new._jwks()["keys"]}
        self.assertIn(old.kid, kids)
        self.assertIn(new.kid, kids)

        # the old-key receipt still verifies and its chain is intact
        code, report = app_new.verify_report(rid)
        self.assertEqual(report["signature"], "valid")
        self.assertEqual(report["verdict"], "PASS")

    def test_without_history_retired_key_fails(self):
        d = tempfile.mkdtemp()
        old = Signer.generate()
        app_old = App(signer=old, ledger=Ledger(os.path.join(d, "k2.db")),
                      verifier=MockVerifier(), gate="dev", price_base_units="2000",
                      pay_to=PAYEE, seller_id="op", chain="base-sepolia",
                      base_url="http://x")
        _, obj = app_old.issue({
            "settlement": {k: v for k, v in sample_settlement().items()
                           if k not in ("verified", "verification_method")},
            "commerce": sample_commerce()})
        rid = obj["receipt"]["payload"]["receipt_id"]
        new = Signer.generate()
        # rotate WITHOUT keeping the old public key -> old receipt no longer verifies
        app_new = App(signer=new, ledger=Ledger(os.path.join(d, "k2.db")),
                      verifier=MockVerifier(), gate="dev", price_base_units="2000",
                      pay_to=PAYEE, seller_id="op", chain="base-sepolia",
                      base_url="http://x")
        _, report = app_new.verify_report(rid)
        self.assertTrue(report["signature"].startswith("INVALID"))


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


class TestCompleteness(unittest.TestCase):
    """Provable completeness: prove the receipt SET is whole, not just that
    each receipt is real. See completeness.py."""

    def _app(self):
        d = tempfile.mkdtemp()
        return App(signer=Signer.generate(), ledger=Ledger(os.path.join(d, "c.db")),
                   verifier=MockVerifier(), gate="off", price_base_units="2000",
                   pay_to=PAYEE, seller_id="op.example", chain="base-sepolia",
                   base_url="http://x")

    def _issue(self, app, i):
        s = sample_settlement(tx_hash="0x" + ("%02x" % (i + 1)) * 32)
        code, obj = app.issue({"settlement": s, "commerce": sample_commerce()})
        self.assertEqual(code, 201, obj)

    def _checker(self, app):
        jwks = app._jwks()
        return lambda env: verify_envelope(env, jwks)

    def test_complete_chain_verifies(self):
        app = self._app()
        for i in range(3):
            self._issue(app, i)
        code, rep = app.completeness_report("op.example")
        self.assertEqual(code, 200)
        self.assertEqual(rep["count"], 3)
        ok, problems = verify_completeness(rep, self._checker(app))
        self.assertTrue(ok, problems)

    def test_hidden_receipt_detected(self):
        app = self._app()
        for i in range(3):
            self._issue(app, i)
        _, rep = app.completeness_report("op.example")
        rep["manifest"].pop(1)          # try to hide the middle receipt
        ok, problems = verify_completeness(rep, self._checker(app))
        self.assertFalse(ok)
        self.assertTrue(any("count" in p for p in problems), problems)

    def test_tampered_head_detected(self):
        app = self._app()
        for i in range(2):
            self._issue(app, i)
        _, rep = app.completeness_report("op.example")
        rep["manifest"][-1]["receipt_hash"] = "sha256:" + "00" * 32
        ok, problems = verify_completeness(rep, self._checker(app))
        self.assertFalse(ok)

    def test_forged_checkpoint_rejected(self):
        app = self._app()
        for i in range(2):
            self._issue(app, i)
        _, rep = app.completeness_report("op.example")
        # re-sign the checkpoint with a key that is NOT in the issuer JWKS
        rep["checkpoint"] = Signer.generate().sign_envelope(rep["checkpoint"]["payload"])
        ok, problems = verify_completeness(rep, self._checker(app))
        self.assertFalse(ok)
        self.assertTrue(any("INVALID" in p for p in problems), problems)

    def test_empty_chain_is_vacuously_complete(self):
        app = self._app()
        _, rep = app.completeness_report("op.example")
        self.assertEqual(rep["count"], 0)
        ok, problems = verify_completeness(rep, self._checker(app))
        self.assertTrue(ok, problems)

    def test_seller_binding_rejects_cross_seller_checkpoint(self):
        # Audit fix: a validly-signed checkpoint for another seller (or a
        # tampered report seller_id) must not pass as this seller.
        app = self._app()
        for i in range(2):
            self._issue(app, i)
        _, rep = app.completeness_report("op.example")
        chk = self._checker(app)
        self.assertTrue(verify_completeness(rep, chk, expected_seller="op.example")[0])
        self.assertFalse(verify_completeness(rep, chk, expected_seller="attacker")[0])
        r2 = copy.deepcopy(rep)
        r2["seller_id"] = "spoofed"
        self.assertFalse(verify_completeness(r2, chk)[0])

    def test_completeness_over_http(self):
        app = self._app()
        for i in range(2):
            self._issue(app, i)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/chain/op.example/completeness") as r:
            rep = json.loads(r.read())
        ok, problems = verify_completeness(rep, self._checker(app))
        self.assertTrue(ok, problems)


class TestSelectiveDisclosure(unittest.TestCase):
    """Reveal a subset of a receipt's fields and prove it, hiding the rest.
    See disclosure.py."""

    def _app(self, **over):
        d = tempfile.mkdtemp()
        cfg = dict(signer=Signer.generate(), ledger=Ledger(os.path.join(d, "d.db")),
                   verifier=MockVerifier(), gate="off", price_base_units="2000",
                   pay_to=PAYEE, seller_id="op.example", chain="base-sepolia",
                   base_url="http://x")
        cfg.update(over)
        return App(**cfg)

    def _issue(self, app):
        s = sample_settlement(tx_hash="0x" + "11" * 32)
        code, obj = app.issue({"settlement": s, "commerce": sample_commerce()})
        self.assertEqual(code, 201, obj)
        return obj["receipt"]["payload"]["receipt_id"]

    def test_receipt_carries_signed_disclosure_commitment(self):
        app = self._app()
        rid = self._issue(app)
        env = app.ledger.get(rid)
        verify_envelope(env, app._jwks())          # signature covers the block
        self.assertEqual(env["payload"]["disclosure"]["alg"], "merkle-sha256-v1")

    def test_reveal_subset_hides_the_rest(self):
        app = self._app()
        rid = self._issue(app)
        code, denv = app.disclose_receipt(
            rid, ["settlement.amount_base_units", "issued_at"])
        self.assertEqual(code, 200)
        body = verify_envelope(denv, app._jwks())  # issuer-authenticated
        ok, revealed, problems = verify_disclosure(body)
        self.assertTrue(ok, problems)
        self.assertEqual(revealed["settlement.amount_base_units"], "5000")
        self.assertIn("issued_at", revealed)
        # the sensitive counterparties are NOT recoverable
        self.assertNotIn("settlement.payer", revealed)
        self.assertNotIn("settlement.payee", revealed)

    def test_tampered_revealed_value_fails(self):
        app = self._app()
        rid = self._issue(app)
        _, denv = app.disclose_receipt(rid, ["settlement.amount_base_units"])
        body = verify_envelope(denv, app._jwks())
        body["revealed"][0]["value"] = "999999999"   # lie about the amount
        ok, revealed, problems = verify_disclosure(body)
        self.assertFalse(ok)
        self.assertTrue(problems)

    def test_forged_disclosure_rejected_at_signature(self):
        app = self._app()
        rid = self._issue(app)
        _, denv = app.disclose_receipt(rid, ["issued_at"])
        forged = Signer.generate().sign_envelope(denv["payload"])
        with self.assertRaises(ValueError):
            verify_envelope(forged, app._jwks())     # key not in JWKS

    def test_disclose_endpoint_is_admin_gated(self):
        app = self._app(admin_token="s3cret")
        rid = self._issue(app)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)

        def post(headers):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/receipts/{rid}/disclose",
                data=json.dumps({"reveal": ["issued_at"]}).encode(),
                headers={"Content-Type": "application/json", **headers})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        code, _ = post({})                                    # no token
        self.assertEqual(code, 403)
        code, denv = post({"Authorization": "Bearer s3cret"})  # with token
        self.assertEqual(code, 200)
        ok, revealed, problems = verify_disclosure(verify_envelope(denv, app._jwks()))
        self.assertTrue(ok, problems)
        self.assertIn("issued_at", revealed)


class TestComplianceBinding(unittest.TestCase):
    """Compliance-bound receipts: bind a pre-payment verdict and prove
    'screened before paid'. See screening.py."""

    VERDICT = {"verdict": "GO", "score": 0.98, "ofac": "clear", "receipt_id": "bw_x"}

    def _app(self):
        d = tempfile.mkdtemp()
        return App(signer=Signer.generate(), ledger=Ledger(os.path.join(d, "s.db")),
                   verifier=MockVerifier(), gate="off", price_base_units="2000",
                   pay_to=PAYEE, seller_id="op.example", chain="base-sepolia",
                   base_url="http://x")

    def _issue(self, app, screening, tx="0x" + "11" * 32, issued_over=None):
        s = sample_settlement(tx_hash=tx)
        c = sample_commerce(screening=screening)
        code, obj = app.issue({"settlement": s, "commerce": c})
        self.assertEqual(code, 201, obj)
        return obj["receipt"]["payload"]

    def test_digest_matches_blackwall_formula(self):
        import hashlib as _h
        bw = "sha256:" + _h.sha256(
            json.dumps(self.VERDICT, sort_keys=True,
                       separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(verdict_digest(self.VERDICT), bw)

    def test_bound_verdict_verifies(self):
        app = self._app()
        sc = build_screening(self.VERDICT, decision="GO", screener="blackwall",
                             decided_at="2026-07-31T10:00:00Z")
        payload = self._issue(app, sc)
        ok, problems = verify_screening(payload, self.VERDICT)
        self.assertTrue(ok, problems)

    def test_swapped_verdict_detected(self):
        app = self._app()
        payload = self._issue(app, build_screening(self.VERDICT, decision="GO"))
        ok, problems = verify_screening(payload, {**self.VERDICT, "verdict": "STOP"})
        self.assertFalse(ok)
        self.assertTrue(any("does not match" in p for p in problems))

    def test_screening_after_issue_rejected(self):
        # A verdict "decided" after the receipt was issued is not before-payment.
        app = self._app()
        sc = build_screening(self.VERDICT, decided_at="2099-01-01T00:00:00Z")
        payload = self._issue(app, sc)
        ok, problems = verify_screening(payload, self.VERDICT)
        self.assertFalse(ok)
        self.assertTrue(any("AFTER" in p for p in problems))

    def test_unbound_receipt_is_not_compliance_bound(self):
        app = self._app()
        s = sample_settlement(tx_hash="0x" + "22" * 32)
        code, obj = app.issue({"settlement": s, "commerce": sample_commerce()})
        self.assertEqual(code, 201, obj)
        ok, problems = verify_screening(obj["receipt"]["payload"], self.VERDICT)
        self.assertFalse(ok)
        self.assertTrue(any("no screening block" in p for p in problems))

    def test_bad_screening_block_is_400(self):
        app = self._app()
        s = sample_settlement(tx_hash="0x" + "33" * 32)
        c = sample_commerce(screening={"verdict_hash": "not-a-hash"})
        code, obj = app.issue({"settlement": s, "commerce": c})
        self.assertEqual(code, 400, obj)

    def test_anchored_ordering_is_the_trustless_check(self):
        self.assertTrue(verify_anchored_before(
            "2026-01-01T09:00:00Z", "2026-01-01T10:00:00Z")[0])
        self.assertFalse(verify_anchored_before(
            "2026-01-01T11:00:00Z", "2026-01-01T10:00:00Z")[0])
        self.assertFalse(verify_anchored_before(None, "2026-01-01T10:00:00Z")[0])

    def test_decided_at_is_self_asserted_binding_still_holds(self):
        # A backdated decided_at still passes verify_screening -- that check is
        # BINDING + self-consistency, not trustless ordering (which is
        # verify_anchored_before). Documents the audited boundary.
        app = self._app()
        payload = self._issue(
            app, build_screening(self.VERDICT, decided_at="2000-01-01T00:00:00Z"))
        ok, _ = verify_screening(payload, self.VERDICT)
        self.assertTrue(ok)

    def test_composes_with_disclosure(self):
        # Prove "screened GO" while hiding the amount -- the killer combination.
        app = self._app()
        sc = build_screening(self.VERDICT, decision="GO", screener="blackwall")
        payload = self._issue(app, sc)
        code, denv = app.disclose_receipt(
            payload["receipt_id"],
            ["commerce.screening.decision", "commerce.screening.verdict_hash"])
        self.assertEqual(code, 200)
        ok, revealed, problems = verify_disclosure(verify_envelope(denv, app._jwks()))
        self.assertTrue(ok, problems)
        self.assertEqual(revealed["commerce.screening.decision"], "GO")
        self.assertNotIn("settlement.amount_base_units", revealed)


if __name__ == "__main__":
    unittest.main()
