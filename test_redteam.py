"""
test_redteam.py -- the adversarial scorecard doubles as a regression guard: the
CAUGHT set must stay caught, no legit control may become a FALSE POSITIVE, and no
attack may silently MISS (a gap must be an EXPLICIT known_gap, never a surprise).
"""
import unittest

import redteam


class TestScorecard(unittest.TestCase):
    def setUp(self):
        self.results = redteam.run()

    def test_no_false_positive_and_no_surprise_miss(self):
        # a legit control blocked, or an attack that gets GO without being marked a
        # known_gap, is a real regression.
        for r in self.results:
            self.assertNotEqual(r["disposition"], "FALSE POSITIVE",
                                "legit control blocked: %s" % r["name"])
            self.assertNotEqual(r["disposition"], "MISS (BUG)",
                                "attack slipped through unmarked: %s" % r["name"])

    def test_caught_set_does_not_shrink(self):
        caught = [r for r in self.results if r["disposition"] == "CAUGHT"]
        # if a gate regresses (stops blocking its attack) this count drops. Tightened
        # from a loose floor to the ACTUAL count -- a weak floor guards nothing.
        # AUDIT: it had drifted back to being weak (floor 24, actual 30), which is
        # six gates' worth of regression the guard would not have noticed.
        self.assertGreaterEqual(len(caught), 31)

    def test_simulation_gates_are_represented_and_catch(self):
        """The newest gates (settlement / authorization / RWA simulation) fold in
        `forecast`, not `decide_payment`, so they were invisible to this scorecard
        until now. Every one of these families must be present AND blocking."""
        by_cat = {}
        for r in self.results:
            by_cat.setdefault(r["category"], []).append(r)
        for cat in ("settlement-sim", "auth-sim", "rwa-sim", "payee-syntax",
                    "currency"):
            self.assertIn(cat, by_cat, "simulation family missing from the scorecard")
            for r in by_cat[cat]:
                if r["expect"] == "block":
                    self.assertEqual(r["disposition"], "CAUGHT",
                                     "%s regressed: %s" % (cat, r["name"]))

    def test_simulation_gates_do_not_over_block(self):
        """The real risk with a simulation gate is FALSE POSITIVES, so the controls
        that pin its restraint are called out by name: a sender-side revert must not be
        blamed on the receiver, an underfunded payer must not gate, and an unreachable
        RPC must fail OPEN."""
        names = {r["name"]: r for r in self.results}
        for n in ("SENDER-side revert (attribution)",
                  "underfunded payer (must not gate)",
                  "RPC unreachable (fail-open)",
                  "clean payment, sim ready",
                  "valid fresh authorization",
                  # The payee-syntax gate is chain-agnostic on purpose: a raw
                  # base58 Solana payee must not be condemned for failing to be
                  # an EVM address.
                  "non-EVM payee is not condemned",
                  # The currency gate is knowledge-based: an unfamiliar asset is
                  # not a non-dollar one, and must not be gated on a guess.
                  "an unrecognized asset is not gated on a guess"):
            self.assertIn(n, names, "restraint control missing: %s" % n)
            self.assertEqual(names[n]["disposition"], "CLEAN",
                             "simulation gate over-blocked: %s" % n)

    def test_every_core_attack_is_caught(self):
        # the non-gap attacks, by category, must each be blocked.
        for r in self.results:
            if r["expect"] == "block" and not r["known_gap"]:
                self.assertIn(r["verdict"], ("HOLD", "STOP"),
                              "core attack not blocked: %s" % r["name"])


if __name__ == "__main__":
    unittest.main()
