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
from receipts.x402_gate import (
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
        from receipts.merkle import verify_inclusion as _vi
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


from receipts.invoice import render_invoice, usdc, vat_breakdown
from receipts.merkle import (
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
        from receipts import qr
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
        from receipts import qr
        m = qr.encode("https://r.example/verify/rcpt_x")
        self.assertTrue(all(len(row) == len(m) for row in m))
        self.assertTrue(all(v in (0, 1) for row in m for v in row))

    def test_too_long_raises(self):
        from receipts import qr
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

    def test_settle_failure_still_returns_receipt_with_warning(self):
        _, fac, port = self._make(facilitator=FakeFacilitator(settle_ok=False))
        code, obj, _ = self._post(port, self._body(PAYER, "0x" + "b4" * 32),
                                  xpayment=make_xpayment(PAYER))
        self.assertEqual(code, 201)
        self.assertIn("settlement_warning", obj)


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


if __name__ == "__main__":
    unittest.main()
