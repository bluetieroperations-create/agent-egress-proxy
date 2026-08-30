"""
Tests for asset_coverage.py -- the decimals-table coverage probe.

Each test states the mutation it kills. The network is injected everywhere, so
nothing here touches a live host.
"""
import json
import unittest

import asset_coverage as AC

JPYC = "0x431D5dfF03120AFA4bDf332c61A6e1766eF37BDB"     # Polygon, 18 dp, yen
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # 6 dp
NEW = "0x" + "e" * 40                                    # not in any table


def _row(host="h", network="eip155:8453", asset=USDC_BASE, amount="1000"):
    return {"host": host, "network": network, "asset": asset, "amount": amount}


def _resolver(table):
    """A stand-in for payload_sim.known_decimals over a {(net, asset): dp} map."""
    return lambda net, asset: table.get(AC.asset_key(net, asset))


class TestAssetKey(unittest.TestCase):
    def test_both_halves_are_lowercased(self):
        # Kills: normalizing only the asset. A live 402 returns an EIP-55
        # CHECKSUMMED address and CAIP-2 casing varies; a key that disagrees with
        # payload_sim's would report covered assets as missing.
        self.assertEqual(AC.asset_key("EIP155:8453", USDC_BASE),
                         ("eip155:8453", USDC_BASE.lower()))

    def test_whitespace_is_stripped(self):
        # Kills: keying on the raw string, so " eip155:8453" is a second asset.
        self.assertEqual(AC.asset_key(" eip155:8453 ", " %s " % USDC_BASE),
                         AC.asset_key("eip155:8453", USDC_BASE))

    def test_matches_payload_sims_normalization(self):
        # Kills: the two drifting apart. This is the whole contract: a pair this
        # module calls unresolved must be one the ENGINE cannot resolve.
        import payload_sim as PS
        for net, asset in PS.KNOWN_DECIMALS_BY_CHAIN:
            self.assertEqual(AC.asset_key(net, asset), (net, asset))

    def test_junk_is_dropped_not_keyed(self):
        # Kills: keying non-strings, which would make a malformed challenge look
        # like a missing table entry and send someone to fix the wrong thing.
        for net, asset in ((None, USDC_BASE), ("eip155:1", None), ("", USDC_BASE),
                           ("eip155:1", ""), (123, USDC_BASE), ({}, [])):
            self.assertIsNone(AC.asset_key(net, asset))


class TestPairsFromAccepts(unittest.TestCase):
    def test_the_same_asset_from_two_hosts_is_one_pair(self):
        # Kills: counting rows instead of distinct pairs -- coverage would then
        # track how chatty a host is rather than how many assets we can scale.
        pairs = AC.pairs_from_accepts([_row(host="a"), _row(host="b")])
        self.assertEqual(len(pairs), 1)
        self.assertEqual(sorted(next(iter(pairs.values()))["hosts"]), ["a", "b"])

    def test_the_same_address_on_two_chains_is_two_pairs(self):
        # Kills: keying on the address alone -- the exact hazard the chain-keyed
        # decimals table exists for (JPYC is 18 where its neighbours are 6).
        pairs = AC.pairs_from_accepts([_row(network="eip155:137"),
                                       _row(network="eip155:8453")])
        self.assertEqual(len(pairs), 2)

    def test_rows_without_an_asset_are_dropped(self):
        # Kills: treating a malformed challenge as an unresolved asset.
        self.assertEqual(AC.pairs_from_accepts([{"host": "a"}, _row()]).__len__(), 1)

    def test_empty_amounts_are_not_collected(self):
        # Kills: counting a missing quote as a quote, which inflates the priced
        # total and hides that a host advertises no price.
        pairs = AC.pairs_from_accepts([_row(amount=None), _row(amount="")])
        self.assertEqual(next(iter(pairs.values()))["amounts"], [])

    def test_junk_input_does_not_raise(self):
        # Kills: assuming the harvest is well-formed. It comes from the network.
        AC.pairs_from_accepts(None)
        AC.pairs_from_accepts(["x", 1, None, {}])


