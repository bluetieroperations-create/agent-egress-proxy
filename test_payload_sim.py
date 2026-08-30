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


class TestRequestSuppliedDecimalsCannotOverrideKnownAsset(unittest.TestCase):
    """The request must not be able to re-scale the atomic comparison.

    AUDIT FINDING (2026-08-29, HIGH). `blackwall.forecast` reads
    `payload.get("decimals")` from the UNTRUSTED request body and
    `resolve_decimals` let that value win unconditionally -- before the
    known-token table was consulted. So a caller could re-scale the very
    comparison that gives payload-sim its STOP authority.

    Reproduced end-to-end through `forecast`: a signed authorization for 10^12
    atomic USDC (1,000,000 USDC) against a claim of "1.0 USDC" is a hard STOP
    honestly, and became verdict=HOLD / hard_stop=False with `"decimals": 12`
    added to the request. amount_status even read "verified".

    This is the ORIGINAL defect relocated: it moved from a hardcoded 6 to
    attacker control, which is strictly worse because it is steerable.

    Mutation: let the caller value win for a known asset -> these FAIL.
    """
    USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

    def _claim(self):
        return {"counterparty": "0x" + "11" * 20, "amount": "1.0",
                "asset": self.USDC, "chain": "base"}

    def _payment(self, value):
        return {"payload": {"authorization": {
            "from": "0x" + "22" * 20, "to": "0x" + "11" * 20, "value": str(value),
            "validAfter": "0", "validBefore": "99999999999",
            "nonce": "0x" + "33" * 32}, "signature": "0x" + "44" * 65}}

    def test_known_asset_ignores_request_decimals(self):
        # USDC is 6dp in the table; the request claiming 12 must not re-scale it.
        self.assertEqual(PS.resolve_decimals(self._claim(), 12), 6)

    def test_overpayment_still_caught_with_hostile_decimals(self):
        r = PS.check_payment_authorization(
            self._claim(), self._payment(10 ** 12), decimals=12, now=1,
            verify_signer=False)
        self.assertTrue(r["mismatches"],
                        "1,000,000 USDC vs a 1.0 USDC claim must not verify")

    def test_contradicting_a_known_asset_is_itself_a_mismatch(self):
        # Disagreeing with an asset we KNOW is an attack indicator, not a hint.
        r = PS.check_payment_authorization(
            self._claim(), self._payment(10 ** 6), decimals=12, now=1,
            verify_signer=False)
        self.assertTrue(any("decimals" in m.lower() for m in r["mismatches"]),
                        "contradicting the known asset should be reported")

    def test_caller_asserted_scale_is_not_called_verified(self):
        """An amount checked at a scale the SCREENED PARTY supplied is not verified.

        AUDIT (2026-08-29, MEDIUM). For an asset we cannot identify, the caller
        value is legitimately the only source -- but the comparison is then only
        as good as that assertion, and `amount_status` still read "verified" with
        no warning. That is the same category error as the original bug: claiming
        a check we did not perform. The module already has the right pattern for
        this (`signer_status="deferred"`, `amount_status="unverified_decimals"`).

        Mutation: report "verified" for a caller-supplied scale -> this FAILS.
        """
        unknown = {"counterparty": "0x" + "11" * 20, "amount": "1.0",
                   "asset": "0x" + "ab" * 20, "chain": "base"}
        r = PS.check_payment_authorization(
            unknown, self._payment(10 ** 18), decimals=18, now=1,
            verify_signer=False)
        self.assertFalse(r["mismatches"])          # arithmetic still done
        self.assertEqual(r["amount_status"], "asserted_decimals")
        self.assertTrue(any("asserted" in w.lower() or "not verified" in w.lower()
                            for w in r["warnings"]),
                        "the unverified scale must be surfaced")

    def test_known_asset_still_reports_verified(self):
        """The good case must not be downgraded -- a scale we established
        ourselves is genuinely verified. Mutation: report asserted_decimals for a
        known asset -> this FAILS."""
        r = PS.check_payment_authorization(
            self._claim(), self._payment(10 ** 6), now=1, verify_signer=False)
        self.assertEqual(r["amount_status"], "verified")

    def test_onchain_lookup_receives_a_chain_it_can_route_on(self):
        """The chain must reach the resolver in the form it understands.

        MERGE BUG (2026-08-29). The cold-start branch routes the on-chain
        `decimals()` read by chain -- a real fix, since the same address is a
        different token on a different network and a wrong answer is cached
        FOREVER. Its `chain_of()` accepts CAIP-2 ("eip155:8453") or a bare id.
        But every claim in this codebase carries a HUMAN name ("base",
        "ethereum"), for which chain_of returns None -- so passing
        claim["chain"] straight through left the routing inert on every real
        request while looking wired up. Exactly the silent-loss failure the
        handoff warned about, arriving through a different door.

        Mutation: pass claim["chain"] unnormalised -> this FAILS (network=None).
        """
        seen = {}

        class Resolver:
            def lookup(self, asset, network=None):
                seen["network"] = network
                return 18

        PS.set_onchain_resolver(Resolver())
        try:
            PS.resolve_decimals({"asset": "0x" + "cd" * 20, "chain": "base"})
        finally:
            PS.set_onchain_resolver(None)
        self.assertEqual(seen.get("network"), "eip155:8453",
                         "the resolver must get a chain it can route on")

    def test_bool_is_not_a_decimals_value(self):
        # bool is an int subclass, so `"decimals": true` silently became 1.
        self.assertIsNone(PS.resolve_decimals({"asset": "0x" + "ab" * 20}, True))
        self.assertIsNone(PS.resolve_decimals({"asset": "0x" + "ab" * 20}, False))

    def test_request_decimals_still_used_for_an_UNKNOWN_asset(self):
        # The legitimate case the parameter exists for (x402 v2 methodDetails):
        # an asset we have no table entry for.
        unknown = {"counterparty": "0x" + "11" * 20, "amount": "1.0",
                   "asset": "0x" + "ab" * 20, "chain": "base"}
        self.assertEqual(PS.resolve_decimals(unknown, 18), 18)


