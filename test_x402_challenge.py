"""Tests for the WWW-Authenticate payment challenge parser.

Fixtures are REAL headers captured live on 2026-08-27. Each test names the
mutation it kills.
"""

import unittest
import x402_challenge as x

# api.webbersites.com -- evm / Base USDC, decimals present
EVM = ('Payment id="s-GnDju9Vmtr6w8Hchbma", realm="x402-data-api.onrender.com", '
       'method="evm", intent="charge", request="eyJhbW91bnQiOiIxMDAwMCIsImN1cnJlbmN5Ijoi'
       'MHg4MzM1ODlmQ0Q2ZURiNkUwOGY0YzdDMzJENGY3MWI1NGJkQTAyOTEzIiwibWV0aG9kRGV0YWlscyI6'
       'eyJjaGFpbklkIjo4NDUzLCJjcmVkZW50aWFsVHlwZXMiOlsiYXV0aG9yaXphdGlvbiJdLCJkZWNpbWFs'
       'cyI6Nn0sInJlY2lwaWVudCI6IjB4ZGQ1ZmNFYTgxQ0E2ZjFFQjM5RkVkNUI5NzNERTc5NDI5M0I4MWJh'
       'NiJ9", description="WebberSites API", expires="2026-08-28T17:35:18.666Z"')

# api.onesource.io -- method "tempo", chain 4217, NO decimals
TEMPO = ('Payment id="29wAKedovy", realm="api.onesource.io", method="tempo", '
         'intent="charge", request="eyJhbW91bnQiOiIzMDAwIiwiY3VycmVuY3kiOiIweDIwQzAwMDAw'
         'MDAwMDAwMDAwMDAwMDAwMGI5NTM3ZDExYzYwRThiNTAiLCJkZXNjcmlwdGlvbiI6ImFwaS5vbmVzb3'
         'VyY2UuaW8gQVBJIGNhbGwiLCJtZXRob2REZXRhaWxzIjp7ImNoYWluSWQiOjQyMTd9LCJyZWNpcGll'
         'bnQiOiIweDE5QjhlOTkwNzlBNTU1OGZmNDQ2MDM1N2IwYTc4ZTE0YTdGNjAwQjcifQ", '
         'expires="2026-08-28T17:35:18.954Z"')


class TestParams(unittest.TestCase):
    def test_extracts_quoted_params(self):
        p = x.parse_params(EVM)
        self.assertEqual(p["method"], "evm")
        self.assertEqual(p["realm"], "x402-data-api.onrender.com")

    def test_rejects_other_auth_schemes(self):
        # kills: treating an ordinary 401 Bearer challenge as a payment demand
        self.assertEqual(x.parse_params('Bearer realm="api", error="invalid_token"'), {})
        self.assertEqual(x.parse_params('Basic realm="x"'), {})

    def test_missing_header_is_empty(self):
        # kills: raising on a 402 that carries no header at all
        self.assertEqual(x.parse_params(None), {})
        self.assertEqual(x.parse_params(""), {})

    def test_scheme_match_is_case_insensitive(self):
        # kills: rejecting 'payment' -- HTTP auth schemes are case-insensitive
        self.assertTrue(x.parse_params('payment id="a", request="e30"'))


class TestDecode(unittest.TestCase):
    def test_base64url_and_missing_padding(self):
        # kills: standard b64decode, which RAISES on '-'/'_' and on unpadded
        # input -- the live headers use both, so this is the whole format
        self.assertEqual(x.decode_request("eyJhIjoxfQ"), {"a": 1})

    def test_garbage_is_none(self):
        # kills: propagating a decode exception into the caller's 402 handling
        self.assertIsNone(x.decode_request("!!!not base64!!!"))
        self.assertIsNone(x.decode_request(None))

    def test_valid_base64_that_is_not_json_is_none(self):
        # kills: returning raw bytes as if they were a parsed payload
        self.assertIsNone(x.decode_request("aGVsbG8="))