class TestImpliedPrice(unittest.TestCase):
    def test_atomic_quote_is_scaled_down(self):
        # Kills: reading the atomic amount as a price -- 1000 units of a 6-dp
        # token is $0.001, not $1000.
        self.assertEqual(AC.implied_price("1000", 6), AC.Decimal("0.001"))

    def test_eighteen_decimals(self):
        # Kills: a hardcoded 6. This is the JPYC case that started the audit.
        self.assertEqual(AC.implied_price("1000000000000000000", 18), AC.Decimal(1))

    def test_a_human_quote_is_not_scaled_again(self):
        # Kills: dividing a human-unit quote by 10^decimals. One live seller
        # advertises "0.003375"; scaling it again gives 3.375e-11 and a bogus
        # "implausible" finding.
        self.assertEqual(AC.implied_price("0.003375", 8), AC.Decimal("0.003375"))

    def test_unknown_decimals_yield_no_price(self):
        # Kills: defaulting to 6 for an asset we cannot scale, which is exactly
        # the defect the decimals work removed from the engine.
        self.assertIsNone(AC.implied_price("1000", None))

    def test_an_exponent_quote_is_refused(self):
        # Kills: re-deriving the atomic-vs-human rule here instead of reusing
        # upto_scheme.parse_ceiling, where refusing "1e6" is security relevant.
        self.assertIsNone(AC.implied_price("1e6", 6))


class TestAssess(unittest.TestCase):
    def test_an_unknown_asset_is_reported_with_its_hosts(self):
        # Kills: reporting a count without the hosts. The hosts are what makes
        # the finding actionable -- they name who introduced the asset.
        pairs = AC.pairs_from_accepts([_row(host="seller.example", asset=NEW)])
        report = AC.assess(pairs, _resolver({}))
        self.assertEqual(report["known_pairs"], 0)
        self.assertEqual(report["unresolved"][0]["hosts"], ["seller.example"])

    def test_a_known_asset_is_not_reported(self):
        # Kills: listing everything, which buries the work item.
        pairs = AC.pairs_from_accepts([_row()])
        report = AC.assess(pairs, _resolver({("eip155:8453", USDC_BASE.lower()): 6}))
        self.assertEqual(report["unresolved"], [])
        self.assertEqual(report["known_pairs"], 1)

    def test_a_wrong_scale_surfaces_as_an_implausible_price(self):
        # Kills: dropping the price check. This is the arm that catches a table
        # entry that is WRONG rather than missing -- coverage alone cannot.
        pairs = AC.pairs_from_accepts([_row(amount="1000000000000000000")])
        report = AC.assess(pairs, _resolver({("eip155:8453", USDC_BASE.lower()): 6}))
        self.assertEqual(len(report["implausible"]), 1)

    def test_a_sane_price_is_not_flagged(self):
        # Kills: a band so tight that ordinary micro-payments trip it.
        pairs = AC.pairs_from_accepts([_row(amount="1000"), _row(amount="29000000")])
        report = AC.assess(pairs, _resolver({("eip155:8453", USDC_BASE.lower()): 6}))
        self.assertEqual(report["implausible"], [])

    def test_a_non_usd_asset_is_reported_separately_not_flagged(self):
        # Kills: running a yen quote through a dollar band. 1 JPYC is about
        # two-thirds of a US cent; judging it as USD invents a finding, and
        # keeping an exchange rate in the repo would be stale on arrival.
        pairs = AC.pairs_from_accepts(
            [_row(network="eip155:137", asset=JPYC, amount="1000000000000000000")])
        report = AC.assess(pairs, _resolver({("eip155:137", JPYC.lower()): 18}))
        self.assertEqual(report["implausible"], [])
        self.assertEqual(len(report["non_usd"]), 1)

    def test_the_resolver_is_what_decides(self):
        # Kills: consulting the table directly instead of the injected resolver.
        # Production passes payload_sim.known_decimals so the report reflects the
        # ENGINE's answer; a private copy could disagree with the thing it audits.
        seen = []
        AC.assess(AC.pairs_from_accepts([_row()]),
                  lambda net, asset: seen.append((net, asset)) or 6)
        self.assertEqual(seen, [("eip155:8453", USDC_BASE)])


