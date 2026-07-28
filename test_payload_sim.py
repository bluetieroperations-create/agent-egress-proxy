"""
Tests for payload_sim.py -- Phase 1 payload simulation (signed-payment vs claim).

Each test states the mutation it kills. Payments are built in the x402 v2 wire
shape and base64-encoded exactly as an agent would send them.
"""
import base64
import json
import unittest

import payload_sim as PS

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CP = "0x" + "a" * 40          # counterparty being scored / paid
OTHER = "0x" + "d" * 40       # a DIFFERENT recipient (the attack)


def _payment(*, to=CP, value="90000", asset=USDC, network="base",
             nonce="0x" + "1" * 64, valid_before="9999999999",
             valid_after="0"):
    """An x402 v2 payment payload (as decoded from an X-PAYMENT header)."""
    return {
        "x402Version": 2,
        "accepted": {"scheme": "exact", "network": network, "asset": asset},
        "payload": {
            "signature": "0x" + "2" * 130,
            "authorization": {
                "from": "0x" + "b" * 40, "to": to, "value": value,
                "validAfter": valid_after, "validBefore": valid_before,
                "nonce": nonce,
            },
        },
    }


def _b64(payment):
    return base64.b64encode(json.dumps(payment).encode()).decode()


def _claim(*, counterparty=CP, amount="0.09", asset=USDC, chain="base"):
    return {"counterparty": counterparty, "amount": amount,
            "asset": asset, "chain": chain}


class TestMatch(unittest.TestCase):
    """Phase-1 field checks in isolation (verify_signer=False; the fixtures carry a
    placeholder signature -- Phase 2 is exercised separately with real signatures).

    Mutation note: if any field comparison is inverted/dropped, the matching
    happy-path below stops being clean OR an attack below stops tripping."""

    def test_matching_payment_passes(self):
        r = PS.check_payment_authorization(_claim(), _b64(_payment()),
                                           verify_signer=False)
        self.assertTrue(r["checked"])
        self.assertTrue(r["matches"])
        self.assertEqual(r["mismatches"], [])

    def test_accepts_predecoded_dict(self):
        r = PS.check_payment_authorization(_claim(), _payment(), verify_signer=False)
        self.assertTrue(r["matches"])

    def test_absent_payment_is_opt_in_not_a_failure(self):
        for empty in (None, ""):
            r = PS.check_payment_authorization(_claim(), empty)
            self.assertFalse(r["checked"])
            self.assertTrue(r["matches"])       # absence never blocks a verdict
            self.assertEqual(r["mismatches"], [])


class TestAttacksTripHardStop(unittest.TestCase):
    """
    Mutation notes (each attack must produce a mismatch):
      - drop the recipient check -> test_recipient_swap FAILS.
      - drop the amount check    -> test_amount_inflated FAILS.
      - compare amount in human units (skip to_atomic) -> test_amount_inflated FAILS.
      - drop the asset check     -> test_asset_swap FAILS.
      - drop the network check   -> test_chain_swap FAILS.
    """
    def test_recipient_swap(self):
        # score "pay CP", but the signed auth actually pays OTHER
        r = PS.check_payment_authorization(_claim(), _b64(_payment(to=OTHER)))
        self.assertFalse(r["matches"])
        self.assertTrue(any("recipient mismatch" in m for m in r["mismatches"]))

    def test_amount_inflated(self):
        # score $0.09 (90000 atomic) but sign 5_000_000 atomic ($5.00)
        r = PS.check_payment_authorization(_claim(amount="0.09"),
                                           _b64(_payment(value="5000000")))
        self.assertFalse(r["matches"])
        self.assertTrue(any("value" in m for m in r["mismatches"]))

    def test_asset_swap(self):
        r = PS.check_payment_authorization(
            _claim(asset=USDC), _b64(_payment(asset="0x" + "e" * 40)))
        self.assertFalse(r["matches"])
        self.assertTrue(any("asset" in m for m in r["mismatches"]))

    def test_chain_swap(self):
        r = PS.check_payment_authorization(
            _claim(chain="base"), _b64(_payment(network="base-sepolia")))
        self.assertFalse(r["matches"])
        self.assertTrue(any("network" in m or "chain" in m for m in r["mismatches"]))

    def test_missing_nonce(self):
        r = PS.check_payment_authorization(_claim(), _b64(_payment(nonce="")))
        self.assertFalse(r["matches"])
        self.assertTrue(any("nonce" in m for m in r["mismatches"]))


