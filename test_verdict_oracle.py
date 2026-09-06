"""
test_verdict_oracle.py -- guards the frozen behavioral snapshot (Stage-0 oracle).

Re-runs verdict_oracle.snapshot() and diffs it against the committed golden
data/verdict_oracle.json. ANY difference fails, with a readable per-row diff.

This is the regression instrument for the data-completeness work: it lets a
change PROVE it did not alter how the verdict works. When a behavior change IS
intentional (e.g. promoting sybil_ring to a gate), regenerate the golden in the
SAME commit (`python verdict_oracle.py --write`); the golden diff in code review
is then the exact, auditable record of which verdicts changed and why.

Mutation notes:
  - any decision-logic change that shifts a verdict/hard_stop/score without a
    golden regen -> test_matches_golden FAILS (the whole point).
  - a structural invariant break (hard_stop on a non-STOP; a non-good record
    winning GO without the verified-floor waiver) -> the invariant tests FAIL
    even if someone regenerated the golden to hide it.
"""
import json
import os
import unittest

import verdict_oracle as O


class TestVerdictOracle(unittest.TestCase):
    def setUp(self):
        self.golden_path = os.path.join(os.path.dirname(__file__) or ".", O.GOLDEN_PATH)

    def test_golden_exists(self):
        self.assertTrue(os.path.exists(self.golden_path),
                        "run `python verdict_oracle.py --write` to create the golden")

    def test_matches_golden(self):
        with open(self.golden_path) as f:
            golden = json.load(f)
        current = O.snapshot()
        gi = {r["id"]: r for r in golden}
        ci = {r["id"]: r for r in current}

        # ids drift (grid changed) -> that is itself an intentional-change signal.
        self.assertEqual(set(gi), set(ci),
                         "scenario grid changed; regenerate the golden if intended")

        diffs = []
        for sid in sorted(ci):
            if ci[sid] != gi[sid]:
                diffs.append("  %s\n    golden : %s\n    current: %s"
                             % (sid, gi[sid], ci[sid]))
        self.assertFalse(diffs, "verdict behavior drifted from the golden:\n"
                         + "\n".join(diffs)
                         + "\n\nIf this change is INTENTIONAL, regenerate with "
                           "`python verdict_oracle.py --write` and review the diff.")

    # --- structural invariants: true regardless of any golden regeneration ---
    def test_hard_stop_implies_stop(self):
        # a hard_stop that is not a STOP would be a boundary breach (a HOLD/GO that
        # claims un-overridable harm). Kills a mutation that leaks hard_stop upward.
        for r in O.snapshot():
            if r["hard_stop"]:
                self.assertEqual(r["verdict"], "STOP", "hard_stop on non-STOP: %s" % r["id"])

    def test_only_good_or_verified_thin_can_go(self):
        # GO is reserved for a 'good' record, OR a thin record carrying the
        # verified-floor badge (the ONE documented waiver of the thin-count gate;
        # it never waives Sybil/STOP). Any other GO is a Sybil/thin regression.
        for r in O.snapshot():
            if r["verdict"] == "GO":
                ok = r["id"].startswith("good|") or (
                    r["id"].startswith("thin|") and r["id"].endswith("|verified_floor"))
                self.assertTrue(ok, "unexpected GO: %s" % r["id"])


if __name__ == "__main__":
    unittest.main()


class TestTheNonDollarOverlayIsCovered(unittest.TestCase):
    """AUDIT of the currency gate: it shipped without an overlay in this grid.

    This file is the frozen tripwire for `decide_payment`. A verdict-affecting
    branch it does not exercise is a branch it cannot protect -- the golden file
    passed unchanged after the gate landed, because nothing in the grid set
    `non_usd_amount`.
    """

    def _rows(self):
        return [r for r in O.snapshot() if r["id"].endswith("|non_usd_amount")]

    def test_the_overlay_is_present_in_the_grid(self):
        # Kills: dropping the overlay, which silently returns the gate to
        # being invisible to the oracle.
        self.assertEqual(len(self._rows()), 40)

    def test_a_non_dollar_amount_never_auto_approves_anywhere_in_the_grid(self):
        # Kills: any path that lets a dollar threshold clear a non-dollar
        # amount. Measured across every record x price combination: 24 HOLD,
        # 16 STOP where a stronger gate already fired, and ZERO GO.
        self.assertEqual([r for r in self._rows() if r["verdict"] == "GO"], [])

    def test_it_never_downgrades_a_stop(self):
        # Kills: the escalation table rewriting a STOP as HOLD. Sanctions,
        # payload mismatch and a high-severity secret keep the STOP authority.
        #
        # The first version of this test also asserted every STOP here is a
        # HARD stop. That was my assumption, not the behaviour: a price gouge
        # STOPs on SCORE without setting hard_stop, and 78 of the grid's 328
        # STOPs are soft for that reason. The test was wrong, not the code --
        # so it asserts the property that actually matters, which is that the
        # overlay adds caution and never subtracts it.
        stops = [r for r in self._rows() if r["verdict"] == "STOP"]
        self.assertEqual(len(stops), 16)
        base = {r["id"].replace("|non_usd_amount", "|none"): r for r in O.snapshot()}
        for row in stops:
            plain = base.get(row["id"].replace("|non_usd_amount", "|none"))
            if plain and plain["verdict"] == "STOP":
                self.assertEqual(row["hard_stop"], plain["hard_stop"], row["id"])

    def test_blast_radius_reports_unknown_not_bounded(self):
        # Kills: claiming "bounded", which is a statement about the DOLLAR
        # value -- exactly the thing that could not be computed.
        self.assertTrue(all(r["blast_radius"] == "unknown" for r in self._rows()))
