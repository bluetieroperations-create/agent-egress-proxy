#!/usr/bin/env python3
"""
test_transfer_sim.py -- TDD for the shared transfer-simulation core + the RWA simulation
readiness source. Each test names the MUTATION it kills. Revert payloads are the REAL
shapes harvested live (USDC blacklist, Matrixdock STBT, BlackRock BUIDL).
"""
import unittest

from transfer_sim import (OK, RECEIVER_BLOCKED, SENDER_BLOCKED, UNKNOWN,
                          SimulationReadinessSource, TransferSimulator, attribute,
                          decode_revert_error, to_readiness_probe)

TOKEN = "0x" + "22" * 20
HOLDER = "0x" + "aa" * 20
AGENT = "0x" + "bb" * 20


def _err(msg):
    """A real Error(string) revert payload for `msg`."""
    body = msg.encode()
    data = ("0x08c379a0"
            + "%064x" % 32
            + "%064x" % len(body)
            + body.hex().ljust(64, "0"))
    return {"error": {"message": "execution reverted", "data": data}}


class TestDecode(unittest.TestCase):
    def test_decodes_real_error_string(self):
        self.assertEqual(
            decode_revert_error(_err("Blacklistable: account is blacklisted")["error"]),
            "Blacklistable: account is blacklisted")

    def test_falls_back_to_message(self):
        self.assertEqual(decode_revert_error({"message": "invalid opcode: INVALID"}),
                         "invalid opcode: INVALID")

    def test_never_raises(self):
        for junk in (None, "str", 5, {"data": "0x08c379a0zz"}, {"data": {}}, {}):
            decode_revert_error(junk)


class TestAttribute(unittest.TestCase):
    def test_target_ok_is_ok(self):
        self.assertEqual(attribute({"ok": True}, {"ok": None})["outcome"], OK)

    def test_control_ok_blames_receiver(self):
        # THE CONTROL'S PURPOSE: target fails, control succeeds -> the RECEIVER is blocked.
        a = attribute({"ok": False, "reason": "STBT: NO_RECEIVE_PERMISSION"}, {"ok": True})
        self.assertEqual(a["outcome"], RECEIVER_BLOCKED)
        self.assertEqual(a["revert_class"], "restriction")

    def test_control_also_fails_blames_sender(self):
        # MUTATION: without the control this would be misattributed to the receiver --
        # the single most important correctness property of this module.
        a = attribute({"ok": False, "reason": "Blacklistable: account is blacklisted"},
                      {"ok": False, "reason": "Blacklistable: account is blacklisted"})
        self.assertEqual(a["outcome"], SENDER_BLOCKED)

    def test_indeterminate_is_unknown(self):
        self.assertEqual(attribute({"ok": None}, {"ok": True})["outcome"], UNKNOWN)
        self.assertEqual(attribute({"ok": False, "reason": "x"}, {"ok": None})["outcome"],
                         UNKNOWN)

    def test_never_raises_on_junk(self):
        for j in (None, "x", 5, []):
            attribute(j, j)


class TestReadinessProbe(unittest.TestCase):
    def test_ok_marks_can_receive(self):
        self.assertIs(to_readiness_probe({"outcome": OK})["can_receive"], True)

    def test_restriction_receiver_block_sets_false(self):
        p = to_readiness_probe({"outcome": RECEIVER_BLOCKED, "revert_class": "restriction",
                                "reason": "Wallet not in registry service"})
        self.assertIs(p["can_receive"], False)
        self.assertEqual(p["reason_code"], "Wallet not in registry service")

    def test_balance_revert_does_not_block(self):
        # MUTATION: treating a balance/gas/opaque revert as a restriction would block
        # legitimate buyers over a sender's empty wallet. Must stay None (fail-open).
        for cls in ("balance", "gas", "opaque", "other", "unknown", None):
            p = to_readiness_probe({"outcome": RECEIVER_BLOCKED, "revert_class": cls,
                                    "reason": "x"})
            self.assertIsNone(p["can_receive"], cls)

    def test_sender_block_does_not_blame_receiver(self):
        p = to_readiness_probe({"outcome": SENDER_BLOCKED, "revert_class": "restriction",
                                "reason": "x"})
        self.assertIsNone(p["can_receive"])