class TestRobustness(unittest.TestCase):
    """Mutation note: a crafted payment must never crash the gate (fail-closed to a
    mismatch, never an exception)."""

    def test_undecodable_is_mismatch(self):
        r = PS.check_payment_authorization(_claim(), "!!!not base64!!!")
        self.assertTrue(r["checked"])
        self.assertFalse(r["matches"])
        self.assertTrue(any("decode" in m for m in r["mismatches"]))

    def test_garbage_never_raises(self):
        for junk in ({}, {"payload": 1}, {"accepted": []}, {"payload": {"authorization": 7}}):
            r = PS.check_payment_authorization(_claim(), junk)
            self.assertFalse(r["matches"])   # no auth fields -> mismatches, no raise

    def test_caip2_network_equivalence_is_not_a_mismatch(self):
        # client sends CAIP-2 network, claim uses the bare name -> same chain
        r = PS.check_payment_authorization(_claim(chain="base"),
                                           _b64(_payment(network="eip155:8453")),
                                           verify_signer=False)
        self.assertTrue(r["matches"])

    def test_symbol_asset_claim_is_not_falsely_flagged(self):
        # claim.asset "USDC" (a symbol) can't be compared to a contract address;
        # must NOT produce a spurious asset mismatch (only real addr claims compare).
        # (symbol asset -> no trusted domain -> Phase 2 degrades to a warning)
        r = PS.check_payment_authorization(_claim(asset="USDC"), _b64(_payment()))
        self.assertTrue(r["matches"])
        self.assertFalse(any("asset" in m for m in r["mismatches"]))

    def test_case_insensitive_recipient(self):
        r = PS.check_payment_authorization(
            _claim(counterparty=CP.upper().replace("0X", "0x")),
            _b64(_payment(to=CP)), verify_signer=False)
        self.assertTrue(r["matches"])


