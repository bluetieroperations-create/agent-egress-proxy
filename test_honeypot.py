#!/usr/bin/env python3
"""
Tests for honeypot.py. Each states the mutation it kills.

The load-bearing test is `test_permissioned_security_is_not_a_honeypot`: this
repo has already shipped a revert-based signal that tried to downgrade BlackRock
for enforcing its own allowlist (see revert_scan / REVERT_AXIS_GATES). The
control simulation is what separates a trap from compliance, and if that
separation regresses the gate becomes the same mistake with a new name.
"""
import unittest
from decimal import Decimal

import honeypot as H
from transfer_sim import OK, RECEIVER_BLOCKED, SENDER_BLOCKED, UNKNOWN as T_UNKNOWN


def blocked(outcome, reason="reverted", cls=None):
    return {"outcome": outcome, "reason": reason, "revert_class": cls}


class TestRetention(unittest.TestCase):
    def test_basic_ratio(self):
        # kills: inverting the ratio (returned/spent, not spent/returned)
        self.assertEqual(H.round_trip_retention(1000, 980), Decimal("0.98"))

    def test_total_loss_is_zero_not_none(self):
        # kills: treating a 0 return as unreadable -- a honeypot returning
        # nothing is the strongest possible signal and must not degrade to None
        self.assertEqual(H.round_trip_retention(1000, 0), Decimal("0"))

    def test_meaningless_inputs_are_none(self):
        # kills: dividing by zero, or manufacturing a ratio from junk
        for spent, ret in ((0, 100), (-5, 100), (100, -1), ("x", 1), (None, 1),
                           (1, None), (True, 10), (10, True)):
            self.assertIsNone(H.round_trip_retention(spent, ret),
                              "%r/%r should be None" % (ret, spent))

    def test_non_finite_is_none(self):
        # REGRESSION (found by fuzz, 68 raises in 40k). Decimal(str(float("nan")))
        # builds a valid Decimal('NaN'); COMPARING it raises InvalidOperation. A
        # function documented never to raise must reject it before the comparison.
        # kills: dropping the is_nan/is_infinite guard
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertIsNone(H.round_trip_retention(1000, bad))
            self.assertIsNone(H.round_trip_retention(bad, 1000))

    def test_never_raises(self):
        # kills: letting Decimal() propagate InvalidOperation
        for a in (object(), [], {}, "1e999999"):
            H.round_trip_retention(a, a)