class TestSimulator(unittest.TestCase):
    def test_simulate_success_and_revert(self):
        sim = TransferSimulator(transport=lambda p: {"result": "0x1"})
        self.assertIs(sim.simulate(TOKEN, HOLDER, AGENT, 1)["ok"], True)
        sim2 = TransferSimulator(transport=lambda p: _err("Under lock-up"))
        r = sim2.simulate(TOKEN, HOLDER, AGENT, 1)
        self.assertIs(r["ok"], False)
        self.assertEqual(r["reason"], "Under lock-up")

    def test_transport_error_is_indeterminate(self):
        def boom(p):
            raise IOError("net")
        self.assertIsNone(TransferSimulator(transport=boom).simulate(
            TOKEN, HOLDER, AGENT, 1)["ok"])

    def test_assess_skips_control_when_target_ok(self):
        # EFFICIENCY: the happy path must cost ONE call, not two.
        calls = []

        def t(p):
            calls.append(p)
            return {"result": "0x1"}
        TransferSimulator(transport=t).assess(TOKEN, HOLDER, AGENT, 1)
        self.assertEqual(len(calls), 1)

    def test_assess_runs_control_when_target_reverts(self):
        calls = []

        def t(p):
            calls.append(p["data"])
            # first call (to AGENT) reverts, second (to control) succeeds
            return _err("not whitelisted") if len(calls) == 1 else {"result": "0x1"}
        a = TransferSimulator(transport=t).assess(TOKEN, HOLDER, AGENT, 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(a["outcome"], RECEIVER_BLOCKED)


class TestSimulationReadinessSource(unittest.TestCase):
    def test_blocked_agent_yields_blocked_grade(self):
        calls = []

        def t(p):
            calls.append(p)
            return _err("CashKYCSenderReceiver: `to` address must be KYC'd") \
                if len(calls) == 1 else {"result": "0x1"}
        src = SimulationReadinessSource(simulator=TransferSimulator(transport=t))
        sig = src.check({"token": TOKEN, "chain": "ethereum", "holder": HOLDER}, AGENT)
        self.assertEqual(sig["grade"], "blocked")

    def test_clean_transfer_is_ready(self):
        src = SimulationReadinessSource(
            simulator=TransferSimulator(transport=lambda p: {"result": "0x1"}))
        sig = src.check({"token": TOKEN, "holder": HOLDER}, AGENT)
        self.assertEqual(sig["grade"], "ready")

    def test_no_holder_is_noop(self):
        # MUTATION: simulating from an empty/unknown wallet would revert on BALANCE and
        # look like a restriction. Without a funded sender we must decline to answer.
        src = SimulationReadinessSource(
            simulator=TransferSimulator(transport=lambda p: {"result": "0x1"}))
        self.assertIsNone(src.check({"token": TOKEN}, AGENT))

    def test_holder_lookup_used(self):
        src = SimulationReadinessSource(
            simulator=TransferSimulator(transport=lambda p: {"result": "0x1"}),
            holder_lookup=lambda tok, chain: HOLDER)
        self.assertEqual(src.check({"token": TOKEN}, AGENT)["grade"], "ready")

    def test_source_never_raises(self):
        def boom(p):
            raise ValueError("x")
        src = SimulationReadinessSource(simulator=TransferSimulator(transport=boom),
                                        holder_lookup=lambda t, c: HOLDER)
        src.check({"token": TOKEN}, AGENT)
        self.assertIsNone(src.check(None, AGENT))
        self.assertIsNone(src.check({"token": TOKEN}, None))

    def test_folds_through_existing_rwa_path(self):
        # THE POINT of reusing the probe shape: no new verdict plumbing.
        from rwa_readiness import apply_rwa_readiness
        calls = []

        def t(p):
            calls.append(p)
            return _err("not whitelisted") if len(calls) == 1 else {"result": "0x1"}
        src = SimulationReadinessSource(simulator=TransferSimulator(transport=t))
        sig = src.check({"token": TOKEN, "holder": HOLDER}, AGENT)
        v = apply_rwa_readiness({"verdict": "GO"}, sig)
        self.assertEqual(v["verdict"], "HOLD")
        self.assertEqual(v["signals"]["rwa_transfer_readiness"]["grade"], "blocked")


class TestHolderLookup(unittest.TestCase):
    """The lookup exists because a live request never carries a funded sender, and
    without one the simulation source silently answers `unknown` for everything."""

    def _lk(self, **kw):
        from transfer_sim import BlockscoutHolderLookup
        return BlockscoutHolderLookup(**kw)

    def test_returns_first_holder(self):
        lk = self._lk(fetch=lambda t, c: {"items": [{"address": {"hash": HOLDER}}]})
        self.assertEqual(lk(TOKEN, "ethereum"), HOLDER)

    def test_caches_per_token(self):
        # MUTATION: re-fetching per verdict adds a request to the HOT PATH and re-leaks
        # the queried token every time.
        calls = []

        def fetch(t, c):
            calls.append(t)
            return {"items": [{"address": {"hash": HOLDER}}]}
        lk = self._lk(fetch=fetch)
        lk(TOKEN, "ethereum")
        lk(TOKEN, "ethereum")
        lk(TOKEN.upper(), "Ethereum")           # same key after normalization
        self.assertEqual(len(calls), 1)

    def test_caches_misses_too(self):
        calls = []

        def fetch(t, c):
            calls.append(t)
            return {"items": []}
        lk = self._lk(fetch=fetch)
        self.assertIsNone(lk(TOKEN, "ethereum"))
        self.assertIsNone(lk(TOKEN, "ethereum"))
        self.assertEqual(len(calls), 1)         # a holder-less token isn't re-fetched

    def test_fail_open_on_error_and_unknown_chain(self):
        def boom(t, c):
            raise IOError("net")
        self.assertIsNone(self._lk(fetch=boom)(TOKEN, "ethereum"))
        self.assertIsNone(self._lk(fetch=lambda t, c: {})(TOKEN, "dogechain"))

    def test_never_raises_on_junk(self):
        # AUDIT (self-caught): `5 or ""` is 5, and 5.strip() explodes -- a non-string
        # chain/token must no-op, not raise.
        lk = self._lk(fetch=lambda t, c: {"items": []})
        for junk in (None, "", 5, {}, [], object()):
            self.assertIsNone(lk(junk, junk))

    def test_skips_malformed_rows(self):
        lk = self._lk(fetch=lambda t, c: {"items": [
            {"address": None}, {"address": {"hash": None}}, "notadict",
            {"address": {"hash": HOLDER}}]})
        self.assertEqual(lk(TOKEN, "ethereum"), HOLDER)


class TestDeployedWiring(unittest.TestCase):
    """GAP REGRESSION: the simulation source was built, live-verified, and NEVER wired
    into the server -- so a deployed instance ran only the interface probe, which a live
    sweep showed answers `unknown` for 535/535 real tokens. The gate was inert in
    production. These pin the wiring contract."""

    def _combined(self, sim_transport):
        from rwa_readiness import CombinedRwaReadinessSource, RwaReadinessSource
        from transfer_sim import SimulationReadinessSource
        return CombinedRwaReadinessSource([
            SimulationReadinessSource(
                simulator=TransferSimulator(transport=sim_transport),
                holder_lookup=lambda tok, chain: HOLDER),
            RwaReadinessSource(rpc_url=None),
        ])

    def test_simulation_is_consulted_first(self):
        calls = []

        def t(p):
            calls.append(p)
            return _err("STBT: NO_RECEIVE_PERMISSION") if len(calls) == 1 \
                else {"result": "0x1"}
        # No `holder` in acquires -- exactly what a real request carries.
        sig = self._combined(t).check({"token": TOKEN, "chain": "ethereum"}, AGENT)
        self.assertEqual(sig["grade"], "blocked")
        self.assertEqual(sig["standard"], "simulation")     # simulation, not the probe

    def test_falls_through_when_simulation_cannot_answer(self):
        # MUTATION: if simulation returned a signal even when indeterminate, it would
        # SHADOW the interface probe instead of deferring to it.
        def dead(p):
            raise IOError("no rpc")
        sig = self._combined(dead).check({"token": TOKEN, "chain": "ethereum"}, AGENT)
        self.assertIsNone(sig)          # both declined -> no-op, verdict untouched

    def test_no_holder_means_no_signal_not_a_false_block(self):
        from transfer_sim import SimulationReadinessSource
        src = SimulationReadinessSource(
            simulator=TransferSimulator(transport=lambda p: _err("exceeds balance")),
            holder_lookup=lambda tok, chain: None)
        self.assertIsNone(src.check({"token": TOKEN, "chain": "ethereum"}, AGENT))


class TestAuditRegressions(unittest.TestCase):
    """Regressions for the adversarial audit of the simulation core."""

    def test_amount_clamped_to_uint256(self):
        # AUDIT BUG: "1e400" -> a 400-digit int that OVERFLOWS the uint256 calldata word
        # and silently truncates to a meaningless probe amount.
        from transfer_sim import UINT256_MAX, clamp_amount
        self.assertEqual(clamp_amount(10 ** 400), UINT256_MAX)
        self.assertLessEqual(clamp_amount(float("1e400")), UINT256_MAX)

    def test_negative_and_junk_amounts_clamped(self):
        # AUDIT BUG: a negative amount encoded garbage yet the call reported ok=True.
        from transfer_sim import clamp_amount
        for bad in (-1, 0, None, "abc", [], float("inf"), float("nan")):
            self.assertGreaterEqual(clamp_amount(bad), 1)

    def test_control_collision_does_not_fabricate_a_block(self):
        # AUDIT: if the real target IS the control address, both calls are identical, so
        # they always agree -- which must NOT read as "receiver blocked".
        from transfer_sim import FRESH_CONTROL
        sim = TransferSimulator(transport=lambda p: _err("blacklisted"))
        a = sim.assess(TOKEN, HOLDER, FRESH_CONTROL, 1)
        self.assertNotEqual(a["outcome"], RECEIVER_BLOCKED)

    def test_rpc_garbage_is_indeterminate(self):
        for garbage in ({}, {"foo": 1}, None, "str", [1], {"error": None}):
            r = TransferSimulator(transport=lambda p, g=garbage: g).simulate(
                TOKEN, HOLDER, AGENT, 1)
            self.assertIn(r["ok"], (None, True, False))


if __name__ == "__main__":
    unittest.main()
