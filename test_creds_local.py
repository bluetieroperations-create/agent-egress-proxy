#!/usr/bin/env python3
"""
Tests for creds_local.load_creds -- the local credential loader for the cdp_*
probes. Security-critical: this file handles the operator's live CDP secret and
burner private key, so the contract is (1) never return/leak values, only key
NAMES; (2) never corrupt a value (base64 secrets end in '==' and contain '+'/'/');
(3) an explicit env var always wins over the file.

TDD-first with mutation notes: each test states the mutation it kills.
"""
import os
import tempfile
import unittest

from creds_local import load_creds


def _write(text, encoding="utf-8"):
    d = tempfile.mkdtemp()
    p = os.path.join(d, ".blackwall-creds")
    with open(p, "w", encoding=encoding) as f:
        f.write(text)
    return p


class LoadCredsTest(unittest.TestCase):
    def setUp(self):
        # Isolate: remember and clear the keys these tests touch.
        self._keys = ("CDP_API_KEY_ID", "CDP_API_KEY_SECRET", "BAZAAR_WALLET_KEY")
        self._saved = {k: os.environ.pop(k, None) for k in self._keys}

    def tearDown(self):
        for k in self._keys:
            os.environ.pop(k, None)
            if self._saved[k] is not None:
                os.environ[k] = self._saved[k]

    def test_returns_names_only_never_values(self):
        # Kills a mutation that appends VALUES (leak) instead of NAMES to `loaded`.
        secret = "Zx9+Qw3/Kp7Tn2mB4dEfGh=="  # synthetic base64 (no real-secret bytes)
        p = _write("CDP_API_KEY_SECRET=%s\n" % secret)
        loaded = load_creds(p)
        self.assertEqual(loaded, ["CDP_API_KEY_SECRET"])
        self.assertNotIn(secret, loaded)
        self.assertNotIn(secret, "".join(loaded))

    def test_base64_secret_with_padding_survives_intact(self):
        # Kills a mutation using split('=',1)[?] or rstrip('=') that would eat the
        # base64 '==' padding or split on the wrong '='. Real CDP secrets end '=='.
        secret = "Ab1+Cd2/Ef3gHij4KlmnopQrsT=="  # synthetic; exercises +, /, '==' padding
        p = _write("CDP_API_KEY_SECRET=%s\n" % secret)
        load_creds(p)
        self.assertEqual(os.environ["CDP_API_KEY_SECRET"], secret)

    def test_crlf_line_endings(self):
        # Kills a mutation dropping the .strip() that trims a trailing '\r'.
        secret = "abc/def+ghi=="
        p = _write("CDP_API_KEY_SECRET=%s\r\n" % secret)
        load_creds(p)
        self.assertEqual(os.environ["CDP_API_KEY_SECRET"], secret)

    def test_utf8_bom_does_not_corrupt_first_key(self):
        # REGRESSION (audit L-1): a BOM-prefixed file (PowerShell Out-File / Notepad
        # on Windows) must not turn the first key into '﻿CDP_API_KEY_ID'.
        # Kills the mutation encoding="utf-8" (no BOM stripping).
        p = _write("CDP_API_KEY_ID=abc-123-uuid\n", encoding="utf-8-sig")
        load_creds(p)
        self.assertEqual(os.environ.get("CDP_API_KEY_ID"), "abc-123-uuid")

    def test_explicit_env_var_wins_over_file(self):
        # Kills a mutation dropping `key not in os.environ` (file clobbers env).
        os.environ["CDP_API_KEY_ID"] = "from-env"
        p = _write("CDP_API_KEY_ID=from-file\n")
        loaded = load_creds(p)
        self.assertEqual(os.environ["CDP_API_KEY_ID"], "from-env")
        self.assertNotIn("CDP_API_KEY_ID", loaded)

    def test_empty_value_is_skipped(self):
        # Kills a mutation dropping the `val` truthiness check -- an unfilled
        # template line must behave like 'not set', not set the key to ''.
        p = _write("CDP_API_KEY_ID=\n")
        loaded = load_creds(p)
        self.assertNotIn("CDP_API_KEY_ID", os.environ)
        self.assertEqual(loaded, [])

    def test_comments_and_blanks_skipped(self):
        p = _write("# comment\n\nCDP_API_KEY_ID=x\n")
        loaded = load_creds(p)
        self.assertEqual(loaded, ["CDP_API_KEY_ID"])

    def test_quotes_stripped_but_base64_preserved(self):
        # A quoted value has its wrapping quotes removed; base64 alphabet contains
        # no quotes so the payload is untouched.
        p = _write('CDP_API_KEY_SECRET="Ab1+Cd2/Ef=="\n')
        load_creds(p)
        self.assertEqual(os.environ["CDP_API_KEY_SECRET"], "Ab1+Cd2/Ef==")

    def test_missing_file_is_noop(self):
        loaded = load_creds("/nonexistent/path/.blackwall-creds")
        self.assertEqual(loaded, [])


if __name__ == "__main__":
    unittest.main()