class TestAssess(unittest.TestCase):
    def test_pool_blocked_control_ok_is_unsellable(self):
        # kills: dropping the RECEIVER_BLOCKED branch -- the core detection
        s = H.assess_honeypot(blocked(RECEIVER_BLOCKED, "TRANSFER_FAILED"))
        self.assertEqual(s["grade"], H.UNSELLABLE)
        self.assertEqual(s["revert_reason"], "TRANSFER_FAILED")
        self.assertTrue(s["reasons"])

    def test_unsellable_ignores_compliance_shaped_wording(self):
        # kills: consulting revert_class on the RECEIVER_BLOCKED path. A honeypot
        # may borrow compliance wording; what convicts it is that the fresh
        # control was ALLOWED. Blocking only the market is not a compliance
        # posture, whatever the revert string says.
        s = H.assess_honeypot(
            blocked(RECEIVER_BLOCKED, "account is not whitelisted", "restriction"))
        self.assertEqual(s["grade"], H.UNSELLABLE)

    def test_permissioned_security_is_not_a_honeypot(self):
        # kills: treating any transfer revert as a honeypot. BUIDL/OUSG block the
        # pool AND the control (nothing is allowlisted) -> SENDER_BLOCKED with a
        # restriction-class revert -> `restricted`, deferred to rwa_readiness.
        # This is the exact false positive REVERT_AXIS_GATES exists to avoid.
        s = H.assess_honeypot(
            blocked(SENDER_BLOCKED, "ERC1400: transfer not allowed", "restriction"))
        self.assertEqual(s["grade"], H.RESTRICTED)
        self.assertNotEqual(s["grade"], H.UNSELLABLE)

    def test_sender_side_non_restriction_is_unknown(self):
        # kills: blaming the token when our own probe sender was simply broke
        s = H.assess_honeypot(
            blocked(SENDER_BLOCKED, "transfer amount exceeds balance", "balance"))
        self.assertEqual(s["grade"], H.UNKNOWN)

    def test_indeterminate_is_unknown(self):
        # kills: defaulting an unreachable RPC to anything that gates
        for o in (T_UNKNOWN, None, "garbage"):
            self.assertEqual(H.assess_honeypot(blocked(o))["grade"], H.UNKNOWN)
        self.assertEqual(H.assess_honeypot(None)["grade"], H.UNKNOWN)
        self.assertEqual(H.assess_honeypot("nonsense")["grade"], H.UNKNOWN)

    def test_clean_transfer_no_quote_is_sellable(self):
        # kills: requiring a quote before conceding a token is sellable
        self.assertEqual(H.assess_honeypot(blocked(OK))["grade"], H.SELLABLE)

    def test_normal_round_trip_is_sellable(self):
        # kills: setting RETENTION_HOLD so high that ordinary fee+slippage trips
        self.assertEqual(
            H.assess_honeypot(blocked(OK), Decimal("0.98"))["grade"], H.SELLABLE)

    def test_extractive_round_trip_is_high_sell_tax(self):
        # kills: dropping the retention axis entirely
        s = H.assess_honeypot(blocked(OK), Decimal("0.05"))
        self.assertEqual(s["grade"], H.HIGH_SELL_TAX)
        self.assertEqual(s["retention"], Decimal("0.05"))

    def test_retention_boundary_is_strict(self):
        # kills: flipping < to <= at the threshold
        self.assertEqual(H.assess_honeypot(blocked(OK), H.RETENTION_HOLD)["grade"],
                         H.SELLABLE)

    def test_unreadable_retention_does_not_gate(self):
        # kills: letting a junk quote produce a finding
        self.assertEqual(H.assess_honeypot(blocked(OK), "junk")["grade"], H.UNKNOWN)

    def test_non_finite_retention_does_not_gate(self):
        # REGRESSION: same NaN trap on the assess path. Must degrade to unknown,
        # never raise and never gate.
        # kills: dropping the is_nan guard in assess_honeypot
        for bad in (float("nan"), float("inf")):
            self.assertEqual(H.assess_honeypot(blocked(OK), bad)["grade"], H.UNKNOWN)

    def test_never_raises_on_garbage(self):
        # kills: any unguarded attribute access on a malformed attribution
        for bad in (object(), [], 3, "", {"outcome": {}}, {"outcome": OK, "x": 1}):
            H.assess_honeypot(bad, bad)


