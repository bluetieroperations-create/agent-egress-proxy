"""
test_two_stage_signer.py -- payload-sim Phase 2 (signer recovery) as a DEFERRED
second-stage check behind the fast verdict.

The security-critical properties:
  * Phase 1 (recipient/amount/asset/chain) STAYS inline and hard-STOPs even in fast mode.
  * Deferring Phase 2 is EXPLICIT (signer_status='deferred' + a reason) -- a fast GO is
    never silently mistaken for signer-verified.
  * The second stage (verify_signed_payment) catches exactly the forged signer the fast
    path let through.
  * Default (verify_signer=True) is UNCHANGED -- forged signer STOPs inline.

Each test states the mutation it kills.
"""
import base64
import json
import unittest

import blackwall as bw

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CP = "0x" + "a" * 40
OTHER = "0x" + "d" * 40
GOOD = {"settlement_count": 500, "dispute_rate": 0.0, "distinct_payers": 30}


class _Src:
    def lookup(self, cp):
        return dict(GOOD)


def _signed(pk, *, to=CP, asset=USDC, frm=None):
    """A real EIP-3009 signed payment (pure-Python secp256k1), base64 X-PAYMENT."""
    import secp256k1 as S
    import eip712 as E
    signer = E.pubkey_to_address(S.privkey_to_pub(pk))
    message = {"from": frm or signer, "to": to, "value": "90000",
               "validAfter": "0", "validBefore": "9999999999", "nonce": "0x" + "1" * 64}
    z = E.transfer_authorization_digest(
        {"name": "USD Coin", "version": "2", "chainId": 8453,
         "verifyingContract": asset}, message)
    r, s, rec = S.ecdsa_sign(z, pk)
    sig = "0x" + (r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([27 + rec])).hex()
    payment = {"x402Version": 2,
               "accepted": {"scheme": "exact", "network": "base", "asset": asset},
               "payload": {"signature": sig, "authorization": message}}
    return base64.b64encode(json.dumps(payment).encode()).decode(), signer


def _body(x_payment):
    return {"counterparty": CP, "amount": "0.09", "asset": USDC, "chain": "base",
            "payment_authorization": x_payment}


class TestTwoStage(unittest.TestCase):
    def _victim(self, pk):
        import eip712 as E
        import secp256k1 as S
        return E.pubkey_to_address(S.privkey_to_pub(pk))

    def test_default_verifies_signer_inline(self):
        # unchanged behavior: a forged signer (from != actual signer) STOPs inline.
        xp, _ = _signed(0xA11CE, frm=self._victim(0xBEEF))
        v, err = bw.forecast(_body(xp), _Src())          # verify_signer defaults True
        self.assertIsNone(err)
        self.assertEqual(v["verdict"], "STOP")
        self.assertEqual(v["signals"]["payload_signer_status"], "mismatch")

    def test_fast_path_defers_signer(self):
        # verify_signer=False: the SAME forged payment is NOT stopped inline (deferred),
        # but the deferral is EXPLICIT. Mutation: run Phase 2 anyway -> verdict STOPs and
        # signer_status != 'deferred', failing this.
        xp, _ = _signed(0xA11CE, frm=self._victim(0xBEEF))
        v, err = bw.forecast(_body(xp), _Src(), verify_signer=False)
        self.assertIsNone(err)
        self.assertNotEqual(v["verdict"], "STOP")        # forged signer NOT caught inline
        self.assertEqual(v["signals"]["payload_signer_status"], "deferred")
        self.assertTrue(any("DEFERRED" in r for r in v["reasons"]))

    def test_second_stage_catches_forged_signer(self):
        # the deferred check, run as stage 2, catches exactly what the fast path missed.
        xp, _ = _signed(0xA11CE, frm=self._victim(0xBEEF))
        r = bw.verify_signed_payment(_body(xp))
        self.assertFalse(r["ok"])
        self.assertEqual(r["signer_status"], "mismatch")
        self.assertTrue(r["mismatches"])

    def test_second_stage_confirms_valid_signer(self):
        xp, _ = _signed(0xA11CE)                         # from == actual signer
        r = bw.verify_signed_payment(_body(xp))
        self.assertTrue(r["ok"])
        self.assertEqual(r["signer_status"], "confirmed")

    def test_phase1_hardstop_still_inline_in_fast_mode(self):
        # THE key safety property: deferring Phase 2 must NOT drop the cheap Phase-1
        # field-match hard stop. A payment paying the WRONG recipient STOPs even fast.
        xp, _ = _signed(0xA11CE, to=OTHER)               # signed payment pays OTHER, not CP
        v, err = bw.forecast(_body(xp), _Src(), verify_signer=False)
        self.assertEqual(v["verdict"], "STOP")           # recipient mismatch caught inline
        self.assertTrue(v["hard_stop"])

    def test_no_payment_is_not_applicable(self):
        v, err = bw.forecast({"counterparty": CP, "amount": "0.09", "asset": USDC,
                              "chain": "base"}, _Src(), verify_signer=False)
        self.assertEqual(v["signals"]["payload_signer_status"], "not_applicable")
        self.assertNotIn("DEFERRED", " ".join(v["reasons"]))  # nothing to defer


class TestTwoStageHTTP(unittest.TestCase):
    """The two-stage flow over HTTP: a `defer_signer` forecast returns fast + deferred,
    and POST /v1/verify-signer completes the check and catches a forged signer."""
    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        handler = type("_H", (bw._Handler,), {"reputation_source": _Src()})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()

    def _post(self, path, obj):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", path, body=json.dumps(obj),
                  headers={"Content-Type": "application/json"})
        r = c.getresponse(); body = json.loads(r.read()); c.close()
        return r.status, body

    def test_defer_then_verify_over_http(self):
        import eip712 as E
        import secp256k1 as S
        victim = E.pubkey_to_address(S.privkey_to_pub(0xBEEF))
        xp, _ = _signed(0xA11CE, frm=victim)          # signed by A11CE, from=victim (forged)
        body = _body(xp)
        body["defer_signer"] = True
        # stage 1: fast verdict, signer deferred (forged signer NOT caught yet)
        st, v = self._post("/v1/forecast-payment", body)
        self.assertEqual(st, 200)
        self.assertNotEqual(v["verdict"], "STOP")
        self.assertEqual(v["signals"]["payload_signer_status"], "deferred")
        # stage 2: the dedicated endpoint catches the forged signer
        st2, r = self._post("/v1/verify-signer", body)
        self.assertEqual(st2, 200)
        self.assertFalse(r["ok"])
        self.assertEqual(r["signer_status"], "mismatch")

    def test_malformed_defer_flag_does_not_reduce_verification(self):
        # AUDIT regression: a truthy-but-not-True flag (the STRING "false") must NOT
        # trigger fast mode -- that would silently drop inline signer verification. Strict
        # `is True` fails toward MORE verification: the forged signer STOPs inline.
        import eip712 as E
        import secp256k1 as S
        victim = E.pubkey_to_address(S.privkey_to_pub(0xBEEF))
        xp, _ = _signed(0xA11CE, frm=victim)
        for bad in ("false", 1, "true", "yes"):
            body = _body(xp)
            body["defer_signer"] = bad
            st, v = self._post("/v1/forecast-payment", body)
            self.assertEqual(v["verdict"], "STOP", "defer_signer=%r wrongly deferred" % bad)
            self.assertEqual(v["signals"]["payload_signer_status"], "mismatch")


if __name__ == "__main__":
    unittest.main()
