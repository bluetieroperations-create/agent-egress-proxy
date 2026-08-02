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
        # if a gate regresses (stops blocking its attack) this count drops.
        self.assertGreaterEqual(len(caught), 12)

    def test_every_core_attack_is_caught(self):
        # the non-gap attacks, by category, must each be blocked.
        for r in self.results:
            if r["expect"] == "block" and not r["known_gap"]:
                self.assertIn(r["verdict"], ("HOLD", "STOP"),
                              "core attack not blocked: %s" % r["name"])


if __name__ == "__main__":
    unittest.main()
