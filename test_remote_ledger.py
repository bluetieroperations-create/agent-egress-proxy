"""Tests for remote_ledger.py -- the durable ENCRYPTED ledger mirror.

Convention (see CLAUDE.md): every test names the MUTATION it kills. The security
properties here are the ones that make it safe to put customer payment intent in
a third party's key-value store at all, so each gets its own test rather than
riding along inside an integration case.
"""
import json
import os
import tempfile
import threading
import time
import unittest

import remote_ledger as rl
from remote_ledger import (DurableEventLedger, LedgerCryptoError, UpstashBackend,
                           derive_key, load_key, seal, unseal)

KEY_HEX = "a" * 64
KEY = derive_key(bytes.fromhex(KEY_HEX))


def _tmp_ledger():
    return os.path.join(tempfile.mkdtemp(), "l.jsonl")

REC = {"kind": "verdict", "ts": "2026-09-06T00:00:00Z", "receipt_id": "r1",
       "counterparty": "0xF00d", "payer": "0xBeef", "amount": "5.00",
       "asset": "USDC", "chain": "base", "verdict": "GO", "score": 0.9}


# ===========================================================================
# Envelope / cipher
# ===========================================================================
class Cipher(unittest.TestCase):

    def test_roundtrip_is_lossless(self):
        """MUTATION: seal() returning the plaintext, or unseal() dropping fields."""
        self.assertEqual(unseal(KEY, seal(KEY, REC)), REC)

    def test_envelope_leaks_no_plaintext(self):
        """The WHOLE POINT. MUTATION: seal() base64-ing the record instead of
        encrypting it -- a b64 blob still round-trips, so only this test dies.

        SENTINELS ARE LONG ON PURPOSE. An earlier version of this test asserted
        that short tokens ("GO", "r1") were absent from the envelope, which is
        not a property of encryption at all: base64 of random ciphertext contains
        any given 2-character token often. Measured on this cipher, "GO" appeared
        by chance in 4.85% of blobs and "r1" in 5.30% -- about a 1-in-10 failure
        per suite run, and it duly failed. A flaky security test is worse than no
        test, because the first response to it is to stop believing it.
        """
        import base64
        rec = dict(REC, counterparty="0xCOUNTERPARTYSENTINEL0123456789ABCDEF",
                   payer="0xPAYERSENTINEL9876543210FEDCBA", amount="1234567.89",
                   receipt_id="RECEIPTSENTINEL-0001", asset="SENTINELASSET")
        sentinels = ["0xCOUNTERPARTYSENTINEL0123456789ABCDEF",
                     "0xPAYERSENTINEL9876543210FEDCBA", "1234567.89",
                     "RECEIPTSENTINEL-0001", "SENTINELASSET",
                     # field NAMES are long enough to be collision-free too
                     "counterparty", "receipt_id", "verdict"]
        # Repeat: a single sample cannot distinguish "encrypted" from "lucky".
        for _ in range(200):
            blob = seal(KEY, rec)
            body = base64.b64decode(blob.split(".")[2])
            for s in sentinels:
                self.assertNotIn(s, blob)
                self.assertNotIn(s.encode(), body)
        # ...and the sentinels really are in the plaintext, so the check is not
        # vacuously passing on values that were never there.
        self.assertEqual(unseal(KEY, seal(KEY, rec)), rec)
        for s in sentinels:
            self.assertIn(s, json.dumps(rec))

    def test_tampered_ciphertext_is_rejected(self):
        """MUTATION: using an unauthenticated mode (CTR) or swallowing InvalidTag.
        Without this, a KV provider (or anyone who breaches it) can rewrite a
        verdict's amount and we would replay their edit as our own history."""
        import base64
        pre, n, ct = seal(KEY, REC).split(".")
        raw = bytearray(base64.b64decode(ct))
        raw[0] ^= 0x01
        bad = "%s.%s.%s" % (pre, n, base64.b64encode(bytes(raw)).decode())
        with self.assertRaises(LedgerCryptoError):
            unseal(KEY, bad)

    def test_version_prefix_is_authenticated(self):
        """MUTATION: not passing the prefix as AAD -- then an attacker can relabel
        a BWL1 blob as a future BWL2 and pick which cipher we decrypt it with."""
        pre, n, ct = seal(KEY, REC).split(".")
        with self.assertRaises(LedgerCryptoError):
            unseal(KEY, "BWL2.%s.%s" % (n, ct))

    def test_nonce_is_random_per_seal(self):
        """MUTATION: a fixed/counter nonce. GCM nonce reuse under one key leaks
        the XOR of two plaintexts AND allows forgery -- catastrophic, silent."""
        blobs = {seal(KEY, REC).split(".")[1] for _ in range(50)}
        self.assertEqual(len(blobs), 50)

    def test_wrong_key_cannot_open(self):
        """MUTATION: unseal ignoring the tag / decrypting with a truncated key."""
        other = derive_key(bytes.fromhex("b" * 64))
        with self.assertRaises(LedgerCryptoError):
            unseal(other, seal(KEY, REC))

    def test_garbage_blob_raises_not_crashes(self):
        """MUTATION: letting an IndexError/binascii error escape as something the
        hydrate loop does not catch, aborting replay on one bad row."""
        for bad in ("", "nope", "BWL1.x", "BWL1..", "BWL1.!!.!!", "a.b.c.d"):
            with self.assertRaises(LedgerCryptoError):
                unseal(KEY, bad)


