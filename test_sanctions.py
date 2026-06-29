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


if __name__ == "__main__":
    unittest.main()