class TestApply(unittest.TestCase):
    GO = {"verdict": "GO", "score": 0.1, "reasons": ["fine"]}

    def test_unsellable_escalates_go_to_hold(self):
        # kills: recording the signal without enforcing it (the `excessive` bug --
        # a documented gate that only ever appended a warning)
        v = H.apply_honeypot(self.GO, H.assess_honeypot(blocked(RECEIVER_BLOCKED)))
        self.assertEqual(v["verdict"], "HOLD")
        self.assertEqual(v["signals"]["honeypot"]["grade"], H.UNSELLABLE)

    def test_never_stops(self):
        # kills: escalating past HOLD. Sanctions/payload keep the STOP authority;
        # this is inference from a simulation, not proof.
        v = H.apply_honeypot(self.GO, H.assess_honeypot(blocked(RECEIVER_BLOCKED)))
        self.assertNotEqual(v["verdict"], "STOP")
        self.assertFalse(v.get("hard_stop"))

    def test_never_upgrades(self):
        # kills: a clean sell path clearing an unrelated HOLD or STOP
        for start in ("HOLD", "STOP"):
            v = H.apply_honeypot({"verdict": start, "reasons": []},
                                 H.assess_honeypot(blocked(OK)))
            self.assertEqual(v["verdict"], start)

    def test_restricted_never_gates(self):
        # kills: double-counting a permissioned security -- rwa_readiness already
        # grades it, and gating here would penalise one property twice
        v = H.apply_honeypot(self.GO, H.assess_honeypot(
            blocked(SENDER_BLOCKED, "not allowed", "restriction")))
        self.assertEqual(v["verdict"], "GO")
        self.assertEqual(v["signals"]["honeypot"]["grade"], H.RESTRICTED)

    def test_sell_tax_is_advisory_by_default(self):
        # kills: shipping SELL_TAX_GATES on. Legitimate fee-on-transfer tokens
        # exist; the threshold wants measuring before it refuses anyone.
        self.assertFalse(H.SELL_TAX_GATES)
        v = H.apply_honeypot(self.GO, H.assess_honeypot(blocked(OK), Decimal("0.05")))
        self.assertEqual(v["verdict"], "GO")
        self.assertEqual(v["signals"]["honeypot"]["grade"], H.HIGH_SELL_TAX)
        self.assertTrue(any("taxed" in r for r in v["reasons"]))

    def test_sell_tax_gates_when_the_lock_is_flipped(self):
        # kills: a lock that is wired but inert -- the gate must actually exist
        v = H.apply_honeypot(self.GO, H.assess_honeypot(blocked(OK), Decimal("0.05")),
                             sell_tax_gates=True)
        self.assertEqual(v["verdict"], "HOLD")

    def test_unknown_does_not_gate(self):
        # kills: fail-closed on an unreachable chain
        v = H.apply_honeypot(self.GO, H.assess_honeypot(blocked(T_UNKNOWN)))
        self.assertEqual(v["verdict"], "GO")

    def test_malformed_signal_leaves_verdict_untouched(self):
        # kills: mutating the verdict from junk input
        for bad in (None, {}, {"grade": "wat"}, [], "x", 7):
            self.assertEqual(H.apply_honeypot(self.GO, bad), self.GO)

    def test_non_mutating(self):
        # kills: in-place edits leaking into the caller's verdict
        original = {"verdict": "GO", "reasons": ["fine"], "signals": {"a": 1}}
        snapshot = {"verdict": "GO", "reasons": ["fine"], "signals": {"a": 1}}
        H.apply_honeypot(original, H.assess_honeypot(blocked(RECEIVER_BLOCKED)))
        self.assertEqual(original, snapshot)