class BrokenCipher(unittest.TestCase):
    """A BROKEN native build is not a missing one. `cryptography` can import as
    metadata and still blow up on use (missing _cffi_backend), and that failure
    arrives as pyo3_runtime.PanicException -- a BaseException, not an Exception.
    Reproduced here with a BaseException subclass, because the real panic type
    is not importable on a healthy machine."""

    class Panic(BaseException):
        pass

    def test_base_exception_from_the_cipher_is_converted(self):
        """MUTATION: `except Exception` in _guard. The panic then escapes seal(),
        blows past _mirror's fail-open guard, and surfaces as a 500 on the
        payment path -- measured against a genuinely broken install."""
        def panics(*a, **k):
            raise self.Panic("native build is broken")
        with self.assertRaises(LedgerCryptoError):
            rl._guard(panics)

    def test_keyboard_interrupt_is_NOT_swallowed(self):
        """MUTATION: a bare `except BaseException` with no passthrough, which
        would make the service unkillable by Ctrl-C inside a cipher call."""
        def interrupt(*a, **k):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            rl._guard(interrupt)

    def test_a_broken_cipher_still_fails_OPEN_on_the_payment_path(self):
        """The property that matters: a verdict must still be served and still
        be written locally when the cipher is unusable.
        MUTATION: letting the panic propagate out of _append."""
        import tempfile as _tf
        path = os.path.join(_tf.mkdtemp(), "l.jsonl")
        led = DurableEventLedger(path, UpstashBackend("https://k", "t",
                                                      transport=FakeTransport()),
                                 KEY, mirror_async=False)
        orig = rl._aesgcm
        rl._aesgcm = lambda key: (_ for _ in ()).throw(self.Panic("broken"))
        try:
            led.record_verdict("r1", "0xA", "1.00", "GO")   # must not raise
        finally:
            rl._aesgcm = orig
        self.assertEqual(len(list(led.read_events())), 1)
        self.assertEqual(led.stats["mirror_failures"], 1)

    def test_ensure_cipher_round_trips_when_healthy(self):
        """MUTATION: ensure_cipher() returning without actually exercising the
        cipher -- a self-test that tests nothing is how this whole class of bug
        reaches production."""
        rl.ensure_cipher(KEY)                     # must not raise
        orig = rl._aesgcm
        rl._aesgcm = lambda key: (_ for _ in ()).throw(self.Panic("broken"))
        try:
            with self.assertRaises(LedgerCryptoError):
                rl.ensure_cipher(KEY)
        finally:
            rl._aesgcm = orig