class TestCoverageAndSignal(unittest.TestCase):
    def test_percentage(self):
        # Kills: an off-by-one in the ratio.
        self.assertEqual(AC.coverage_pct({"total_pairs": 4, "known_pairs": 3}), 75.0)

    def test_an_empty_probe_is_zero_not_perfect(self):
        # Kills: returning 100% (or raising) when the harvest found nothing --
        # a failed probe must never read as full coverage on a dashboard.
        self.assertEqual(AC.coverage_pct({"total_pairs": 0, "known_pairs": 0}), 0.0)

    def test_needs_attention_on_an_unresolved_asset(self):
        # Kills: signalling only on price anomalies, so a NEW asset -- the common
        # case and the reason this runs -- passes silently.
        self.assertTrue(AC.needs_attention({"unresolved": [{}], "implausible": []}))

    def test_needs_attention_on_an_implausible_price(self):
        # Kills: signalling only on coverage, so a WRONG entry passes silently.
        self.assertTrue(AC.needs_attention({"unresolved": [], "implausible": [{}]}))

    def test_quiet_when_there_is_nothing_to_do(self):
        # Kills: always signalling, which trains a reader to ignore the signal.
        self.assertFalse(AC.needs_attention({"unresolved": [], "implausible": []}))


class TestFormatReport(unittest.TestCase):
    def test_an_unresolved_asset_appears_in_the_text(self):
        # Kills: a summary line that hides the work item.
        pairs = AC.pairs_from_accepts([_row(asset=NEW, host="seller.example")])
        text = AC.format_report(AC.assess(pairs, _resolver({})))
        self.assertIn(NEW.lower(), text.lower())
        self.assertIn("seller.example", text)

    def test_a_clean_run_says_so(self):
        # Kills: printing an empty report, which is indistinguishable from a
        # crash for anyone reading a scheduled run's output.
        pairs = AC.pairs_from_accepts([_row()])
        text = AC.format_report(AC.assess(pairs, _resolver({("eip155:8453", USDC_BASE.lower()): 6})))
        self.assertIn("nothing to do", text)

    def test_the_report_is_json_serializable(self):
        # Kills: leaving `hosts` as a set, which breaks --json at the last step,
        # after the probe has already spent its network budget.
        pairs = AC.pairs_from_accepts([_row(asset=NEW)])
        json.dumps(AC.assess(pairs, _resolver({})))


class TestHarvest(unittest.TestCase):
    def test_accepts_are_flattened_with_their_host(self):
        # Kills: losing the host, without which an unresolved asset cannot be
        # traced back to the seller that introduced it.
        rows = AC.harvest([("a.example", "u1")],
                          fetch=lambda url: [{"network": "eip155:8453",
                                              "asset": USDC_BASE, "amount": "5"}])
        self.assertEqual(rows[0]["host"], "a.example")

    def test_the_v2_amount_field_is_read(self):
        # Kills: reading only maxAmountRequired -- the v1 spelling. Live sellers
        # use `amount` 69 to 4; reading the wrong one silently prices nothing.
        rows = AC.harvest([("a", "u")],
                          fetch=lambda url: [{"network": "eip155:8453",
                                              "asset": USDC_BASE, "amount": "7"}])
        self.assertEqual(rows[0]["amount"], "7")

    def test_the_v1_amount_field_still_works(self):
        # Kills: swapping one spelling for the other rather than trying both.
        rows = AC.harvest([("a", "u")],
                          fetch=lambda url: [{"network": "eip155:8453",
                                              "asset": USDC_BASE,
                                              "maxAmountRequired": "9"}])
        self.assertEqual(rows[0]["amount"], "9")

    def test_a_host_that_returns_nothing_is_skipped_not_fatal(self):
        # Kills: letting one dead host end the run. A probe over 195 hosts that
        # dies on the first failure never reports anything.
        rows = AC.harvest([("dead", "u1"), ("live", "u2")],
                          fetch=lambda url: [] if url == "u1" else
                          [{"network": "eip155:8453", "asset": USDC_BASE, "amount": "5"}])
        self.assertEqual([r["host"] for r in rows], ["live"])

    def test_non_dict_accepts_entries_are_ignored(self):
        # Kills: trusting the shape of attacker-authored challenge content.
        rows = AC.harvest([("a", "u")], fetch=lambda url: ["nope", None, 3])
        self.assertEqual(rows, [])