class TestSource(unittest.TestCase):
    def test_fail_open_on_transport_error(self):
        # kills: letting an RPC exception escape into the verdict path
        class Boom:
            def assess(self, *a, **k):
                raise RuntimeError("rpc down")
        s = H.HoneypotSource(simulator=Boom(), holder_lookup=lambda t, c: "0x1",
                             pool_lookup=lambda t, c: "0x2")
        self.assertEqual(s.check("0xtok", "base")["grade"], H.UNKNOWN)

    def test_no_pool_is_unknown_not_a_finding(self):
        # kills: reading "no pool found" as "cannot be sold". An unlisted token
        # is not a honeypot; it just has no market we can probe.
        s = H.HoneypotSource(simulator=object(), holder_lookup=lambda t, c: "0x1",
                             pool_lookup=lambda t, c: None)
        self.assertEqual(s.check("0xtok", "base")["grade"], H.UNKNOWN)

    def test_no_holder_is_unknown(self):
        # kills: simulating from an empty wallet, whose BALANCE revert would be
        # indistinguishable from a restriction
        s = H.HoneypotSource(simulator=object(), holder_lookup=lambda t, c: None,
                             pool_lookup=lambda t, c: "0x2")
        self.assertEqual(s.check("0xtok", "base")["grade"], H.UNKNOWN)

    def test_missing_token_or_deps_is_unknown(self):
        # kills: probing with nothing configured
        self.assertEqual(H.HoneypotSource().check("0xtok", "base")["grade"], H.UNKNOWN)
        s = H.HoneypotSource(simulator=object(), pool_lookup=lambda t, c: "0x2")
        self.assertEqual(s.check(None, "base")["grade"], H.UNKNOWN)

    def test_drives_the_simulator_at_the_pool(self):
        # kills: simulating at the wrong receiver -- probing a fresh EOA instead
        # of the pool would make every honeypot look sellable
        seen = {}
        class Sim:
            def assess(self, token, frm, to, amount):
                seen.update(token=token, frm=frm, to=to)
                return blocked(RECEIVER_BLOCKED)
        s = H.HoneypotSource(simulator=Sim(), holder_lookup=lambda t, c: "0xholder",
                             pool_lookup=lambda t, c: "0xpool")
        out = s.check("0xtok", "base")
        self.assertEqual(seen["to"], "0xpool")
        self.assertEqual(seen["frm"], "0xholder")
        self.assertEqual(out["grade"], H.UNSELLABLE)

    def test_retention_folds_in_when_a_quoter_is_present(self):
        # kills: wiring a quoter that is never consulted
        class Sim:
            def assess(self, *a, **k):
                return blocked(OK)
        def quoter(token, chain, side, amount=None):
            return (1000, 500) if side == "buy" else (500, 10)
        s = H.HoneypotSource(simulator=Sim(), holder_lookup=lambda t, c: "0xh",
                             pool_lookup=lambda t, c: "0xp", quoter=quoter)
        out = s.check("0xtok", "base")
        self.assertEqual(out["grade"], H.HIGH_SELL_TAX)
        self.assertEqual(out["retention"], Decimal("0.01"))

    def test_broken_quoter_does_not_break_the_check(self):
        # kills: a quote failure taking down the transfer-path finding with it
        class Sim:
            def assess(self, *a, **k):
                return blocked(RECEIVER_BLOCKED)
        def quoter(*a, **k):
            raise RuntimeError("quoter down")
        s = H.HoneypotSource(simulator=Sim(), holder_lookup=lambda t, c: "0xh",
                             pool_lookup=lambda t, c: "0xp", quoter=quoter)
        self.assertEqual(s.check("0xtok", "base")["grade"], H.UNSELLABLE)


if __name__ == "__main__":
    unittest.main()


class TestHandlerBinding(unittest.TestCase):
    """STRUCTURAL parity: every source `_Handler` reads must be bound by
    `serve_forever`.

    REGRESSION. Adding a source takes seven edits -- forecast signature, the fold,
    the `_Handler` class default, the handler call site, `BlackwallServer.__init__`,
    the serve() call site, and the `_BoundHandler` dict. Six of those are
    signatures; the seventh is a DICT LITERAL, so omitting it raises nothing. The
    honeypot source was wired in all six and inert because of the seventh: the
    handler kept its `None` default and the check never ran, while the startup
    banner still announced it as ON. Unit tests passed, the redteam passed, and the
    live endpoint answered in 7ms because it was doing nothing.

    This asserts the property rather than the instance, so the next source added
    cannot repeat it.
    """

    def test_every_handler_source_is_bound(self):
        # kills: omitting any `*_source` from the _BoundHandler dict
        import ast
        import inspect

        import blackwall
        tree = ast.parse(inspect.getsource(blackwall))
        handler_defaults, bound = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "_Handler":
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        for t in stmt.targets:
                            if isinstance(t, ast.Name) and t.id.endswith("_source"):
                                handler_defaults.add(t.id)
            if isinstance(node, ast.FunctionDef) and node.name == "serve_forever":
                for d in ast.walk(node):
                    if isinstance(d, ast.Dict):
                        for k in d.keys:
                            if isinstance(k, ast.Constant) and \
                                    isinstance(k.value, str) and \
                                    k.value.endswith("_source"):
                                bound.add(k.value)
        self.assertTrue(handler_defaults, "found no *_source defaults on _Handler")
        missing = handler_defaults - bound
        self.assertEqual(missing, set(),
                         "sources declared on _Handler but never bound in "
                         "serve_forever (they stay None and silently never "
                         "run): %s" % sorted(missing))
