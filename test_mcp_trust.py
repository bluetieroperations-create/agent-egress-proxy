"""Tests for the MCP capability-mismatch gate. Each names the mutation it kills."""

import unittest
import mcp_trust as m


def row(url, cls="tools_listed", tools=None, name="io.github.a/b", digest="d"):
    return {"url": url, "class": cls, "name": name, "tools_digest": digest,
            "tool_count": len(tools or []),
            "tools": [{"name": t} for t in (tools or [])]}


REAL_DESC = "Solana pre-trade safety for agents: rug check, honeypot sell-sim"


class TestHostOf(unittest.TestCase):
    def test_extracts_from_url(self):
        self.assertEqual(m.host_of("https://api.example.com/mcp"), "api.example.com")

    def test_lowercases(self):
        # kills: case-sensitive join -- a registry URL and an x402 payTo are
        # written by different parties and differ in case
        self.assertEqual(m.host_of("https://API.Example.COM/x"), "api.example.com")

    def test_strips_port_and_userinfo(self):
        # kills: ':8080' or 'user@' defeating the join
        self.assertEqual(m.host_of("https://u:p@api.example.com:8443/mcp"),
                         "api.example.com")

    def test_accepts_bare_host(self):
        # kills: requiring a scheme, so directory hosts never match
        self.assertEqual(m.host_of("api.example.com"), "api.example.com")

    def test_empty_is_none(self):
        # kills: returning "" and matching an empty index key
        self.assertIsNone(m.host_of(""))
        self.assertIsNone(m.host_of(None))


class TestGradeRow(unittest.TestCase):
    def test_boilerplate_plus_real_description_is_mismatch(self):
        # kills: trusting the description -- unverified publisher copy
        self.assertEqual(
            m.grade_row(row("https://a", tools=["echo", "add", "server_time"]),
                        REAL_DESC), m.MISMATCH)

    def test_boilerplate_with_stub_description_is_ready(self):
        # kills: flagging honest demo servers that never claimed anything
        self.assertEqual(m.grade_row(row("https://a", tools=["echo"]), "demo"),
                         m.READY)

    def test_real_tools_are_ready(self):
        # kills: flagging every server with a long description
        self.assertEqual(
            m.grade_row(row("https://a", tools=["search_flights"]), REAL_DESC),
            m.READY)

    def test_boilerplate_alongside_real_tools_is_ready(self):
        # kills: flagging a genuine server that also ships a health probe
        self.assertEqual(
            m.grade_row(row("https://a", tools=["echo", "get_invoice"]), REAL_DESC),
            m.READY)

    def test_zero_tools_is_empty(self):
        # kills: collapsing "handshakes but exposes nothing" into ready
        self.assertEqual(m.grade_row(row("https://a", tools=[]), REAL_DESC), m.EMPTY)

    def test_dead_is_unreachable(self):
        # kills: grading an unmeasurable server as if it had been inspected
        self.assertEqual(m.grade_row(row("https://a", cls="dead"), REAL_DESC),
                         m.UNREACHABLE)

    def test_gated_server_is_ready_not_mismatch(self):
        # kills: punishing a server for requiring auth -- we could not enumerate
        # it, which is not evidence it lies
        self.assertEqual(m.grade_row(row("https://a", cls="auth_required"), REAL_DESC),
                         m.READY)

    def test_garbage_is_unknown(self):
        # kills: raising on a malformed row
        self.assertEqual(m.grade_row(None), m.UNKNOWN)


class TestBuildIndex(unittest.TestCase):
    def test_reads_servers_key_or_bare_list(self):
        # kills: handling only one of the two stored shapes
        r = row("https://a.com", tools=["x"])
        self.assertIn("a.com", m.build_index({"servers": [r]}))
        self.assertIn("a.com", m.build_index([r]))

    def test_worst_grade_wins_per_host(self):
        # kills: first-wins, letting a clean entry mask a mismatching one on the
        # SAME host -- the host is what a payment resolves to
        idx = m.build_index(
            [row("https://a.com/one", tools=["real_tool"], name="x/clean"),
             row("https://a.com/two", tools=["echo", "add"], name="x/liar")],
            {"x/clean": REAL_DESC, "x/liar": REAL_DESC})
        self.assertEqual(idx["a.com"]["grade"], m.MISMATCH)

    def test_worst_grade_wins_regardless_of_order(self):
        # kills: order-dependent grading
        idx = m.build_index(
            [row("https://a.com/two", tools=["echo", "add"], name="x/liar"),
             row("https://a.com/one", tools=["real_tool"], name="x/clean")],
            {"x/clean": REAL_DESC, "x/liar": REAL_DESC})
        self.assertEqual(idx["a.com"]["grade"], m.MISMATCH)

    def test_rows_without_url_skipped(self):
        # kills: a None host key colliding with every lookup miss
        self.assertEqual(m.build_index([{"class": "dead"}]), {})

    def test_empty_reading_is_empty_index(self):
        # kills: raising on a missing/corrupt artifact instead of failing open
        self.assertEqual(m.build_index(None), {})
        self.assertEqual(m.build_index({}), {})