class TestDecimalsAudit(unittest.TestCase):
    """Audit 2026-08-27: decimals were hardcoded to 6 everywhere, so the atomic
    comparison mis-scaled by 10^(d-6) for any non-6-decimal asset."""

    DAI = "0x6B175474E89094C44Da98b954EedeAC495271d0F"     # 18
    USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"    # 6
    PAYER = "0x" + "1" * 40
    PAYEE = "0x" + "2" * 40

    def _xpay(self, atomic, asset):
        import base64, json
        return base64.b64encode(json.dumps({
            "x402Version": 1, "scheme": "exact", "network": "eip155:1",
            "payload": {"authorization": {
                "from": self.PAYER, "to": self.PAYEE, "value": str(atomic),
                "validAfter": "0", "validBefore": "99999999999",
                "nonce": "0x" + "0" * 64},
                "signature": "0x" + "0" * 130, "asset": asset}}).encode()).decode()

    def _claim(self, asset, amount="1.0"):
        return {"counterparty": self.PAYEE, "amount": amount,
                "asset": asset, "chain": "eip155:1"}

    def test_underpayment_bypass_is_caught(self):
        # kills: the hardcoded 6. A signed 10^6 atomic DAI (0.000000000001 DAI)
        # MATCHED a claim of 1.0 DAI -- the STOP authority's guarantee, void.
        r = PS.check_payment_authorization(
            self._claim(self.DAI), self._xpay(10 ** 6, self.DAI), verify_signer=False)
        self.assertFalse(r["matches"])
        self.assertTrue(r["mismatches"])

    def test_valid_18_decimal_payment_is_not_false_stopped(self):
        # kills: the mirror failure -- a correct 1.0 DAI payment reported as a
        # mismatch and hard-STOPped, blocking legitimate business
        r = PS.check_payment_authorization(
            self._claim(self.DAI), self._xpay(10 ** 18, self.DAI), verify_signer=False)
        self.assertTrue(r["matches"])
        self.assertEqual(r["amount_status"], "verified")

    def test_usdc_behaviour_unchanged(self):
        # kills: a fix that regresses the overwhelmingly common 6-decimal case
        r = PS.check_payment_authorization(
            self._claim(self.USDC), self._xpay(10 ** 6, self.USDC), verify_signer=False)
        self.assertTrue(r["matches"])
        self.assertEqual(r["amount_status"], "verified")

    def test_unknown_asset_is_reported_not_assumed(self):
        # kills: silently assuming 6 for an unrecognised asset, which is exactly
        # how the bypass arose. The caller must be able to see it was unchecked.
        r = PS.check_payment_authorization(
            self._claim("0x" + "9" * 40), self._xpay(10 ** 6, "0x" + "9" * 40),
            verify_signer=False)
        self.assertEqual(r["amount_status"], "unverified_decimals")
        self.assertTrue(any("NOT verified" in w for w in r["warnings"]))

    def test_explicit_decimals_used_for_an_asset_we_cannot_identify(self):
        # kills: ignoring a caller-supplied value, e.g. from the v2 challenge's
        # methodDetails.decimals.
        #
        # REVISED (audit 2026-08-29): this asserted that a caller value wins over
        # the table, demonstrated on USDC -- a token we KNOW is 6dp. That is the
        # capability shown on precisely the input where honouring it is unsafe:
        # forecast sources `decimals` from the untrusted request body, so it let a
        # request re-scale the atomic comparison and downgrade a hard STOP to HOLD
        # (see TestRequestSuppliedDecimalsCannotOverrideKnownAsset). The capability
        # is real and kept -- an 18dp asset with no table entry is exactly what the
        # parameter exists for -- so it is asserted here on an UNKNOWN asset, where
        # the caller is the only source of truth and cannot contradict us.
        unknown = "0x" + "ab" * 20
        r = PS.check_payment_authorization(
            self._claim(unknown), self._xpay(10 ** 18, unknown),
            decimals=18, verify_signer=False)
        self.assertTrue(r["matches"])
        # ...but reported as ASSERTED, not verified: for an asset we cannot
        # identify the scale comes from the caller, so the arithmetic is only as
        # good as that claim. See test_caller_asserted_scale_is_not_called_verified.
        self.assertEqual(r["amount_status"], "asserted_decimals")

    def test_symbol_claims_still_resolve(self):
        # kills: requiring a contract address, breaking callers that pass "USDC"
        r = PS.check_payment_authorization(
            self._claim("USDC"), self._xpay(10 ** 6, self.USDC), verify_signer=False)
        self.assertEqual(r["amount_status"], "verified")

    def test_nonsense_decimals_is_treated_as_unknown(self):
        # kills: trusting a hostile/garbage decimals value into the scaling math
        for bad in ("abc", -1, 999, None if False else 10 ** 9):
            r = PS.check_payment_authorization(
                self._claim("0x" + "9" * 40), self._xpay(1, "0x" + "9" * 40),
                decimals=bad, verify_signer=False)
            self.assertEqual(r["amount_status"], "unverified_decimals")


