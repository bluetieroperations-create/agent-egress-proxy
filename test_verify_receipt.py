"""
Tests for verify_receipt.py -- the reference third-party CLI verifier.

The CLI's contract is what an automated integrator relies on: exit 0 MUST mean
"this is a trustworthy production attestation by Black_Wall". Each test names the
mutation it kills.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import receipt_sig as rs
import verify_receipt as vr

RFC_SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
DEV_SEED = "00" * 32


def _write(obj):
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj))
    return path


def _run(argv):
    """Run the CLI main, capturing (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = vr.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestValidProductionReceipt(unittest.TestCase):
    def test_real_key_receipt_is_valid_exit_0(self):
        sr = rs.build_signed_receipt(
            receipt_id="r", counterparty="0xabc", amount="4.50", asset="USDC",
            chain="base", verdict="GO", score=0.8, ts="t", seed_hex=RFC_SEED)
        path = _write(sr)
        self.addCleanup(os.remove, path)
        code, out, _ = _run(["--receipt", path, "--pubkey", rs.public_key_hex(RFC_SEED)])
        self.assertEqual(code, 0)
        self.assertIn("VALID", out)

    def test_tampered_receipt_is_invalid_exit_1(self):
        sr = rs.build_signed_receipt(
            receipt_id="r", counterparty="0xabc", amount="4.50", asset="USDC",
            chain="base", verdict="GO", score=0.8, ts="t", seed_hex=RFC_SEED)
        sr["verdict"] = "STOP"
        path = _write(sr)
        self.addCleanup(os.remove, path)
        code, out, _ = _run(["--receipt", path, "--pubkey", rs.public_key_hex(RFC_SEED)])
        self.assertEqual(code, 1)
        self.assertIn("INVALID", out)


class TestDevKeyReceiptIsNotTrusted(unittest.TestCase):
    """A dev-key receipt is FORGEABLE by anyone (the seed is public). The CLI must
    NOT report it as a trustworthy attestation via a success exit code."""

    def _forged(self):
        # Anyone can produce this -- the dev seed is built in and public.
        sr = rs.build_signed_receipt(
            receipt_id="forged", counterparty="0xATTACKER", amount="1000000",
            asset="USDC", chain="base", verdict="GO", score=1.0, ts="t",
            seed_hex=DEV_SEED)
        path = _write(sr)
        self.addCleanup(os.remove, path)
        return path

    def test_dev_key_receipt_does_not_exit_success(self):
        # mutation: exiting 0 (VALID) on a forgeable dev-key receipt -- an
        # automated `if verify_receipt ...` pipeline would then TRUST a forgery.
        path = self._forged()
        code, out, err = _run(["--receipt", path, "--pubkey", rs.public_key_hex(DEV_SEED)])
        self.assertNotEqual(code, 0)
        self.assertIn("DEV", (out + err).upper())

    def test_dev_key_can_be_explicitly_allowed(self):
        # An operator testing locally can opt in; then it verifies and exits 0,
        # but the FORGEABLE nature is still surfaced.
        path = self._forged()
        code, out, err = _run(["--receipt", path, "--allow-dev-key",
                               "--pubkey", rs.public_key_hex(DEV_SEED)])
        self.assertEqual(code, 0)
        self.assertIn("DEV", (out + err).upper())


if __name__ == "__main__":
    unittest.main()
