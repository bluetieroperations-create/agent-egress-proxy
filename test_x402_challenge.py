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
