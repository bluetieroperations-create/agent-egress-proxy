"""Each test names the mutation it kills."""
import unittest
from pfas_exceedance import (limit_ugl, detected_value, peak_by_system,
                             over_limit, LIMIT_NGL)


def row(pwsid="A", cont="PFOA", sign="=", val="0.010", state="NC",
        name="TEST", size="L"):
    return {"PWSID": pwsid, "PWSName": name, "Size": size, "State": state,
            "Contaminant": cont, "AnalyticalResultsSign": sign,
            "AnalyticalResultValue": val}


class TestUnits(unittest.TestCase):
    def test_limit_is_converted_to_file_units(self):
        # kills: comparing µg/L readings against the ng/L limit -- off by
        # 1000x, and it silently reported ZERO exceedances on the first run
        self.assertAlmostEqual(limit_ugl("PFOA"), 0.004)
        self.assertAlmostEqual(limit_ugl("PFHxS"), 0.010)

    def test_unregulated_contaminant_has_no_limit(self):
        # kills: treating every measured chemical as regulated -- UCMR 5
        # covers 29 PFAS plus lithium; only five are regulated
        self.assertIsNone(limit_ugl("lithium"))
        self.assertIsNone(limit_ugl("PFTA"))


class TestDetection(unittest.TestCase):
    def test_non_detect_is_not_a_value(self):
        # kills: reading the "<" rows -- 97% of the file is non-detects and
        # counting them would put every system over the limit
        self.assertIsNone(detected_value(row(sign="<", val="")))

    def test_blank_value_is_not_a_value(self):
        self.assertIsNone(detected_value(row(sign="=", val="")))

    def test_detection_parses(self):
        self.assertAlmostEqual(detected_value(row(val="0.0123")), 0.0123)


class TestRollup(unittest.TestCase):
    def test_peak_is_the_maximum(self):
        # kills: taking the first or last reading -- compliance is judged on
        # the highest result, so a mean or a first-row read understates it
        peak, _ = peak_by_system([row(val="0.005"), row(val="0.020"),
                                  row(val="0.003")])
        self.assertAlmostEqual(peak["A"]["PFOA"], 0.020)

    def test_system_monitored_but_clean_is_still_counted(self):
        # kills: only recording systems with detections -- the denominator
        # would collapse and the exceedance rate would read ~100%
        _, meta = peak_by_system([row(sign="<", val="")])
        self.assertIn("A", meta)

    def test_clean_system_is_not_over_limit(self):
        peak, _ = peak_by_system([row(val="0.001")])
        self.assertEqual(over_limit(peak), {})

    def test_over_limit_flags_the_right_contaminant(self):
        peak, _ = peak_by_system([row(cont="PFOA", val="0.009"),
                                  row(cont="PFHxS", val="0.005")])
        bad = over_limit(peak)
        self.assertEqual(list(bad["A"]), ["PFOA"])  # PFHxS limit is 0.010

    def test_each_contaminant_uses_its_own_limit(self):
        # kills: applying one threshold to all five -- PFOA/PFOS are 4 ng/L
        # but PFHxS/PFNA/HFPO-DA are 10
        self.assertNotEqual(LIMIT_NGL["PFOA"], LIMIT_NGL["PFHxS"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