class KeyLoading(unittest.TestCase):

    def test_accepts_hex_and_base64(self):
        """MUTATION: accepting only one encoding -- an operator pastes the other
        and mirroring silently refuses to start."""
        import base64
        raw = bytes(range(32))
        self.assertEqual(load_key(raw.hex()), derive_key(raw))
        self.assertEqual(load_key(base64.b64encode(raw).decode()), derive_key(raw))

    def test_rejects_short_or_malformed_loudly(self):
        """MUTATION: padding/truncating to 32 bytes instead of refusing. A short
        key that 'works' is the worst outcome -- it looks encrypted and is not."""
        for bad in ("", "abcd", "z" * 64, "a" * 63, "a" * 65):
            with self.assertRaises(ValueError):
                load_key(bad)

    def test_refuses_a_key_reused_from_another_secret(self):
        """MUTATION: dropping the domain check. Reusing the receipt SIGNING seed
        as the ledger key would let one compromise cover both, and receipt_signer
        already refuses the reverse direction for the same reason."""
        raw = bytes(range(32))
        with self.assertRaises(ValueError):
            load_key(raw.hex(), forbid=(raw.hex(),))

    def test_derive_key_is_domain_separated(self):
        """MUTATION: using the operator's secret directly as the AES key, so the
        same bytes used elsewhere produce the same key here."""
        raw = bytes(range(32))
        self.assertNotEqual(derive_key(raw), raw)
        self.assertEqual(len(derive_key(raw)), 32)


# ===========================================================================
# Backend
# ===========================================================================
class FakeTransport:
    """Records requests; replays scripted responses."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])
        self.list = []
        self.kv = {}

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers,
                           "body": json.loads(body.decode()), "timeout": timeout})
        if self.responses:
            return self.responses.pop(0)
        cmd = json.loads(body.decode())
        if cmd[0] == "RPUSH":
            self.list.append(cmd[2])
            return 200, json.dumps({"result": len(self.list)}).encode()
        if cmd[0] == "LRANGE":
            # Real Redis semantics: start/stop are INCLUSIVE and may be negative
            # (from the end). A fake that ignored them would let an off-by-one in
            # the tail slice pass, which is the bug this exists to catch.
            n = len(self.list)
            start, stop = int(cmd[2]), int(cmd[3])
            if start < 0:
                start = max(0, n + start)
            if stop < 0:
                stop = n + stop
            return 200, json.dumps(
                {"result": self.list[start:stop + 1]}).encode()
        if cmd[0] == "SET":
            self.kv[cmd[1]] = cmd[2]
            return 200, json.dumps({"result": "OK"}).encode()
        if cmd[0] == "GET":
            return 200, json.dumps({"result": self.kv.get(cmd[1])}).encode()
        return 200, json.dumps({"result": None}).encode()


class Backend(unittest.TestCase):

    def setUp(self):
        self.t = FakeTransport()
        self.b = UpstashBackend("https://kv.example.com", "tok",
                                list_key="bw:ledger", transport=self.t)

    def test_append_issues_rpush_with_bearer_auth(self):
        """MUTATION: wrong command, wrong arg order, or a missing/!Bearer token."""
        self.b.append("BLOB")
        c = self.t.calls[0]
        self.assertEqual(c["body"], ["RPUSH", "bw:ledger", "BLOB"])
        self.assertEqual(c["headers"]["Authorization"], "Bearer tok")

    def test_read_all_returns_append_order(self):
        """MUTATION: reversing, or using LPUSH/RPOP semantics that invert order."""
        for x in ("a", "b", "c"):
            self.b.append(x)
        self.assertEqual(self.b.read_all(), ["a", "b", "c"])
        self.assertEqual(self.t.calls[-1]["body"], ["LRANGE", "bw:ledger", "0", "-1"])

    def test_read_all_honors_limit_from_the_TAIL(self):
        """The cap must keep the NEWEST records: an unbounded LRANGE on a large
        list is what makes boot slow, but dropping the tail would discard the
        most recent history -- exactly what going_bad needs.
        MUTATION: slicing [:limit] (oldest) instead of a negative range."""
        for x in "abcdef":
            self.b.append(x)
        self.assertEqual(self.b.read_all(limit=2), ["e", "f"])
        self.assertEqual(self.t.calls[-1]["body"], ["LRANGE", "bw:ledger", "-2", "-1"])

    def test_non_2xx_raises(self):
        """MUTATION: returning [] on an error, which hydrate would read as 'no
        history' and silently start from empty."""
        t = FakeTransport(responses=[(500, b"boom")])
        b = UpstashBackend("https://kv.example.com", "tok", transport=t)
        with self.assertRaises(rl.RemoteLedgerError):
            b.read_all()

    def test_transport_exception_is_wrapped(self):
        """MUTATION: letting a raw URLError escape, which the fail-open callers
        do not catch (they catch RemoteLedgerError)."""
        def boom(*a, **k):
            raise OSError("network down")
        b = UpstashBackend("https://kv.example.com", "tok", transport=boom)
        with self.assertRaises(rl.RemoteLedgerError):
            b.append("x")


