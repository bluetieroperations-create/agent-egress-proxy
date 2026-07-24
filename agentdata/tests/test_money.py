import unittest

from agentdata.core import money


class TestMoney(unittest.TestCase):
    def test_to_atomic(self):
        self.assertEqual(money.to_atomic("0.20", 6), 200_000)
        self.assertEqual(money.to_atomic("1", 6), 1_000_000)
        self.assertEqual(money.to_atomic("0.000001", 6), 1)

    def test_no_float(self):
        with self.assertRaises(ValueError):
            money.to_atomic(0.2, 6)               # float rejected
        with self.assertRaises(ValueError):
            money.to_atomic("0.0000001", 6)       # excess precision refused

    def test_from_atomic_roundtrip(self):
        for s in ("0", "0.2", "1", "0.000001"):
            self.assertEqual(money.from_atomic(money.to_atomic(s, 6), 6), s)

    def test_parse_atomic_int(self):
        self.assertEqual(money.parse_atomic_int("200000"), 200_000)
        self.assertIsNone(money.parse_atomic_int("0.2"))
        self.assertIsNone(money.parse_atomic_int(True))
        self.assertIsNone(money.parse_atomic_int(-1))


if __name__ == "__main__":
    unittest.main()
