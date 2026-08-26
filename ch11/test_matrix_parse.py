"""Each test names the mutation it kills."""
import unittest
from matrix_parse import creditor_name, normalize, parse_text, unwrap

ROW = "ACOUSTIC DESIGN, INC. 1003 MADISON STREET    PADUCAH KY 42001 USA"


class TestParse(unittest.TestCase):
    def test_splits_name_from_street(self):
        # kills: treating the whole head as a name, which glues the street on
        self.assertEqual(creditor_name(ROW), "ACOUSTIC DESIGN, INC")

    def test_po_box_is_an_address_start(self):
        # kills: dropping the PO-box marker -- measured, it left names as
        # "AIRGAS USA, LLC PO BOX 734672"
        line = "AIRGAS USA, LLC PO BOX  734672    TULSA OK 74136 USA"
        self.assertEqual(creditor_name(line), "AIRGAS USA, LLC")

    def test_one_is_not_an_address_start(self):
        # kills: adding "ONE" as a marker -- it truncated CAPITAL ONE to
        # CAPITAL in the first real run
        line = "CAPITAL ONE P.O BOX 71087    CHARLOTTE NC 28272 USA"
        self.assertEqual(creditor_name(line), "CAPITAL ONE")

    def test_name_starting_with_digits_survives(self):
        # kills: splitting on the FIRST digit run -- "24 HOURS CLOSING" and
        # "401(K) ADMINISTRATOR" are real creditor names
        line = "24 HOURS CLOSING 1320 MATTHEW MINT HILL RD    MATTHEWS NC 28105"
        self.assertEqual(creditor_name(line), "24 HOURS CLOSING")

    def test_redacted_individuals_skipped(self):
        # kills: emitting individuals -- they are privacy-redacted and are not
        # counterparties for a supplier graph
        self.assertIsNone(creditor_name("TRIPLETT, JORDAN ADDRESS ON FILE"))

    def test_boilerplate_rejected(self):
        # kills: accepting page furniture as creditors
        self.assertIsNone(creditor_name("Case 26-50236 Doc 182    DALLAS TX 75201"))

    def test_no_city_state_zip_is_not_a_row(self):
        # kills: parsing prose lines that merely contain digits
        self.assertIsNone(creditor_name("I am employed as a Case Manager by Epiq"))


class TestNormalize(unittest.TestCase):
    def test_suffixes_collapse(self):
        # kills: exact-string matching -- the same creditor is written
        # "C.H. ROBINSON INTERNATIONAL, INC" and "...WORLDWIDE, INC."
        self.assertEqual(normalize("AIRGAS USA, LLC"), normalize("Airgas USA Inc."))

    def test_distinct_firms_stay_distinct(self):
        # kills: over-normalizing until unrelated creditors merge
        self.assertNotEqual(normalize("ESTES EXPRESS LINES"),
                            normalize("ECHO GLOBAL LOGISTICS INC"))


class TestDocument(unittest.TestCase):
    def test_wrapped_rows_rejoin(self):
        # kills: dropping the unwrap step -- the country tail wraps and would
        # split one creditor across two unparseable lines
        t = "ACME CO 1 MAIN ST    ERIE PA 16501 UNITED STATES OF\nAMERICA"
        self.assertIn("ACME CO", parse_text(t))

    def test_duplicates_collapse(self):
        # kills: returning raw rows -- the same creditor appears on multiple
        # service lists within one filing
        t = (ROW + "\n" + ROW.replace("INC.", "INCORPORATED"))
        self.assertEqual(len(parse_text(t)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