class TestPerChainDecimals(unittest.TestCase):
    """The (network, asset) table resolved on-chain from the live x402 corpus.

    Mutations these kill: dropping the chain-keyed lookup from `known_decimals`;
    consulting it AFTER the address-only table; keying it by address alone;
    matching the network case-sensitively; letting a caller override it.
    """

    JPYC = "0x431D5dfF03120AFA4bDf332c61A6e1766eF37BDB"   # Polygon, 18 dp
    EURC = "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42"   # Base, 6 dp

    def test_18_decimal_asset_resolves_from_its_chain(self):
        # Kills: removing the chain-keyed lookup (falls through to unknown ->
        # None), or reading this token at the corpus-wide default of 6.
        self.assertEqual(PS.known_decimals({"asset": self.JPYC, "chain": "eip155:137"}), 18)

    def test_human_chain_name_resolves_too(self):
        # Kills: keying the table on CAIP-2 only. Real claims say "polygon".
        self.assertEqual(PS.known_decimals({"asset": self.JPYC, "chain": "polygon"}), 18)

    def test_checksummed_and_lowercase_asset_agree(self):
        # Kills: storing the table checksummed. A live 402 returns EIP-55; our
        # crawl stores lowercase. The same join bug already cost 64 endpoints
        # once (see advertised_prices.py).
        self.assertEqual(PS.known_decimals({"asset": self.JPYC.lower(), "chain": "polygon"}),
                         PS.known_decimals({"asset": self.JPYC, "chain": "polygon"}))

    def test_network_matching_is_case_insensitive(self):
        # Kills: comparing the network verbatim, so "Polygon" misses.
        self.assertEqual(PS.known_decimals({"asset": self.JPYC, "chain": "POLYGON"}), 18)

    def test_no_chain_means_unknown_not_a_guess(self):
        # Kills: also adding these to the address-only KNOWN_DECIMALS. An address
        # alone does not determine a token -- answering here would be a guess.
        self.assertIsNone(PS.known_decimals({"asset": self.JPYC}))

    def test_right_address_wrong_chain_does_not_match(self):
        # Kills: ignoring the network half of the key. This is the whole reason
        # the table is chain-keyed.
        self.assertIsNone(PS.known_decimals({"asset": self.JPYC, "chain": "base"}))

    def test_caller_cannot_override_a_chain_resolved_asset(self):
        # Kills: letting request-supplied decimals win for assets that are known
        # only via the chain table -- the HIGH finding of 2026-08-29, which the
        # address-only table's coverage would otherwise have left reachable here.
        claim = {"asset": self.JPYC, "chain": "polygon"}
        self.assertEqual(PS.resolve_decimals(claim, 6), 18)
        self.assertTrue(PS.decimals_conflict(claim, 6))

    def test_non_evm_assets_resolve(self):
        # Kills: restricting the table to 0x addresses. Solana/Algorand/Stellar
        # ids are not hex and were the bulk of the unresolved corpus.
        self.assertEqual(PS.known_decimals(
            {"asset": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
             "chain": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"}), 6)
        self.assertEqual(PS.known_decimals(
            {"asset": "31566704",
             "chain": "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="}), 6)

    def test_stellar_is_seven_not_six(self):
        # Kills: folding Stellar into the 6-decimal majority. Stellar amounts are
        # stroops (1e-7); at 6 dp a 0.02 USDC quote reads as 0.2.
        self.assertEqual(PS.known_decimals(
            {"asset": "USDC:GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN",
             "chain": "stellar:pubnet"}), 7)

    def test_existing_flat_table_still_answers(self):
        # Kills: replacing the address-only table instead of layering over it.
        self.assertEqual(PS.known_decimals({"asset": USDC, "chain": "base"}), 6)
        self.assertEqual(PS.known_decimals({"asset": USDC}), 6)

    def test_table_keys_are_normalized(self):
        # Kills: a hand-edited entry that can never match because it was pasted
        # checksummed or with a mixed-case network.
        for net, asset in PS.KNOWN_DECIMALS_BY_CHAIN:
            self.assertEqual(net, net.lower(), net)
            self.assertEqual(asset, asset.lower(), asset)

    def test_non_evm_aliases_point_at_real_table_chains(self):
        # Kills: a non-EVM alias for a chain the table does not carry -- silently
        # inert. (EVM names go through x402.to_caip2, tested there.)
        chains = {net for net, _ in PS.KNOWN_DECIMALS_BY_CHAIN}
        for name, caip in PS._DECIMALS_NETWORKS.items():
            self.assertIn(caip, chains, "alias %r points at an unused chain" % name)

    def test_bare_solana_is_not_guessed(self):
        # Kills: aliasing "solana" to mainnet. It names neither cluster, and the
        # devnet and mainnet mints are different tokens.
        self.assertIsNone(PS.known_decimals(
            {"asset": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "chain": "solana"}))


class TestHumanChainNameIsNotANetworkMismatch(unittest.TestCase):
    """AUDIT 2026-08-29 (HIGH): a CORRECT payment was hard-STOPped because the
    claim spelled its chain "polygon" while the payment said "eip155:137", and
    `x402._CAIP2` knew only Base and Ethereum -- so `to_caip2` compared the two
    spellings of ONE chain and called it a network mismatch.
    """

    JPYC = "0x431D5dfF03120AFA4bDf332c61A6e1766eF37BDB"

    def _check(self, chain, network):
        payment = _payment(value="1000000000000000000", asset=self.JPYC, network=network)
        return PS.check_payment_authorization(
            _claim(amount="1.0", asset=self.JPYC, chain=chain), _b64(payment))

    def test_human_chain_name_matches_its_caip2_payment(self):
        # Kills: dropping the alias -- the reason list regains a network mismatch
        # and a correct payment becomes a hard STOP.
        result = self._check("polygon", "eip155:137")
        self.assertEqual(result["mismatches"], [])
        self.assertEqual(result["amount_status"], "verified")

    def test_a_genuinely_different_chain_still_mismatches(self):
        # Kills: making to_caip2 collapse everything to equal. The gate must
        # still fire when the payment really is on another chain.
        result = self._check("polygon", "eip155:8453")
        self.assertTrue(any("network" in m for m in result["mismatches"]), result)

    def test_alias_targets_are_well_formed(self):
        # Kills: a typo'd or duplicated alias silently mapping a chain name to
        # the wrong id -- which would make two DIFFERENT chains compare equal.
        import x402
        for name, caip in x402._CAIP2.items():
            self.assertEqual(name, name.lower(), name)
            self.assertRegex(caip, r"^eip155:[1-9][0-9]*$", name)
        # every id reachable by more than one name must be reached only by names
        # that are genuinely synonyms -- assert the count, so a paste error that
        # points a new name at an existing id is visible in the diff.
        self.assertEqual(len(set(x402._CAIP2.values())), 14)


class TestAmountThatCannotBeRepresented(unittest.TestCase):
    """AUDIT 2026-08-29 (MEDIUM): `to_atomic` returns None for a claim amount
    carrying more precision than the asset has, and the three reasons a
    comparison could not be made were collapsed into one message that always
    read as a comparison -- while `amount_status` still said "verified",
    asserting a check that never ran.
    """

    STELLAR = "USDC:GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"

    def _run(self, amount, value):
        payment = _payment(value=value, asset="stellar-token", network="stellar:pubnet")
        return PS.check_payment_authorization(
            {"counterparty": CP, "amount": amount, "asset": self.STELLAR,
             "chain": "stellar:pubnet"}, _b64(payment))

    def test_over_precise_amount_is_not_called_verified(self):
        # Kills: reporting amount_status "verified" when to_atomic returned None.
        # 8 decimal places on a 7-decimal asset: nothing was ever compared.
        result = self._run("0.00000001", "100000")
        self.assertEqual(result["amount_status"], "unrepresentable_amount")
        self.assertFalse(result["matches"])

    def test_the_reason_does_not_pretend_a_comparison_happened(self):
        # Kills: restoring the "value is X but you asked me to score Y" wording
        # for this case, which sends an operator hunting a mismatch that is
        # really an unrepresentable amount.
        reason = self._run("0.00000001", "100000")["mismatches"][0]
        self.assertIn("more precision than the asset has", reason)
        self.assertNotIn("but you asked me to score", reason)

    def test_a_representable_amount_still_compares_normally(self):
        # Kills: routing every amount down the unrepresentable branch.
        ok = self._run("0.01", "100000")
        self.assertEqual(ok["amount_status"], "verified")
        self.assertEqual([m for m in ok["mismatches"] if "amount" in m or "value" in m], [])

    def test_a_real_mismatch_still_reads_as_a_mismatch(self):
        # Kills: losing the genuine comparison message.
        bad = self._run("0.01", "999999")
        self.assertTrue(any("but you asked me to score" in m for m in bad["mismatches"]), bad)

    def test_unreadable_signed_value_is_its_own_case(self):
        # Kills: reporting a garbage signed value as an amount disagreement.
        payment = _payment(value="not-a-number", asset=USDC)
        r = PS.check_payment_authorization(_claim(), _b64(payment))
        self.assertEqual(r["amount_status"], "unreadable_payment_value")
        self.assertTrue(any("not a readable amount" in m for m in r["mismatches"]), r)


class TestKnownAssetOnAForeignChain(unittest.TestCase):
    """Pinning a DELIBERATE consequence of widening the chain aliases: a claim
    naming Base USDC but a non-Base chain can now build an EIP-712 domain (it
    previously could not, so signer verification degraded to a warning). The
    pair is incoherent -- that token exists on Base -- so recovery fails and it
    hard-stops. Pinned so the change stays intentional rather than incidental.
    """

    def test_base_usdc_claimed_on_polygon_fails_signer_recovery(self):
        # Kills: silently reverting to a warning here, which would let a claim
        # and a signature disagree about the chain without a stop.
        payment = _payment(value="90000", asset=USDC, network="eip155:137")
        r = PS.check_payment_authorization(_claim(chain="polygon"), _b64(payment))
        self.assertEqual(r["signer_status"], "mismatch")

    def test_base_usdc_on_base_is_unaffected(self):
        # Kills: breaking the ordinary path while pinning the foreign-chain one.
        payment = _payment(value="90000", asset=USDC, network="base")
        r = PS.check_payment_authorization(_claim(chain="base"), _b64(payment))
        self.assertNotIn("network", " ".join(r["mismatches"]))


class TestXdcUsdc(unittest.TestCase):
    """Added 2026-08-30 after `asset_coverage` surfaced it as unresolved on
    api.402rates.com -- the first asset the coverage probe found that the table
    did not already cover, which is the whole reason that probe exists.

    Resolved under the reviewed procedure in docs/DECIMALS_AUDIT.md: read
    `decimals()` from every public RPC chain 50 publishes. 7 of 7 answered, all
    returned 6, symbol USDC.
    """

    XDC_USDC = "0xfA2958CB79b0491CC627c1557F441eF849Ca8eb1"

    def test_it_resolves_on_its_own_chain(self):
        # Kills: dropping the entry, which returns this asset to "unverified" and
        # switches the amount check off for every XDC payment.
        self.assertEqual(PS.known_decimals(
            {"asset": self.XDC_USDC, "chain": "eip155:50"}), 6)

    def test_the_same_address_on_another_chain_does_not_match(self):
        # Kills: adding it to the address-only table. Chain 50 is not Base.
        self.assertIsNone(PS.known_decimals(
            {"asset": self.XDC_USDC, "chain": "eip155:8453"}))

    def test_a_caller_cannot_rescale_it(self):
        # Kills: letting request-supplied decimals win for a newly added asset --
        # the same HIGH finding the chain table was built to close.
        claim = {"asset": self.XDC_USDC, "chain": "eip155:50"}
        self.assertEqual(PS.resolve_decimals(claim, 18), 6)
        self.assertTrue(PS.decimals_conflict(claim, 18))
