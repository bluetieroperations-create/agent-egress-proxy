"""
Tests for keccak.py -- Ethereum Keccak-256. Vectors are canonical published
values; each kills a specific mutation.
"""
import unittest

from keccak import keccak256


class TestKeccak256(unittest.TestCase):
    def _hex(self, b):
        return keccak256(b).hex()

    def test_empty(self):
        # THE distinguishing vector: proves this is Keccak (0x01 pad), NOT SHA3.
        self.assertEqual(
            self._hex(b""),
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")

    def test_abc(self):
        self.assertEqual(
            self._hex(b"abc"),
            "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")

    def test_hello(self):
        self.assertEqual(
            self._hex(b"hello"),
            "1c8aff950685c2ed4bc3174f3472287b56d9517b9c948127319a09a7a36deac8")

    def test_differs_from_sha3(self):
        # hashlib.sha3_256 uses the 0x06 pad; Keccak uses 0x01 -> must differ.
        import hashlib
        self.assertNotEqual(keccak256(b""), hashlib.sha3_256(b"").digest())

    def test_multiblock(self):
        # > one 136-byte rate block: exercises multiple absorb rounds.
        self.assertEqual(len(keccak256(b"\xa3" * 200)), 32)
        self.assertEqual(
            keccak256(b"\xa3" * 200).hex(),
            "3a57666b048777f2c953dc4456f45a2588e1cb6f2da760122d530ac2ce607d4a")

    def test_rejects_non_bytes(self):
        with self.assertRaises(TypeError):
            keccak256("not bytes")


if __name__ == "__main__":
    unittest.main()