class TestTargetsFromDirectory(unittest.TestCase):
    def test_reads_the_liveness_shape(self):
        # Kills: expecting a different artifact than the one the repo writes.
        self.assertEqual(
            AC.targets_from_directory([{"host": "h", "url": "https://h/x"}]),
            [("h", "https://h/x")])

    def test_rows_without_a_url_are_skipped(self):
        # Kills: emitting a target with no URL, which becomes a wasted request.
        self.assertEqual(AC.targets_from_directory([{"host": "h"}, None, "x"]), [])


if __name__ == "__main__":
    unittest.main()


class TestMalformedIsNotUnresolved(unittest.TestCase):
    """FOUND ON THE FIRST LIVE RUN: a seller advertises
    `0x8AC76a51cc950d9822D68b83fE43AD4843bA77E` -- 39 hex characters, not 40.
    Reporting that as "unresolved" sends someone to look up decimals for a
    contract that cannot exist. A broken counterparty and a missing table entry
    are different work and must not share a list.
    """

    TRUNCATED = "0x8AC76a51cc950d9822D68b83fE43AD4843bA77E"   # 39, not 40

    def test_the_real_truncated_address_is_malformed(self):
        # Kills: dropping the length/format check, which puts a nonexistent
        # contract on the decimals work list.
        self.assertTrue(AC.is_malformed_asset(self.TRUNCATED))

    def test_a_valid_address_is_not(self):
        # Kills: a check so strict it condemns real tokens.
        self.assertFalse(AC.is_malformed_asset(USDC_BASE))

    def test_non_evm_identifiers_are_not_judged(self):
        # Kills: running is_evm_address over Solana/Stellar/Algorand ids, which
        # would condemn every non-EVM asset in the corpus as broken.
        for asset in ("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "31566704",
                      "USDC:GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"):
            self.assertFalse(AC.is_malformed_asset(asset), asset)

    def test_it_lands_in_malformed_not_unresolved(self):
        # Kills: keeping one list. The two lists mean different actions --
        # report a seller bug vs resolve a scale.
        pairs = AC.pairs_from_accepts(
            [_row(network="eip155:56", asset=self.TRUNCATED, host="seller.example")])
        report = AC.assess(pairs, _resolver({}))
        self.assertEqual(report["unresolved"], [])
        self.assertEqual(len(report["malformed"]), 1)
        self.assertEqual(report["malformed"][0]["hosts"], ["seller.example"])

    def test_a_malformed_asset_still_needs_attention(self):
        # Kills: filing it away silently. It is still a finding -- just a
        # different one.
        self.assertTrue(AC.needs_attention(
            {"unresolved": [], "implausible": [], "malformed": [{}]}))

    def test_its_quotes_are_still_counted(self):
        # Kills: dropping the quotes from the total, which would make coverage
        # look better by hiding the broken ones.
        pairs = AC.pairs_from_accepts([_row(asset=self.TRUNCATED)])
        self.assertEqual(AC.assess(pairs, _resolver({}))["total_quotes"], 1)

    def test_the_text_report_names_it_a_seller_bug(self):
        # Kills: printing it under the decimals heading, which is the mistake
        # this whole class exists to prevent.
        pairs = AC.pairs_from_accepts([_row(asset=self.TRUNCATED)])
        text = AC.format_report(AC.assess(pairs, _resolver({})))
        self.assertIn("MALFORMED", text)
        self.assertNotIn("UNRESOLVED", text)


