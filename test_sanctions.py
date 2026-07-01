"""
Tests for OFAC sanctions screening (the "superset of free" layer).

Run: python -m unittest test_sanctions.py -v
"""
import os
import tempfile
import unittest

import blackwall as bw
import sanctions as S

# SYNTHETIC test address (not a real sanctioned address) -- these tests verify
# the screening MECHANISM, not any specific designation. The real list is
# operator-supplied / fetched via `python sanctions.py`.
SANC = "0x000000000000000000000000000000000000dEaD"


class TestSanctionsList(unittest.TestCase):
    """
    Mutation notes:
      - case-sensitive membership -> test_case_insensitive FAILS.
      - count comments as addresses -> test_from_file FAILS.
    """
    def test_membership_case_insensitive(self):
        sl = S.SanctionsList([SANC])
        self.assertTrue(sl.is_sanctioned(SANC))
        self.assertTrue(sl.is_sanctioned(SANC.lower()))
        self.assertTrue(sl.is_sanctioned(SANC.upper()))
        self.assertFalse(sl.is_sanctioned("0x" + "1" * 40))

    def test_non_string_safe(self):
        sl = S.SanctionsList([SANC])
        self.assertFalse(sl.is_sanctioned(None))
        self.assertFalse(sl.is_sanctioned(12345))

    def test_from_file(self):
        path = os.path.join(tempfile.mkdtemp(), "s.txt")
        with open(path, "w") as f:
            f.write("# comment\n\n%s\n%s  # inline\n" % (SANC, "0x" + "a" * 40))
        sl = S.SanctionsList.from_file(path)
        self.assertEqual(len(sl), 2)
        self.assertTrue(sl.is_sanctioned(SANC))

    def test_missing_file_is_empty(self):
        sl = S.SanctionsList.from_file("/no/such/file")
        self.assertEqual(len(sl), 0)

    def test_from_file_rejects_non_address_junk(self):
        # H-1: from_file must validate like the refresh path (parse_sanctioned_
        # addresses), else junk inflates len() and makes sanctions_enabled()
        # advertise screening that screens nothing real. Mutation: from_file adds
        # unvalidated tokens -> this FAILS (len would be > 1).
        path = os.path.join(tempfile.mkdtemp(), "s.txt")
        with open(path, "w") as f:
            f.write("%s\n" % SANC)                              # 1 real address
            f.write("garbage-not-an-address\n")                 # junk
            f.write("DROP TABLE sanctions\n")                   # junk w/ spaces
            f.write("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa\n")     # BTC, not EVM
            f.write("0x%s\n" % ("z" * 40))                      # 0x + non-hex
        sl = S.SanctionsList.from_file(path)
        self.assertEqual(len(sl), 1)
        self.assertTrue(sl.is_sanctioned(SANC))


class TestScreeningSource(unittest.TestCase):
    class _Inner:
        def __init__(self, rec):
            self.rec = rec
        def lookup(self, cp):
            return dict(self.rec)

    def test_sanctioned_flag_set_and_stops(self):
        inner = self._Inner({"settlement_count": 1000,
                             "confirmed_settlement_count": 1000,
                             "distinct_payers": 50, "dispute_rate": 0.0,
                             "sanctioned": False, "_meta": {"known": True}})
        src = S.SanctionsScreeningSource(inner, S.SanctionsList([SANC]))
        # clean counterparty -> inner record unchanged
        self.assertFalse(src.lookup("0x" + "b" * 40)["sanctioned"])
        # sanctioned counterparty -> flagged
        self.assertTrue(src.lookup(SANC)["sanctioned"])
        # ...and that drives a STOP even with otherwise-perfect reputation.
        v = bw.decide_payment("0.09", src.lookup(SANC), ["0.09"] * 5,
                              counterparty=SANC)
        self.assertEqual(v["verdict"], "STOP")

    def test_does_not_clobber_inner_sanctioned_true(self):
        inner = self._Inner({"sanctioned": True})
        src = S.SanctionsScreeningSource(inner, S.SanctionsList([]))
        self.assertTrue(src.lookup("0xanything")["sanctioned"])


class TestDescriptorAdvertisesSuperset(unittest.TestCase):
    def test_screening_advertised(self):
        import discovery as D
        d = D.build_descriptor(sanctions_screening=True)
        self.assertIn("sanctions-ofac", d["screening"])
        self.assertEqual(d["signals"][0], "sanctions-ofac")
        # without screening, it's not claimed
        d2 = D.build_descriptor(sanctions_screening=False)
        self.assertEqual(d2["screening"], [])