class TestSource(unittest.TestCase):
    def setUp(self):
        self.src = m.McpTrustSource(
            reading=[row("https://liar.com", tools=["echo", "add", "server_time"],
                         name="x/liar")],
            descriptions={"x/liar": REAL_DESC}, as_of="2026-08-27")

    def test_lookup_by_full_resource_url(self):
        # kills: requiring a bare host -- a payment carries a full resource URL
        s = self.src.signal("https://liar.com/x402/pay")
        self.assertEqual(s["grade"], m.MISMATCH)
        self.assertEqual(s["as_of"], "2026-08-27")

    def test_unknown_host_is_none(self):
        # kills: returning a default grade for unmeasured payees, which would
        # gate the entire ecosystem we have never probed
        self.assertIsNone(self.src.signal("https://never-seen.com/x"))

    def test_signal_does_not_leak_index_state(self):
        # kills: returning the stored dict, so a caller mutating the signal
        # corrupts the startup index for every later request
        s = self.src.signal("https://liar.com/a")
        s["grade"] = "tampered"
        self.assertEqual(self.src.signal("https://liar.com/a")["grade"], m.MISMATCH)


class TestApply(unittest.TestCase):
    def go(self):
        return {"verdict": "GO", "score": 10, "reasons": [], "signals": {}}

    def test_mismatch_escalates_go_to_hold(self):
        # kills: recording the signal without gating -- the whole point
        v = m.apply_mcp_trust(self.go(), {"grade": m.MISMATCH, "host": "a.com"})
        self.assertEqual(v["verdict"], "HOLD")
        self.assertTrue(v["signals"]["mcp_trust"]["gated"])

    def test_empty_escalates(self):
        # kills: gating only on mismatch and ignoring a zero-tool endpoint
        self.assertEqual(
            m.apply_mcp_trust(self.go(), {"grade": m.EMPTY})["verdict"], "HOLD")

    def test_never_stops(self):
        # kills: escalating to STOP -- intent is NOT established for any
        # publisher, and sanctions/payload keep the STOP authority
        v = m.apply_mcp_trust(self.go(), {"grade": m.MISMATCH})
        self.assertNotEqual(v["verdict"], "STOP")

    def test_never_upgrades_a_stop(self):
        # kills: a READY/mismatch signal clearing a STOP from another gate
        stop = {"verdict": "STOP", "reasons": ["sanctioned"], "signals": {}}
        for g in (m.READY, m.MISMATCH, m.EMPTY, m.UNKNOWN):
            self.assertEqual(m.apply_mcp_trust(stop, {"grade": g})["verdict"], "STOP")

    def test_hold_stays_hold(self):
        # kills: rewriting an existing HOLD into GO
        h = {"verdict": "HOLD", "reasons": [], "signals": {}}
        self.assertEqual(m.apply_mcp_trust(h, {"grade": m.MISMATCH})["verdict"], "HOLD")

    def test_unreachable_records_but_does_not_gate(self):
        # kills: gating on staleness -- the reading may be weeks old, and an
        # endpoint down when measured says nothing about a payment happening now
        v = m.apply_mcp_trust(self.go(), {"grade": m.UNREACHABLE})
        self.assertEqual(v["verdict"], "GO")
        self.assertFalse(v["signals"]["mcp_trust"]["gated"])

    def test_ready_and_unknown_are_no_ops(self):
        # kills: emitting a signal key for every payment, and gating unmeasured payees
        for g in (m.READY, m.UNKNOWN):
            v = m.apply_mcp_trust(self.go(), {"grade": g})
            self.assertEqual(v["verdict"], "GO")
            self.assertNotIn("mcp_trust", v["signals"])

    def test_none_signal_is_identity(self):
        # kills: raising or mutating when no reading covers this payee
        base = self.go()
        self.assertIs(m.apply_mcp_trust(base, None), base)

    def test_does_not_mutate_input(self):
        # kills: in-place mutation leaking into the caller's verdict object
        base = self.go()
        m.apply_mcp_trust(base, {"grade": m.MISMATCH})
        self.assertEqual(base["verdict"], "GO")
        self.assertEqual(base["signals"], {})

    def test_garbage_verdict_is_returned_unchanged(self):
        # kills: raising on a non-dict verdict -- fail-open is mandatory
        self.assertEqual(m.apply_mcp_trust("nope", {"grade": m.MISMATCH}), "nope")

    def test_unrecognized_grade_is_a_no_op(self):
        # kills: a future/typo grade silently gating payments
        v = m.apply_mcp_trust(self.go(), {"grade": "weird"})
        self.assertEqual(v["verdict"], "GO")
        self.assertNotIn("mcp_trust", v["signals"])


if __name__ == "__main__":
    unittest.main()