class TestPhase2SignerRecovery(unittest.TestCase):
    """
    Phase 2: recover the EIP-3009 signer (real signatures, pure-Python secp256k1)
    and confirm it is the stated payer -- and, because the digest domain is built
    from the CLAIM, that recovery also binds the chain + asset.

    Mutation notes:
      - skip the signer check -> test_wrong_key_signature FAILS.
      - build the domain from the payment's metadata instead of the claim ->
        test_wrong_chain_signature / test_wrong_asset_signature FAIL.
      - treat a failed recovery as a pass -> test_forged_signature FAILS.
    """
    def _signed(self, pk, *, to=CP, value="90000", asset=USDC, network="base",
                chain_id=8453, name="USD Coin", version="2", frm=None,
                nonce="0x" + "1" * 64):
        import secp256k1 as S
        import eip712 as E
        signer = E.pubkey_to_address(S.privkey_to_pub(pk))
        message = {"from": frm or signer, "to": to, "value": value,
                   "validAfter": "0", "validBefore": "9999999999", "nonce": nonce}
        z = E.transfer_authorization_digest(
            {"name": name, "version": version, "chainId": chain_id,
             "verifyingContract": asset}, message)
        r, s, rec = S.ecdsa_sign(z, pk)
        sig = "0x" + (r.to_bytes(32, "big") + s.to_bytes(32, "big")
                      + bytes([27 + rec])).hex()
        payment = {"x402Version": 2,
                   "accepted": {"scheme": "exact", "network": network, "asset": asset},
                   "payload": {"signature": sig, "authorization": message}}
        return payment, signer

    def test_valid_signature_matches(self):
        payment, signer = self._signed(0xA11CE)
        r = PS.check_payment_authorization(
            {"counterparty": CP, "amount": "0.09", "asset": USDC, "chain": "base"},
            payment)
        self.assertTrue(r["matches"], r["mismatches"])
        self.assertEqual(r["warnings"], [])

    def test_wrong_key_signature(self):
        # `from` claims one address, but a DIFFERENT key actually signed
        victim = __import__("eip712").pubkey_to_address(
            __import__("secp256k1").privkey_to_pub(0xBEEF))
        payment, _ = self._signed(0xA11CE, frm=victim)   # signed by A11CE, from=victim
        r = PS.check_payment_authorization(
            {"counterparty": CP, "amount": "0.09", "asset": USDC, "chain": "base"},
            payment)
        self.assertFalse(r["matches"])
        self.assertTrue(any("signer" in m for m in r["mismatches"]))

    def test_forged_signature(self):
        payment, _ = self._signed(0xA11CE)
        # corrupt the signature -> recovery yields a different / no signer
        payment["payload"]["signature"] = "0x" + "3" * 130
        r = PS.check_payment_authorization(
            {"counterparty": CP, "amount": "0.09", "asset": USDC, "chain": "base"},
            payment)
        self.assertFalse(r["matches"])

    def test_wrong_chain_signature(self):
        # signature made for base-sepolia (84532), but the claim says base (8453).
        # Domain built from the CLAIM -> recovery over 8453 != signer -> STOP.
        payment, _ = self._signed(0xA11CE, chain_id=84532, network="base-sepolia")
        r = PS.check_payment_authorization(
            {"counterparty": CP, "amount": "0.09", "asset": USDC, "chain": "base"},
            payment)
        self.assertFalse(r["matches"])   # chain bound THROUGH the signature

    def test_wrong_asset_signature(self):
        # signature made over a different verifyingContract than the claimed asset
        other = "0x" + "c" * 40
        payment, _ = self._signed(0xA11CE, asset=other)
        r = PS.check_payment_authorization(
            {"counterparty": CP, "amount": "0.09", "asset": USDC, "chain": "base"},
            payment)
        self.assertFalse(r["matches"])   # asset bound THROUGH the signature

    def test_unknown_asset_degrades_to_warning(self):
        # a claim asset with no trusted EIP-712 domain -> can't recover -> warning,
        # not a hard stop (Phase-1 recipient+amount still bind).
        other = "0x" + "c" * 40
        payment, _ = self._signed(0xA11CE, asset=other)
        r = PS.check_payment_authorization(
            {"counterparty": CP, "amount": "0.09", "asset": other, "chain": "base"},
            payment)
        self.assertTrue(r["matches"])
        self.assertTrue(any("signer not verified" in w for w in r["warnings"]))

    def test_missing_signature_is_warning(self):
        payment, _ = self._signed(0xA11CE)
        del payment["payload"]["signature"]
        r = PS.check_payment_authorization(
            {"counterparty": CP, "amount": "0.09", "asset": USDC, "chain": "base"},
            payment)
        self.assertTrue(r["matches"])
        self.assertTrue(any("no signature" in w for w in r["warnings"]))


class TestTimeValidityAdvisory(unittest.TestCase):
    """Mutation note: time issues must be WARNINGS, not hard-stop mismatches."""

    def test_expired_is_warning_not_mismatch(self):
        r = PS.check_payment_authorization(
            _claim(), _b64(_payment(valid_before="1000")), now=2000,
            verify_signer=False)
        self.assertTrue(r["matches"])                 # not a hard stop
        self.assertTrue(any("expired" in w for w in r["warnings"]))

    def test_not_yet_valid_is_warning(self):
        r = PS.check_payment_authorization(
            _claim(), _b64(_payment(valid_after="5000")), now=2000,
            verify_signer=False)
        self.assertTrue(r["matches"])
        self.assertTrue(any("not yet valid" in w for w in r["warnings"]))

    def test_no_now_skips_time_checks(self):
        r = PS.check_payment_authorization(
            _claim(), _b64(_payment(valid_before="1000")),  # no `now`
            verify_signer=False)
        self.assertEqual(r["warnings"], [])


if __name__ == "__main__":
    unittest.main()