class ErrorBody(unittest.TestCase):
    """AUDIT FINDING, found while diagnosing a live mirror that wrote NOTHING.

    A Redis-REST store answers a COMMAND-level failure with HTTP 200 and an
    `error` field: a READ-ONLY token, NOPERM, WRONGTYPE, a quota refusal. The
    backend read only `result`, so every one of those became `None` -- and
    `append` ignores its return value, so `_mirror` counted the record MIRRORED
    and the startup banner kept saying ON while the store held nothing.
    """

    def test_error_body_raises_even_on_HTTP_200(self):
        """MUTATION: dropping the `error` check, or checking `in payload`
        instead of truthiness. This is the read-only-token case verbatim."""
        t = FakeTransport(responses=[(200, json.dumps(
            {"error": "ERR this instance is read-only"}).encode())])
        b = UpstashBackend("https://kv.example.com", "tok", transport=t)
        with self.assertRaises(rl.RemoteLedgerError) as cm:
            b.append("BLOB")
        # The message must name the command AND carry the store's own words --
        # "mirroring failed" without them sends an operator to the wrong place.
        self.assertIn("RPUSH", str(cm.exception))
        self.assertIn("read-only", str(cm.exception))

    def test_a_rejected_write_is_COUNTED_not_swallowed(self):
        """The property the finding is really about: the ledger must KNOW it
        did not persist. MUTATION: any path where a rejected RPUSH still lands
        in stats['mirrored']."""
        t = FakeTransport(responses=[(200, json.dumps({"error": "NOPERM"}).encode())
                                     for _ in range(rl.MIRROR_ATTEMPTS)])
        led = DurableEventLedger(_tmp_ledger(),
                                 UpstashBackend("https://k", "t", transport=t),
                                 KEY, mirror_async=False)
        led.record_verdict("r1", "0xA", "1.00", "GO")
        self.assertEqual(led.stats["mirrored"], 0)
        self.assertEqual(led.stats["mirror_failures"], 1)

    def test_an_error_body_does_NOT_break_the_payment_path(self):
        """RESTRAINT CONTROL. Raising harder must not become raising THROUGH:
        the verdict is already written locally and a KV refusal is not the
        payment path's problem. MUTATION: letting RemoteLedgerError escape
        _append."""
        t = FakeTransport(responses=[(200, json.dumps({"error": "NOPERM"}).encode())
                                     for _ in range(rl.MIRROR_ATTEMPTS)])
        path = _tmp_ledger()
        led = DurableEventLedger(path, UpstashBackend("https://k", "t", transport=t),
                                 KEY, mirror_async=False)
        led.record_verdict("r1", "0xA", "1.00", "GO")   # must not raise
        with open(path) as f:
            self.assertEqual(len(f.read().strip().splitlines()), 1)

    def test_a_result_of_null_is_still_a_valid_answer(self):
        """RESTRAINT CONTROL: GET on a missing key legitimately returns
        {"result": null}. MUTATION: treating a falsy `result` as an error,
        which would make every empty read look like a broken store."""
        t = FakeTransport(responses=[(200, json.dumps({"result": None}).encode())])
        b = UpstashBackend("https://kv.example.com", "tok", transport=t)
        self.assertEqual(b.read_all(), [])