class TestToAccepts(unittest.TestCase):
    def test_evm_challenge_maps_to_engine_fields(self):
        # kills: any field-name drift from what decide_payment/forecast read
        a = x.to_accepts(EVM)
        self.assertEqual(a["payTo"], "0xdd5fcEa81CA6f1EB39FEd5B973DE794293B81ba6")
        self.assertEqual(a["maxAmountRequired"], "10000")
        self.assertEqual(a["asset"], "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
        self.assertEqual(a["network"], "eip155:8453")
        self.assertEqual(a["decimals"], 6)

    def test_non_base_chain_is_carried_not_assumed(self):
        # kills: hardcoding Base -- a live host settles on chain 4217 (Tempo),
        # and assuming 8453 would score the payment against the wrong chain
        a = x.to_accepts(TEMPO)
        self.assertEqual(a["chainId"], 4217)
        self.assertEqual(a["network"], "eip155:4217")
        self.assertEqual(a["method"], "tempo")

    def test_absent_decimals_is_none_not_six(self):
        # kills: defaulting decimals to 6 -- guessing mis-scales the amount by
        # 10^n, the same class of error as the PFAS units bug
        self.assertIsNone(x.to_accepts(TEMPO)["decimals"])

    def test_symbol_in_currency_does_not_become_the_address(self):
        # kills: blindly reading `currency` as an address. One live host sends
        # currency="USDC" with the address in `asset`; taking currency would put
        # the string "USDC" in the asset slot and every address compare fails
        import base64, json
        payload = base64.b64encode(json.dumps({
            "amount": "10000", "currency": "USDC",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "recipient": "0xd5f8481D8F25d3966d2010DBf9B47fFbdf745A9E",
        }).encode()).decode()
        a = x.to_accepts('Payment id="a", method="asterpay", request="%s"' % payload)
        self.assertEqual(a["asset"], "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
        self.assertEqual(a["assetSymbol"], "USDC")

    def test_bearer_challenge_yields_nothing(self):
        # kills: manufacturing a payment option from a plain auth challenge
        self.assertIsNone(x.to_accepts('Bearer realm="api"'))

    def test_undecodable_request_yields_nothing(self):
        # kills: emitting an accepts entry with every field None, which would
        # look like a payment to a nowhere address
        self.assertIsNone(x.to_accepts('Payment id="a", request="!!!"'))


class TestAcceptsFromResponse(unittest.TestCase):
    def test_body_entries_come_first(self):
        # kills: reordering, which would change which option a client picks
        body = {"accepts": [{"payTo": "0xbody"}]}
        out = x.accepts_from_response(body, EVM)
        self.assertEqual(out[0]["payTo"], "0xbody")
        self.assertEqual(out[1]["source"], "www-authenticate")

    def test_header_only_endpoint_is_no_longer_invisible(self):
        # kills: the actual bug -- a v2 endpoint parsing to zero options and
        # being unpayable though the engine would score it fine
        self.assertEqual(len(x.accepts_from_response({}, EVM)), 1)

    def test_purely_additive_when_header_is_absent(self):
        # kills: dropping body options when no header is present
        body = {"accepts": [{"payTo": "0xa"}, {"payTo": "0xb"}]}
        self.assertEqual(len(x.accepts_from_response(body, None)), 2)

    def test_non_dict_body_does_not_raise(self):
        # kills: assuming the 402 body is always JSON -- several return HTML
        self.assertEqual(len(x.accepts_from_response("<html>", EVM)), 1)


if __name__ == "__main__":
    unittest.main()


class TestConsumerWiring(unittest.TestCase):
    """The v2 header must reach the consumers that previously read only the body."""

    HDR = {"WWW-Authenticate": EVM}

    def test_directory_liveness_classifies_header_challenge(self):
        # kills: the original regex, which required scheme "x402" and
        # requirements= -- it matched NONE of the 11 live challenges, so every
        # one was recorded as opaque_402, indistinguishable from broken
        import directory_liveness as dl
        accepts, carrier = dl.parse_challenge("", self.HDR)
        self.assertEqual(carrier, dl.HDR_ACCEPTS)
        self.assertEqual(accepts[0]["payTo"],
                         "0xdd5fcEa81CA6f1EB39FEd5B973DE794293B81ba6")

    def test_directory_liveness_body_still_wins(self):
        # kills: the header shadowing a good body, changing what works today
        import directory_liveness as dl
        _, carrier = dl.parse_challenge('{"accepts":[{"payTo":"0xb"}]}', self.HDR)
        self.assertEqual(carrier, dl.BODY_ACCEPTS)

    def test_crawler_records_a_header_only_payee(self):
        # kills: the crawl skipping v2 sellers entirely, so they never enter
        # reputation, price baselines or the directory
        import discovery_crawl as dc
        recs = dc.extract_resources({"resource": "https://x.test/a"}, _headers=self.HDR)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["payTo"],
                         "0xdd5fcea81ca6f1eb39fed5b973de794293b81ba6")
        self.assertEqual(recs[0]["price_atomic"], 10000)
        self.assertEqual(recs[0]["network"], "eip155:8453")

    def test_crawler_is_additive_not_replacing(self):
        # kills: the header entry displacing body entries
        import discovery_crawl as dc
        body = {"accepts": [{"payTo": "0x" + "b" * 40, "maxAmountRequired": "1",
                             "asset": "0x" + "c" * 40, "network": "eip155:8453"}]}
        self.assertEqual(len(dc.extract_resources(body, _headers=self.HDR)), 2)

    def test_crawler_unchanged_without_headers(self):
        # kills: the new branch altering existing body-only crawl behaviour
        import discovery_crawl as dc
        self.assertEqual(dc.extract_resources({"resource": "https://x.test/a"}), [])

    def test_attest_falls_back_to_header(self):
        # kills: traceipt_attest reading only the body, so a v2 Traceipt-style
        # endpoint yields no requirements and the auto-pay silently no-ops
        import traceipt_attest as ta
        got = ta._first_accepts({}, self.HDR)
        self.assertEqual(got["payTo"], "0xdd5fcEa81CA6f1EB39FEd5B973DE794293B81ba6")

    def test_attest_prefers_body(self):
        # kills: the header overriding an explicit body requirement
        import traceipt_attest as ta
        got = ta._first_accepts({"accepts": [{"payTo": "0xbody"}]}, self.HDR)
        self.assertEqual(got["payTo"], "0xbody")

    def test_attest_without_headers_is_unchanged(self):
        # kills: a signature change breaking existing single-arg callers
        import traceipt_attest as ta
        self.assertIsNone(ta._first_accepts({}))
        self.assertEqual(ta._first_accepts({"accepts": [{"payTo": "0xa"}]})["payTo"],
                         "0xa")


class TestUntrustedInput(unittest.TestCase):
    """The challenge is written by an untrusted third-party server, so every
    field is attacker-controlled and may be any JSON type."""

    def _hdr(self, payload):
        import base64, json
        blob = base64.b64encode(json.dumps(payload).encode()).decode()
        return 'Payment id="i", method="evm", request="%s"' % blob

    def test_non_address_recipient_rejected(self):
        # kills: propagating a junk string as a payee address. discovery_crawl
        # validated downstream; traceipt_attest and x402_pay did not.
        self.assertIsNone(x.to_accepts(self._hdr({"amount": "1",
                                                  "recipient": "NOT_AN_ADDRESS"})))

    def test_non_string_recipient_rejected(self):
        # kills: a dict or list reaching the payTo slot and flowing into the
        # payment path as if it were an address
        for bad in ({"evil": 1}, ["0x" + "a" * 40], 12345, None, True):
            self.assertIsNone(
                x.to_accepts(self._hdr({"amount": "1", "recipient": bad})), repr(bad))

    def test_wrong_length_address_rejected(self):
        # kills: a length check loose enough to admit a truncated address
        for bad in ("0x" + "a" * 39, "0x" + "a" * 41, "0x"):
            self.assertIsNone(x.to_accepts(self._hdr({"amount": "1", "recipient": bad})))

    def test_non_hex_address_rejected(self):
        # kills: accepting 42 arbitrary characters that merely start with 0x
        self.assertIsNone(
            x.to_accepts(self._hdr({"amount": "1", "recipient": "0x" + "z" * 40})))

    def test_non_scalar_amount_rejected(self):
        # kills: a dict/list amount reaching the atomic-value comparison
        for bad in ({"a": 1}, [1], None, True):
            self.assertIsNone(
                x.to_accepts(self._hdr({"amount": bad, "recipient": "0x" + "a" * 40})),
                repr(bad))

    def test_non_integer_chain_id_does_not_forge_a_network(self):
        # kills: string-formatting an attacker value into "eip155:<anything>"
        a = x.to_accepts(self._hdr({"amount": "1", "recipient": "0x" + "a" * 40,
                                    "methodDetails": {"chainId": "8453; DROP"}}))
        self.assertIsNone(a["network"])

    def test_non_integer_decimals_is_dropped(self):
        # kills: a string or bool decimals reaching 10**n scaling
        for bad in ("6", True, {"n": 6}):
            a = x.to_accepts(self._hdr({"amount": "1", "recipient": "0x" + "a" * 40,
                                        "methodDetails": {"decimals": bad}}))
            self.assertIsNone(a["decimals"], repr(bad))

    def test_non_dict_method_details_does_not_raise(self):
        # kills: assuming methodDetails is an object
        a = x.to_accepts(self._hdr({"amount": "1", "recipient": "0x" + "a" * 40,
                                    "methodDetails": "nope"}))
        self.assertIsNone(a["chainId"])

    def test_valid_challenge_still_parses(self):
        # kills: hardening so aggressive it rejects the real thing
        a = x.to_accepts(self._hdr({"amount": "10000", "recipient": "0x" + "a" * 40,
                                    "currency": "0x" + "b" * 40,
                                    "methodDetails": {"chainId": 8453, "decimals": 6}}))
        self.assertEqual(a["maxAmountRequired"], "10000")
        self.assertEqual(a["network"], "eip155:8453")
        self.assertEqual(a["decimals"], 6)

    def test_oversize_header_does_not_hang(self):
        # kills: a regex that degrades on a hostile 200KB header
        import time
        t = time.time()
        x.parse_params('Payment id="' + "a" * 200000 + '", request="e30"')
        self.assertLess(time.time() - t, 1.0)
