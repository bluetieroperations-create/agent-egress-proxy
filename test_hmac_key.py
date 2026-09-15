"""
Tests for hmac_key.py -- the ONE owner of the HMAC capability secret.

Each test names the mutation it kills.
"""
import os
import subprocess
import sys
import unittest

import hmac_key


class LoadKey(unittest.TestCase):
    def setUp(self):
        hmac_key.reset_ephemeral_for_tests()

    def test_an_explicit_secret_is_used_verbatim(self):
        key, ephemeral = hmac_key.load_key({"BLACKWALL_RECEIPT_KEY": "operator-secret"})
        self.assertEqual(key, b"operator-secret")
        self.assertFalse(ephemeral)

    def test_UNSET_yields_a_RANDOM_key_not_a_committed_one(self):
        # THE WHOLE POINT. MUTATION: any constant fallback. A committed secret
        # means every capability token in this engine -- report tokens, approval
        # decide/redeem tokens, seller revoke tokens -- is forgeable by anyone
        # who can read the public repo.
        key, ephemeral = hmac_key.load_key({})
        self.assertTrue(ephemeral)
        self.assertEqual(len(key), 32)
        self.assertNotIn(b"dev", key.lower())
        self.assertNotIn(b"insecure", key.lower())

    def test_the_ephemeral_key_is_STABLE_within_a_process(self):
        # MUTATION: generating a fresh key per call. Then a token could not be
        # verified even by the request that issued it -- the feature would be
        # broken rather than merely non-durable.
        a, _ = hmac_key.load_key({})
        b, _ = hmac_key.load_key({})
        self.assertEqual(a, b)

    def test_an_explicit_secret_WINS_over_an_already_generated_ephemeral(self):
        # MUTATION: caching the ephemeral key and returning it regardless. An
        # operator who sets the variable must get their own secret, or a
        # restart-order accident silently keeps the random one.
        hmac_key.load_key({})
        key, ephemeral = hmac_key.load_key({"BLACKWALL_RECEIPT_KEY": "real"})
        self.assertEqual(key, b"real")
        self.assertFalse(ephemeral)

    def test_whitespace_only_is_treated_as_UNSET(self):
        # MUTATION: accepting "   " as a secret. A blank dashboard field is the
        # most likely way this is misconfigured, and a whitespace HMAC key is
        # low-entropy and guessable.
        key, ephemeral = hmac_key.load_key({"BLACKWALL_RECEIPT_KEY": "   "})
        self.assertTrue(ephemeral)
        self.assertEqual(len(key), 32)

    def test_a_short_secret_is_ACCEPTED_but_reported_as_weak(self):
        # Deliberately NOT refused: an operator's existing short secret must not
        # stop a deploy, and rejecting it would be a breaking change dressed as
        # a security fix. It is surfaced instead.
        self.assertTrue(hmac_key.is_weak(b"short"))
        self.assertFalse(hmac_key.is_weak(b"x" * 32))

    def test_the_committed_constants_are_GONE_from_the_tree(self):
        # MUTATION: re-adding any fallback anywhere. Asserted against the
        # SOURCE, because the defect is the constant EXISTING AT ALL -- three
        # separate modules had one (blackwall, approvals, seller_audit) and each
        # was found as a separate audit finding.
        #
        # The scan is deliberately BLUNT and cannot tell a mention from a use,
        # which it demonstrated by failing on the docstrings that explain the
        # fix. That is the right trade: a scan clever enough to allow mentions is
        # a scan that can be talked into allowing a use. The convention is
        # therefore to DESCRIBE these constants in prose, never quote them.
        import blackwall
        import approvals
        self.assertFalse(hasattr(blackwall, "_DEV_RECEIPT_KEY"))
        here = os.path.dirname(os.path.abspath(__file__))
        for name in ("blackwall.py", "approvals.py", "seller_audit.py", "hmac_key.py"):
            with open(os.path.join(here, name), "r", encoding="utf-8") as fh:
                src = fh.read()
            for banned in ("dev-insecure-key", "not-for-production",
                           "blackwall-dev-audit-key"):
                self.assertNotIn(
                    banned, src,
                    "%s still carries the committed key %r" % (name, banned))


class EveryConsumerSharesTheOneSecret(unittest.TestCase):
    """blackwall, approvals and seller_audit must agree on WHICH secret."""

    def setUp(self):
        hmac_key.reset_ephemeral_for_tests()
        self._prev = os.environ.get("BLACKWALL_RECEIPT_KEY")
        os.environ["BLACKWALL_RECEIPT_KEY"] = "one-operator-secret"

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("BLACKWALL_RECEIPT_KEY", None)
        else:
            os.environ["BLACKWALL_RECEIPT_KEY"] = self._prev

    def test_all_three_derive_from_hmac_key(self):
        # MUTATION: one module reading the env var itself. Then an operator
        # rotating the secret would rotate some capabilities and not others.
        import approvals
        import blackwall
        import seller_audit
        expected = hmac_key.load_key()[0]
        self.assertEqual(blackwall._receipt_key(), expected)
        self.assertEqual(approvals._key(), expected)
        self.assertEqual(seller_audit._revoke_key(), expected)

    def test_they_stay_DOMAIN_SEPARATED_on_the_shared_secret(self):
        # One secret, three capabilities. MUTATION: dropping any prefix -- the
        # right to report an outcome would become the right to approve a payment
        # or revoke a merchant.
        import approvals
        import blackwall
        import seller_audit
        subject = "the-same-string"
        tokens = {
            blackwall.sign_report_token(subject),
            approvals.sign_approval_token(subject),
            seller_audit.sign_revoke_token(subject),
        }
        self.assertEqual(len(tokens), 3, "two capabilities produced the same token")


class EphemeralIsPerProcess(unittest.TestCase):
    def test_two_processes_do_NOT_share_an_ephemeral_key(self):
        # This is the COST of the random fallback, asserted rather than implied:
        # a token does not survive a restart. It fails SAFE (a rejected report,
        # never an accepted forgery) and it is why the boot banner must warn.
        code = ("import hmac_key,sys;"
                "sys.stdout.write(hmac_key.load_key({})[0].hex())")
        env = dict(os.environ)
        env.pop("BLACKWALL_RECEIPT_KEY", None)
        a = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, env=env, cwd=os.path.dirname(
                               os.path.abspath(__file__))).stdout
        b = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, env=env, cwd=os.path.dirname(
                               os.path.abspath(__file__))).stdout
        self.assertTrue(a and b)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