class TestParseSanctionedAddresses(unittest.TestCase):
    """
    The pure parser for a plain-text OFAC list -- shared by from_file and the
    startup refresh, so a header line or a non-EVM chain entry can't poison the
    list.

    Mutation notes:
      - skip the is_evm_address filter -> test_ignores_non_evm FAILS.
      - stop stripping `#` comments -> test_ignores_comments FAILS.
      - return [] always -> test_extracts_addresses FAILS.
    """
    def test_extracts_addresses(self):
        text = ("0x0330070fd38ec3bb94f58fa55d40368271e9e54a\n"
                "0x08723392ed15743cc38513c4925f5e6be5c17243\n")
        got = S.parse_sanctioned_addresses(text)
        self.assertEqual(len(got), 2)
        self.assertIn("0x0330070fd38ec3bb94f58fa55d40368271e9e54a", got)

    def test_ignores_comments_and_blanks(self):
        text = ("# OFAC EVM addresses -- header\n"
                "\n"
                "0x0330070fd38ec3bb94f58fa55d40368271e9e54a  # inline note\n")
        got = S.parse_sanctioned_addresses(text)
        self.assertEqual(got, ["0x0330070fd38ec3bb94f58fa55d40368271e9e54a"])

    def test_ignores_non_evm(self):
        # a bitcoin address / garbage token must not enter an EVM sanctions list
        text = ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa\n"
                "not-an-address\n"
                "0x0330070fd38ec3bb94f58fa55d40368271e9e54a\n")
        got = S.parse_sanctioned_addresses(text)
        self.assertEqual(got, ["0x0330070fd38ec3bb94f58fa55d40368271e9e54a"])

    def test_empty_input(self):
        self.assertEqual(S.parse_sanctioned_addresses(""), [])
        self.assertEqual(S.parse_sanctioned_addresses(None), [])


class TestRefreshFromUrlIsBounded(unittest.TestCase):
    """M-1: the live fetch reads a remote body into memory. A hijacked/MITM'd
    upstream (or a CDN error page streaming megabytes) must NOT OOM the boot. The
    cap must RAISE rather than truncate: truncation could silently drop real
    sanctioned addresses (fail-open on the compliance axis)."""
    class _Resp:
        def __init__(self, data):
            self._d = data
        def read(self, n=-1):
            return self._d if n < 0 else self._d[:n]
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _patch(self, body):
        import urllib.request
        self._orig = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=15: self._Resp(body)
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", self._orig))

    def test_oversized_feed_raises(self):
        # Mutation: no size cap -> huge body accepted, no raise -> this FAILS.
        huge = ("\n".join(["0x" + "a" * 40] * 200_000)).encode()  # ~8MB
        self._patch(huge)
        with self.assertRaises(Exception):
            S.SanctionsList().refresh_from_url(max_bytes=1_000_000)

    def test_under_cap_feed_loads(self):
        self._patch(("0x" + "b" * 40 + "\n").encode())
        sl = S.SanctionsList()
        self.assertEqual(sl.refresh_from_url(max_bytes=1_000_000), 1)
        self.assertTrue(sl.is_sanctioned("0x" + "b" * 40))


class TestBackgroundRefresh(unittest.TestCase):
    """M-3: the refresh runs OFF the boot path so a slow feed can't block the
    socket bind / deploy healthcheck. Verifies it merges, fails open, and -- the
    key property -- returns immediately while a slow refresher is still running."""

    def test_merges_in_background(self):
        sl = S.SanctionsList()
        extra = "0x" + "c" * 40
        def fake(l):
            l.add(extra)
            return 1
        t = S.start_background_refresh(sl, refresher=fake)
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertTrue(sl.is_sanctioned(extra))

    def test_fail_open_on_error(self):
        sl = S.SanctionsList([SANC])
        def boom(l):
            raise RuntimeError("feed down")
        t = S.start_background_refresh(sl, refresher=boom)
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(sl), 1)           # unchanged, no crash
        self.assertTrue(sl.is_sanctioned(SANC))

    def test_does_not_block_caller(self):
        # The key M-3 property: the call RETURNS while a slow refresher is still
        # running -- boot is not blocked. Mutation: run the refresh synchronously
        # (not in a daemon thread) -> the call blocks on release.wait and this
        # test hangs / FAILS.
        import threading
        started, release = threading.Event(), threading.Event()
        sl = S.SanctionsList([SANC])
        def slow(l):
            started.set()
            release.wait(5)                    # simulate a slow / drip feed
            return 0
        t = S.start_background_refresh(sl, refresher=slow)
        self.assertTrue(started.wait(2))       # thread ran
        self.assertTrue(t.is_alive())          # still working -- yet we got here
        release.set()
        t.join(5)
        self.assertFalse(t.is_alive())


if __name__ == "__main__":
    unittest.main()