class TestMalformedPayee(unittest.TestCase):
    """FOUND ON THE FIRST LIVE RUN, on the same seller as the truncated asset:
    a Solana payTo of `2DgEL95L8Dta...WYcpFACILITATOR_URL=https://x402.org/
    facilitator` -- a real address with an environment variable concatenated
    onto it. That matters more than a decimals gap: it is the address an agent
    would PAY.
    """

    GLUED = ("2DgEL95L8DtaRb4ubYqrrnMbX7Zxgjxq7k8Ed9XAWYcp"
             "FACILITATOR_URL=https://x402.org/facilitator")
    CLEAN_SOLANA = "2DgEL95L8DtaRb4ubYqrrnMbX7Zxgjxq7k8Ed9XAWYcp"

    def test_an_env_var_glued_onto_an_address_is_malformed(self):
        # Kills: judging only 0x-prefixed values. This is base58, so the EVM
        # check never fires and the real signal is the `=` and the URL.
        self.assertTrue(AC.is_malformed_identifier(self.GLUED))

    def test_the_clean_solana_address_is_not_condemned(self):
        # Kills: rejecting anything non-EVM. There is no cheap base58 validity
        # test, so a clean unknown identifier must pass.
        self.assertFalse(AC.is_malformed_identifier(self.CLEAN_SOLANA))

    def test_whitespace_and_urls_are_rejected_on_any_chain(self):
        # Kills: dropping the content check that catches this class without
        # knowing the chain's address format.
        for bad in ("0x1234 5678", "https://evil.example", "a=b", "addr\tid",
                    "addr\nid", "   ", ""):
            self.assertTrue(AC.is_malformed_identifier(bad), repr(bad))

    def test_a_bad_payee_is_reported_with_its_host(self):
        # Kills: checking only the asset. A payment to a nonexistent address is
        # a worse outcome than an unscaled amount.
        rows = [dict(_row(host="seller.example"), pay_to=self.GLUED)]
        report = AC.assess(AC.pairs_from_accepts(rows),
                           _resolver({("eip155:8453", USDC_BASE.lower()): 6}))
        self.assertEqual(len(report["malformed_payees"]), 1)
        self.assertEqual(report["malformed_payees"][0]["hosts"], ["seller.example"])

    def test_a_good_payee_is_not_reported(self):
        # Kills: flagging every payee, which buries the real one.
        rows = [dict(_row(), pay_to="0x" + "a" * 40)]
        report = AC.assess(AC.pairs_from_accepts(rows),
                           _resolver({("eip155:8453", USDC_BASE.lower()): 6}))
        self.assertEqual(report["malformed_payees"], [])

    def test_a_bad_payee_alone_still_needs_attention(self):
        # Kills: signalling only on asset problems, so a broken payee -- the
        # more serious finding -- passes silently.
        self.assertTrue(AC.needs_attention({"malformed_payees": [{}]}))

    def test_it_is_reported_under_its_own_heading(self):
        # Kills: filing a payee problem under the decimals heading.
        rows = [dict(_row(), pay_to=self.GLUED)]
        text = AC.format_report(AC.assess(AC.pairs_from_accepts(rows),
                                _resolver({("eip155:8453", USDC_BASE.lower()): 6})))
        self.assertIn("MALFORMED PAYEE", text)

    def test_a_bad_payee_names_only_the_host_that_advertises_it(self):
        # Kills: attaching the pair's whole host set. Two sellers quoting the
        # same asset are not both responsible for one of them having a broken
        # payee -- and the report is what someone acts on.
        rows = [dict(_row(host="innocent.example"), pay_to="0x" + "a" * 40),
                dict(_row(host="broken.example"), pay_to=self.GLUED)]
        report = AC.assess(AC.pairs_from_accepts(rows),
                           _resolver({("eip155:8453", USDC_BASE.lower()): 6}))
        self.assertEqual(report["malformed_payees"][0]["hosts"], ["broken.example"])