class WriteProbe(unittest.TestCase):
    """`hydrate` only READS, so a read-only token boots clean and mirrors
    nothing. The probe is what turns that silent state into a message."""

    def test_probe_writes_to_a_SEPARATE_key_with_an_expiry(self):
        """MUTATION: probing by RPUSHing the log itself, which adds a row an
        operator has to explain and which never expires."""
        t = FakeTransport()
        b = UpstashBackend("https://k", "t", list_key="bw:ledger", transport=t)
        b.probe()
        cmds = [c["body"] for c in t.calls]
        self.assertEqual(cmds[0][0], "SET")
        self.assertEqual(cmds[0][1], "bw:ledger:probe")
        self.assertEqual(cmds[0][3:], ["EX", str(rl.PROBE_TTL_SECONDS)])
        self.assertNotIn("RPUSH", [c[0] for c in cmds])
        self.assertEqual(t.list, [])          # the log is untouched

    def test_probe_raises_when_the_store_refuses_the_write(self):
        """MUTATION: probing with a read command, which a read-only token
        passes -- the exact hole being closed."""
        t = FakeTransport(responses=[(200, json.dumps(
            {"error": "ERR read-only"}).encode())])
        with self.assertRaises(rl.RemoteLedgerError):
            UpstashBackend("https://k", "t", transport=t).probe()

    def test_probe_raises_when_the_value_does_not_read_back(self):
        """A store that accepts a write it cannot return is not one to trust
        with the only durable copy. MUTATION: dropping the read-back."""
        t = FakeTransport(responses=[(200, json.dumps({"result": "OK"}).encode()),
                                     (200, json.dumps({"result": "other"}).encode())])
        with self.assertRaises(rl.RemoteLedgerError):
            UpstashBackend("https://k", "t", transport=t).probe()

    def test_verify_writable_returns_the_reason_and_never_raises(self):
        """MUTATION: letting the probe's exception escape verify_writable, which
        would make a KV outage at boot crash the service -- strictly worse than
        the silent mirror it replaces."""
        t = FakeTransport(responses=[(200, json.dumps(
            {"error": "ERR read-only"}).encode())])
        led = DurableEventLedger(_tmp_ledger(),
                                 UpstashBackend("https://k", "t", transport=t),
                                 KEY, mirror_async=False)
        self.assertIn("read-only", led.verify_writable())

    def test_verify_writable_is_None_on_a_healthy_store(self):
        """MUTATION: returning a truthy value on success, which would print the
        NOT-WRITABLE warning on every healthy boot and train operators to
        ignore it."""
        led = DurableEventLedger(_tmp_ledger(),
                                 UpstashBackend("https://k", "t",
                                                transport=FakeTransport()),
                                 KEY, mirror_async=False)
        self.assertIsNone(led.verify_writable())

    def test_an_unprobeable_backend_is_not_a_failure(self):
        """The backend is duck-typed and injected. MUTATION: raising or
        returning a reason when `probe` is simply absent, which would warn on
        every test double and every future backend."""
        class Bare:
            def append(self, blob):
                pass

            def read_all(self, limit=None):
                return []
        led = DurableEventLedger(_tmp_ledger(), Bare(), KEY, mirror_async=False)
        self.assertIsNone(led.verify_writable())


class ResponseCap(unittest.TestCase):
    """AUDIT FINDING. The KV is a THIRD PARTY, and _urllib_transport read its
    response with an unbounded r.read() -- a compromised or merely broken store
    answering with a huge body would be buffered straight into memory on a 512MB
    box. http_util.py caps its reads for exactly this reason; this path did not.

    Exercised against a REAL server because the injected fake transport in the
    other tests bypasses _urllib_transport entirely -- the cap lives in the one
    function those tests never call.
    """

    def serve(self, nbytes):
        import threading as _t
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                blob = b'{"result":["' + b"A" * nbytes + b'"]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        _t.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d" % srv.server_address[1]

    def test_oversized_response_is_refused_not_buffered(self):
        """MUTATION: r.read() with no limit. The read succeeds, the process
        balloons, and on a small instance the durability feature is what kills
        the service it exists to protect."""
        url = self.serve(200_000)
        old = rl.MAX_RESPONSE_BYTES
        rl.MAX_RESPONSE_BYTES = 50_000
        try:
            b = UpstashBackend(url, "tok")
            with self.assertRaises(rl.RemoteLedgerError):
                b.read_all()
        finally:
            rl.MAX_RESPONSE_BYTES = old

    def test_normal_response_still_works_through_the_real_transport(self):
        """RESTRAINT CONTROL: the cap must not break the ordinary path. Without
        this, a cap set absurdly low would pass the test above and silently
        disable mirroring entirely."""
        url = self.serve(10)
        b = UpstashBackend(url, "tok")
        self.assertEqual(b.read_all(), ["A" * 10])


