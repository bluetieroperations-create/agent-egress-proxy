"""
Tests for verdict_anchor.py -- the OPT-IN server-side auto-anchor hook.

Each test states the mutation it kills. The hook must (1) never leak the secret
report_token into the anchored digest, (2) be non-blocking + fail-open, and
(3) stay OFF unless BLACKWALL_ANCHOR is truthy.
"""
import os
import unittest

import verdict_anchor as VA


def _flag(name):
    """Stand-in for blackwall._env_flag (truthy only for 1/true/yes/on)."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _sync(fn):
    """A runner that executes inline -- makes the fire-and-forget path testable."""
    fn()


class TestAnchorableView(unittest.TestCase):
    """
    Mutation notes:
      - keep report_token -> test_drops_secret FAILS (secret would enter the digest).
      - drop receipt_id   -> test_keeps_public_handle FAILS (proof unlinkable).
    """
    RESP = {"verdict": "GO", "score": 0.99, "receipt_id": "bw_1",
            "report_token": "SECRET"}

    def test_drops_secret(self):
        view = VA.anchorable(self.RESP)
        self.assertNotIn("report_token", view)

    def test_keeps_public_handle(self):
        view = VA.anchorable(self.RESP)
        self.assertEqual(view["receipt_id"], "bw_1")
        self.assertEqual(view["verdict"], "GO")

    def test_non_dict_passthrough(self):
        self.assertEqual(VA.anchorable("x"), "x")


class TestSubmit(unittest.TestCase):
    """
    Mutation notes:
      - submit the raw response (with token) -> test_anchors_tokenless_view FAILS.
      - drop the spend cap -> test_passes_cap FAILS.
      - let a runner exception escape -> test_scheduling_failure_swallowed FAILS.
    """
    RESP = {"verdict": "STOP", "score": 0.0, "receipt_id": "bw_2",
            "report_token": "SECRET"}

    def _capture(self):
        calls = []

        def fake_anchor(base_url, verdict_obj, **kw):
            calls.append((base_url, verdict_obj, kw))
            return {"ok": True, "digest": "sha256:" + "a" * 64,
                    "attestation_id": "att_x"}
        return calls, fake_anchor

    def test_anchors_tokenless_view(self):
        calls, fake = self._capture()
        a = VA.VerdictAnchor("https://t", max_amount_atomic=1_000_000,
                             runner=_sync, _anchor=fake, log=lambda m: None)
        a.submit(self.RESP)
        self.assertEqual(len(calls), 1)
        _, view, _ = calls[0]
        self.assertNotIn("report_token", view)   # secret never anchored
        self.assertEqual(view["receipt_id"], "bw_2")

    def test_passes_cap_and_pay(self):
        calls, fake = self._capture()
        sentinel = lambda req: "PAY"
        a = VA.VerdictAnchor("https://t", pay=sentinel, max_amount_atomic=42,
                             runner=_sync, _anchor=fake, log=lambda m: None)
        a.submit(self.RESP)
        _, _, kw = calls[0]
        self.assertEqual(kw["max_amount_atomic"], 42)
        self.assertIs(kw["pay"], sentinel)

    def test_anchor_failure_swallowed(self):
        def boom(*a, **k):
            raise RuntimeError("network down")
        a = VA.VerdictAnchor("https://t", runner=_sync, _anchor=boom,
                             log=lambda m: None)
        a.submit(self.RESP)  # must NOT raise -- fail-open

    def test_scheduling_failure_swallowed(self):
        def bad_runner(fn):
            raise RuntimeError("no threads")
        a = VA.VerdictAnchor("https://t", runner=bad_runner,
                             _anchor=lambda *a, **k: {"ok": True},
                             log=lambda m: None)
        a.submit(self.RESP)  # even a broken runner must not break the caller


class TestFromEnv(unittest.TestCase):
    """
    Mutation notes:
      - ignore the flag (always build) -> test_disabled_by_default FAILS.
      - ignore BLACKWALL_ANCHOR_URL    -> test_reads_url FAILS.
      - crash on a bad cap             -> test_bad_cap_defaults FAILS.
    """
    ENV = ("BLACKWALL_ANCHOR", "BLACKWALL_ANCHOR_URL",
           "BLACKWALL_ANCHOR_MAX_ATOMIC", "SIGNER_PRIVATE_KEY")

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in self.ENV}

    def tearDown(self):
        for k in self.ENV:
            os.environ.pop(k, None)
            if self._saved[k] is not None:
                os.environ[k] = self._saved[k]

    def test_disabled_by_default(self):
        self.assertIsNone(VA.from_env(_flag))

    def test_disabled_when_zero(self):
        os.environ["BLACKWALL_ANCHOR"] = "0"   # the bool("0") trap
        self.assertIsNone(VA.from_env(_flag))

    def test_enabled_defaults(self):
        os.environ["BLACKWALL_ANCHOR"] = "1"
        a = VA.from_env(_flag, pay=None)
        self.assertIsNotNone(a)
        self.assertEqual(a.base_url, VA.DEFAULT_URL)
        self.assertEqual(a.max_amount_atomic, VA.DEFAULT_MAX_ATOMIC)

    def test_reads_url_and_cap(self):
        os.environ["BLACKWALL_ANCHOR"] = "true"
        os.environ["BLACKWALL_ANCHOR_URL"] = "https://my.traceipt"
        os.environ["BLACKWALL_ANCHOR_MAX_ATOMIC"] = "500000"
        a = VA.from_env(_flag, pay=None)
        self.assertEqual(a.base_url, "https://my.traceipt")
        self.assertEqual(a.max_amount_atomic, 500000)

    def test_bad_cap_defaults(self):
        os.environ["BLACKWALL_ANCHOR"] = "on"
        os.environ["BLACKWALL_ANCHOR_MAX_ATOMIC"] = "not-a-number"
        a = VA.from_env(_flag, pay=None)
        self.assertEqual(a.max_amount_atomic, VA.DEFAULT_MAX_ATOMIC)

    def test_pay_from_env_no_key(self):
        # no SIGNER_PRIVATE_KEY -> no signer (anchor will 402/fail-open unsigned)
        self.assertIsNone(VA.pay_from_env())


if __name__ == "__main__":
    unittest.main()