# ===========================================================================
# DurableEventLedger
# ===========================================================================
class Durable(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ledger.jsonl")
        self.t = FakeTransport()
        self.backend = UpstashBackend("https://kv.example.com", "tok", transport=self.t)

    def mk(self, **kw):
        kw.setdefault("mirror_async", False)
        return DurableEventLedger(self.path, self.backend, KEY, **kw)

    def test_record_writes_locally_and_mirrors_once(self):
        """MUTATION: dropping the mirror call (local-only = today's data loss), or
        mirroring twice (duplicate history inflates settlement_count)."""
        led = self.mk()
        led.record_verdict("r1", "0xF00d", "5.00", "GO")
        self.assertEqual(len(list(led.read_events())), 1)
        self.assertEqual(len(self.t.list), 1)

    def test_mirrored_payload_is_encrypted(self):
        """MUTATION: mirroring rec as plaintext JSON. The provider must never see
        counterparty/payer/amount -- that is the entire premise of this design."""
        led = self.mk()
        led.record_verdict("r1", "0xF00dCafe", "5.00", "GO")
        self.assertNotIn("0xF00dCafe", self.t.list[0])
        self.assertEqual(unseal(KEY, self.t.list[0])["counterparty"], "0xF00dCafe")

    def test_hydrate_restores_events_in_order(self):
        """MUTATION: hydrate writing nothing, or losing order."""
        src = self.mk()
        src.record_verdict("r1", "0xA", "1.00", "GO")
        src.record_verdict("r2", "0xB", "2.00", "HOLD")
        os.remove(self.path)                       # simulate the container reset

        led = self.mk()
        n = led.hydrate()
        self.assertEqual(n, 2)
        got = list(led.read_events())
        self.assertEqual([e["receipt_id"] for e in got], ["r1", "r2"])
        self.assertEqual(got[0]["counterparty"], "0xA")

    def test_hydrate_is_BYTE_exact_against_the_file_it_replaces(self):
        """Stronger than 'semantically equal': the restored file must diff clean
        against the original, so a restore is verifiable without parsing.
        MUTATION: seal() serializing with sort_keys=True -- every line still
        round-trips and every other test passes, but the bytes all change."""
        import hashlib
        src = self.mk()
        for i in range(4):
            src.record_verdict("r%d" % i, "0xA", "%d.50" % i, "GO")
            src.record_outcome("r%d" % i, "settled", settlement_tx="0xtx%d" % i)
        before = hashlib.sha256(open(self.path, "rb").read()).hexdigest()
        os.remove(self.path)

        led = self.mk()
        led.hydrate()
        after = hashlib.sha256(open(self.path, "rb").read()).hexdigest()
        self.assertEqual(after, before)

    def test_hydrate_does_NOT_re_mirror(self):
        """THE BUG THIS DESIGN INVITES. hydrate() must write the local file
        DIRECTLY, not through _append -- otherwise every restart re-pushes the
        whole history and the remote list grows 2^restarts.
        MUTATION: hydrate calling self._append / record_verdict."""
        src = self.mk()
        src.record_verdict("r1", "0xA", "1.00", "GO")
        src.record_verdict("r2", "0xB", "2.00", "GO")
        os.remove(self.path)
        before = len(self.t.list)

        led = self.mk()
        led.hydrate()
        self.assertEqual(len(self.t.list), before)

    def test_hydrate_is_idempotent_over_an_existing_file(self):
        """MUTATION: appending to the local file instead of replacing it -- a
        double hydrate would double every counterparty's settlement_count."""
        src = self.mk()
        src.record_verdict("r1", "0xA", "1.00", "GO")
        led = self.mk()
        led.hydrate()
        led.hydrate()
        self.assertEqual(len(list(led.read_events())), 1)

    def test_mirror_failure_never_breaks_a_verdict(self):
        """FAIL-OPEN, matching verdict_anchor.py. A KV outage must not turn into
        a 500 on the payment path.
        MUTATION: letting RemoteLedgerError propagate out of record_verdict."""
        def boom(*a, **k):
            raise OSError("down")
        led = DurableEventLedger(self.path,
                                 UpstashBackend("https://k", "t", transport=boom),
                                 KEY, mirror_async=False)
        led.record_verdict("r1", "0xA", "1.00", "GO")     # must not raise
        self.assertEqual(len(list(led.read_events())), 1)  # local write still happened
        self.assertEqual(led.stats["mirror_failures"], 1)

    def test_undecryptable_remote_row_is_skipped_not_fatal(self):
        """A row from a rotated key, or provider corruption, must not abort the
        whole replay and lose the rows after it.
        MUTATION: letting LedgerCryptoError escape the hydrate loop."""
        src = self.mk()
        src.record_verdict("r1", "0xA", "1.00", "GO")
        self.t.list.insert(0, "BWL1.garbage.garbage")
        src.record_verdict("r2", "0xB", "2.00", "GO")
        os.remove(self.path)

        led = self.mk()
        n = led.hydrate()
        self.assertEqual(n, 2)
        self.assertEqual(led.stats["undecryptable"], 1)
        self.assertEqual([e["receipt_id"] for e in led.read_events()], ["r1", "r2"])

    def test_hydrate_failure_leaves_the_ledger_usable(self):
        """MUTATION: raising out of hydrate at boot, which would crash-loop the
        service on a KV outage -- strictly worse than starting with no history."""
        def boom(*a, **k):
            raise OSError("down")
        led = DurableEventLedger(self.path,
                                 UpstashBackend("https://k", "t", transport=boom),
                                 KEY, mirror_async=False)
        self.assertEqual(led.hydrate(), 0)
        led.record_verdict("r1", "0xA", "1.00", "GO")
        self.assertEqual(len(list(led.read_events())), 1)

    def test_outcomes_survive_the_round_trip(self):
        """MUTATION: mirroring only verdict events. Outcomes ARE the labels --
        a ledger of verdicts with no outcomes has no dispute_rate at all."""
        src = self.mk()
        src.record_verdict("r1", "0xA", "1.00", "GO")
        src.record_outcome("r1", "settled", settlement_tx="0xdead")
        os.remove(self.path)
        led = self.mk()
        led.hydrate()
        kinds = [e["kind"] for e in led.read_events()]
        self.assertEqual(kinds, ["verdict", "outcome"])

    def test_reputation_survives_a_simulated_restart(self):
        """END TO END, and the reason any of this exists: the aggregate a
        counterparty earned must be identical after the container is wiped.
        MUTATION: any of the above, but this is the one that states the goal."""
        src = self.mk()
        for i in range(4):
            src.record_verdict("r%d" % i, "0xA", "1.00", "GO")
            src.record_outcome("r%d" % i, "settled", settlement_tx="0xtx%d" % i)
        before = src.aggregate()
        os.remove(self.path)

        led = self.mk()
        led.hydrate()
        self.assertEqual(led.aggregate(), before)
        self.assertTrue(before)          # guard: an empty dict would pass vacuously

    def test_hydrate_honors_max_hydrate_and_keeps_the_NEWEST(self):
        """The cap is what stops a cold start from replaying an unbounded list.
        It must also keep the NEWEST rows -- recency is exactly what the
        going_bad gate reads, so trimming the wrong end would be worse than not
        trimming at all.
        MUTATION: hydrate calling read_all() with no limit (found by mutation
        testing -- nothing else in this suite pinned it)."""
        src = self.mk()
        for i in range(5):
            src.record_verdict("r%d" % i, "0xA", "1.00", "GO")
        os.remove(self.path)

        led = self.mk(max_hydrate=2)
        self.assertEqual(led.hydrate(), 2)
        self.assertEqual([e["receipt_id"] for e in led.read_events()], ["r3", "r4"])

    def test_transient_failure_is_retried_and_lands_exactly_once(self):
        """At-least-once is the SAFE direction here: aggregate_counterparties
        dedupes settlements by tx hash (ledger.py:134), so a duplicate cannot
        inflate settlement_count, while a LOST outcome erases a dispute and makes
        a bad counterparty look better than it is.
        MUTATION: MIRROR_ATTEMPTS = 1 (give up on the first blip)."""
        state = {"n": 0}
        real = self.t

        def flaky(url, headers, body, timeout):
            state["n"] += 1
            if state["n"] == 1:
                raise OSError("transient")
            return real(url, headers, body, timeout)

        rl.MIRROR_BACKOFF = 0
        led = DurableEventLedger(self.path,
                                 UpstashBackend("https://k", "t", transport=flaky),
                                 KEY, mirror_async=False)
        led.record_verdict("r1", "0xA", "1.00", "GO")
        self.assertEqual(led.stats["mirrored"], 1)
        self.assertEqual(led.stats["mirror_failures"], 0)
        self.assertEqual(len(real.list), 1)      # exactly once, not twice

    def test_permanent_failure_gives_up_after_a_bounded_number_of_attempts(self):
        """MUTATION: an unbounded retry loop, which would wedge the mirror worker
        forever on a dead KV and silently stop mirroring everything behind it."""
        calls = {"n": 0}

        def dead(*a, **k):
            calls["n"] += 1
            raise OSError("down")

        rl.MIRROR_BACKOFF = 0
        led = DurableEventLedger(self.path,
                                 UpstashBackend("https://k", "t", transport=dead),
                                 KEY, mirror_async=False)
        led.record_verdict("r1", "0xA", "1.00", "GO")
        self.assertEqual(calls["n"], rl.MIRROR_ATTEMPTS)
        self.assertEqual(led.stats["mirror_failures"], 1)

    def test_backlog_overflow_drops_instead_of_blocking(self):
        """The durability feature must not become the thing that takes the
        service down. A full queue must not block record_verdict -- that call is
        on the payment path.
        MUTATION: an unbounded Queue, or put() instead of put_nowait()."""
        gate = threading.Event()

        def hang(*a, **k):
            gate.wait()               # worker is stuck; the queue fills behind it
            raise OSError("down")

        # Release the worker on a timer rather than after the loop, so the
        # BLOCKING-put mutant also terminates instead of deadlocking the suite --
        # it just misses the deadline, which is what fails it.
        threading.Timer(3.0, gate.set).start()

        led = DurableEventLedger(self.path,
                                 UpstashBackend("https://k", "t", transport=hang),
                                 KEY, mirror_async=True, queue_max=4)
        t0 = time.time()
        for i in range(60):
            led.record_verdict("r%d" % i, "0xA", "1.00", "GO")
        elapsed = time.time() - t0
        gate.set()
        self.assertLess(elapsed, 1.0, "record_verdict blocked on a full backlog")
        self.assertGreater(led.stats["dropped"], 0)
        # The LOCAL ledger is still complete -- dropping only ever affects the mirror.
        self.assertEqual(len(list(led.read_events())), 60)

    def test_async_mirror_preserves_order_and_drains_on_close(self):
        """MUTATION: spawning a thread per record (reorders under load) or not
        draining the queue on close (loses the tail on a graceful shutdown)."""
        led = DurableEventLedger(self.path, self.backend, KEY, mirror_async=True)
        for i in range(25):
            led.record_verdict("r%d" % i, "0xA", "1.00", "GO")
        led.close()
        self.assertEqual(len(self.t.list), 25)
        ids = [unseal(KEY, b)["receipt_id"] for b in self.t.list]
        self.assertEqual(ids, ["r%d" % i for i in range(25)])


if __name__ == "__main__":
    unittest.main()
