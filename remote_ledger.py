"""
remote_ledger.py -- durable, ENCRYPTED mirror of the append-only verdict ledger.

WHY THIS EXISTS
---------------
On a platform with no persistent disk (a Render free instance, which also spins
down on inactivity), the container filesystem resets on every restart. Measured
on the live deploy: the SQLite reputation store is byte-identical to the
image-baked snapshot, because its ONLY writer -- reputation_store.ingest_from_chain
-- is gated behind BLACKWALL_INGEST, which the deploy sets to 0. So the 46k-row
store is READ-ONLY in production and fully reproducible from the image; it needs
no durability at all.

Everything the engine ACCUMULATES goes to exactly one place: the append-only
JSONL ledger (ledger.EventLedger). Every verdict lands there via record_verdict,
every outcome via record_outcome, and ledger.aggregate_counterparties folds it
back into reputation -- including the RECENCY-weighted recent_dispute_rate that
drives the `going_bad` gate. That file IS the moat, and today it evaporates on
every restart.

So the problem is not "persist a database". It is "persist an append-only log of
a few hundred bytes per verdict", which a free key-value store can hold.

WHY ENCRYPTED
-------------
A ledger record carries counterparty, payer, amount, asset, chain, resource,
agent_id, verdict and score -- that is a customer's payment intent, not public
chain data. Handing it in the clear to a third-party KV provider would leak the
query stream this project avoids leaking everywhere else (see readiness.py's
LocalReadinessSource, and rpc_node.py). Records are sealed with AES-256-GCM
before they leave the process; the provider stores opaque blobs and timestamps.

NO PURE-PYTHON CIPHER FALLBACK, DELIBERATELY
--------------------------------------------
AES-GCM is not in the stdlib, and this module does NOT hand-roll one. The repo
already paid for that lesson once: receipt_signer.py's pure-Python Ed25519 was
CORRECT (3/3 RFC 8032 vectors) and still shipped a 48x latency regression and a
variable-time leak that recovers the signing key. A cipher fallback here would be
worse, because `cryptography` is pip-installed in the image (Dockerfile:42) and
effectively required on a public deploy anyway -- so the fallback would never run
in production, i.e. it would be untested code on the one path that matters.

If `cryptography` is missing, mirroring REFUSES to enable and says so. It never
degrades to plaintext. A quiet downgrade that ships payment intent in the clear
because a dependency was absent is precisely the failure mode this codebase keeps
catching in itself.

FAIL-OPEN ON THE PAYMENT PATH
-----------------------------
A KV outage must never turn into a 500 on /v1/forecast-payment. The local file
stays authoritative for every read; the mirror is best-effort, serialized through
one worker thread (verdict_anchor.py's posture), and failures are counted, not
raised. A hydrate failure at boot leaves the service running with no history --
which reads as cold-start HOLDs, i.e. it fails SAFE.

STDLIB except for the cipher, which is imported lazily so this module still
imports without `cryptography` installed.
"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request

from ledger import EventLedger

# Envelope version. Bound as AES-GCM associated data so a blob cannot be
# relabelled into a future format and steered at a different cipher.
ENVELOPE_PREFIX = "BWL1"

# Domain separation: the AES key is DERIVED from the operator's secret rather
# than being it, so the same bytes used for another purpose never yield this key.
KEY_LABEL = b"blackwall-ledger-mirror-v1"

KEY_BYTES = 32
NONCE_BYTES = 12

# Bound on how much history a boot will replay. An unbounded LRANGE over a large
# list is what makes a cold start slow; the cap keeps the NEWEST rows, because
# recency is what going_bad reads.
DEFAULT_MAX_HYDRATE = 50000

# Retries for one record. See _mirror for why at-least-once is the safe
# direction here rather than at-most-once.
MIRROR_ATTEMPTS = 3
MIRROR_BACKOFF = 0.25

# Bound on the in-process mirror backlog. During a KV outage every record burns
# its full retry budget, so an UNBOUNDED queue would grow without limit and the
# durability feature would become the thing that OOMs the service it protects.
# Overflow drops the OLDEST pending record and is counted -- dropping is strictly
# better than blocking the payment path, which is what an unbounded put() on a
# full queue would eventually do.
DEFAULT_QUEUE_MAX = 10000

# Cap on a single KV response. The store is a THIRD PARTY: a compromised or
# merely broken one answering with an unbounded body would be read straight into
# memory on a 512MB box. http_util.py caps its reads for exactly this reason;
# this path did not, which was an omission rather than a decision. 64MB is far
# above a real ledger page (a record is a few hundred bytes) and far below what
# would exhaust the instance.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class RemoteLedgerError(Exception):
    """Any transport/protocol failure talking to the KV store. Callers on the
    payment path catch THIS and carry on."""


class LedgerCryptoError(RemoteLedgerError):
    """A blob could not be authenticated or decoded. Subclasses RemoteLedgerError
    so a hydrate loop catching the parent cannot miss it."""


# ===========================================================================
# Key handling + envelope  (PURE)
# ===========================================================================
def derive_key(secret):
    """Derive the AES-256 key from an operator secret. PURE.

    HMAC-SHA256 with a fixed label: cheap, standard, and it means a secret reused
    from somewhere else does not produce the same key here.
    """
    if not isinstance(secret, (bytes, bytearray)):
        raise TypeError("secret must be bytes")
    return hmac.new(bytes(secret), KEY_LABEL, hashlib.sha256).digest()


def _decode_secret(raw):
    """32 raw bytes from a 64-char hex or a base64 string. Anything else -> None."""
    s = (raw or "").strip()
    if len(s) == 2 * KEY_BYTES:
        try:
            return bytes.fromhex(s)
        except ValueError:
            pass
    try:
        b = base64.b64decode(s, validate=True)
        if len(b) == KEY_BYTES:
            return b
    except (binascii.Error, ValueError):
        pass
    return None


def load_key(raw, forbid=()):
    """Operator secret -> AES key, or raise ValueError LOUDLY.

    No padding, no truncation, no dev-key fallback. A key that is silently
    "fixed up" looks encrypted and is not, which is worse than refusing to start
    (receipt_signer.py makes the same call about a committed signing key).

    `forbid` holds other configured secrets (the receipt signing seed, the report
    HMAC key). Reusing one here would let a single compromise cover both.
    """
    secret = _decode_secret(raw)
    if secret is None:
        raise ValueError(
            "ledger key must be 32 bytes as 64 hex chars or base64; "
            "got %d character(s)" % len(raw or ""))
    for other in forbid:
        if other and _decode_secret(other) == secret:
            raise ValueError(
                "ledger key must differ from the other configured secrets "
                "(signing seed / receipt key) -- do not reuse one secret")
    return derive_key(secret)


# KeyboardInterrupt/SystemExit must never be swallowed by a cipher guard.
_PASSTHROUGH = (KeyboardInterrupt, SystemExit)

CIPHER_MISSING = ("ledger mirroring needs a working `cryptography` install for "
                  "AES-GCM (pip install -r requirements-signing.txt); refusing "
                  "to mirror payment records in plaintext")


def _guard(fn, *args):
    """Run a cipher call, converting even a BaseException into our error type.

    FOUND BY RUNNING IT, NOT BY READING IT: a BROKEN native `cryptography` build
    (importable metadata, unusable bindings -- e.g. a missing _cffi_backend)
    raises pyo3_runtime.PanicException, which derives from BaseException rather
    than Exception. A plain `except Exception` does NOT catch it, so it escaped
    every fail-open guard in this module and surfaced as a 500 on the payment
    path -- the exact failure this design promises cannot happen. A missing
    package raises ImportError and was handled; a broken one was not, and
    "installed" is not "working".
    """
    try:
        return fn(*args)
    except _PASSTHROUGH:
        raise
    except BaseException as e:
        raise LedgerCryptoError("%s [%s]" % (CIPHER_MISSING, type(e).__name__)) from e


def _aesgcm(key):
    """Lazy import so this module loads without `cryptography` installed."""
    def build():
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key)
    return _guard(build)


def ensure_cipher(key):
    """Prove the cipher actually WORKS, at boot, with a real round trip.

    Eager because the alternative is discovering it on the first paid request:
    mirroring would count a failure per record and quietly persist nothing while
    the banner still said it was ON. Raises LedgerCryptoError so the caller can
    refuse to start.
    """
    probe = {"kind": "cipher-probe"}
    if unseal(key, seal(key, probe)) != probe:
        raise LedgerCryptoError("cipher self-test did not round-trip")


def seal(key, rec, nonce=None):
    """Record dict -> "BWL1.<b64 nonce>.<b64 ciphertext>". PURE given `nonce`.

    A FRESH RANDOM NONCE PER RECORD. GCM nonce reuse under one key leaks the XOR
    of two plaintexts and permits forgery, silently -- so `nonce` is a seam for
    tests only and must never be pinned by a caller.
    """
    n = nonce if nonce is not None else os.urandom(NONCE_BYTES)
    # Same serialization as EventLedger._append (NOT sort_keys): that makes a
    # hydrate byte-exact against the file it replaces, so an operator can verify
    # a restore with `diff` instead of having to parse both sides. Nothing signs
    # this blob -- GCM covers integrity -- so a canonical form buys nothing here.
    body = json.dumps(rec, separators=(",", ":")).encode("utf-8")
    # The WHOLE cipher interaction inside one guard -- construction included.
    # Guarding only the operation would leave a panic raised while BUILDING the
    # cipher outside every fail-open path.
    ct = _guard(lambda: _aesgcm(key).encrypt(n, body, ENVELOPE_PREFIX.encode()))
    return "%s.%s.%s" % (ENVELOPE_PREFIX,
                         base64.b64encode(n).decode(),
                         base64.b64encode(ct).decode())


def unseal(key, blob):
    """Envelope -> record dict. Raises LedgerCryptoError on ANY failure.

    One exception type for tamper, truncation, wrong key, bad base64 and bad
    JSON alike, so the hydrate loop can skip a single bad row with one `except`
    instead of enumerating library-specific errors it might not have thought of.
    """
    parts = (blob or "").split(".")
    if len(parts) != 3 or parts[0] != ENVELOPE_PREFIX:
        raise LedgerCryptoError("unrecognized ledger envelope")
    try:
        nonce = base64.b64decode(parts[1], validate=True)
        ct = base64.b64decode(parts[2], validate=True)
    except (binascii.Error, ValueError) as e:
        raise LedgerCryptoError("undecodable ledger envelope") from e
    if len(nonce) != NONCE_BYTES:
        raise LedgerCryptoError("bad nonce length")
    try:
        body = _guard(lambda: _aesgcm(key).decrypt(nonce, ct, ENVELOPE_PREFIX.encode()))
        return json.loads(body.decode("utf-8"))
    except RemoteLedgerError:
        raise
    except Exception as e:
        raise LedgerCryptoError("ledger record failed authentication") from e


# ===========================================================================
# Backend: Upstash-style Redis REST (bearer token, JSON command array)
# ===========================================================================
def _urllib_transport(url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        # Read ONE byte past the cap so an oversized body is detected rather
        # than silently truncated into unparseable JSON.
        raw = r.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RemoteLedgerError(
                "kv response exceeds %d bytes; refusing to buffer it"
                % MAX_RESPONSE_BYTES)
        return r.status, raw


# Lifetime of the boot write-probe key. Long enough to survive its own read-back
# on a slow link, short enough that it is gone before anyone browsing the store
# wonders what it is.
PROBE_TTL_SECONDS = 60


class UpstashBackend:
    """Append-only log over a Redis-REST KV (RPUSH / LRANGE).

    The command-array form (`POST /` with body ["RPUSH", key, value]) is used
    rather than the path form (`/rpush/<key>/<value>`) so a value never has to
    survive URL escaping or a URL length limit -- our values are base64
    ciphertext and can be long.

    `transport` is injected so every test runs without network.
    """

    def __init__(self, base_url, token, list_key="blackwall:ledger",
                 transport=None, timeout=5.0):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token
        self.list_key = list_key
        self.timeout = timeout
        self._transport = transport or _urllib_transport

    def _cmd(self, *args):
        body = json.dumps([str(a) for a in args]).encode("utf-8")
        headers = {"Authorization": "Bearer %s" % self.token,
                   "Content-Type": "application/json"}
        try:
            status, raw = self._transport(self.base_url, headers, body, self.timeout)
        except RemoteLedgerError:
            raise
        except Exception as e:
            # Wrap EVERYTHING (URLError, socket.timeout, ssl errors, ...) so the
            # fail-open callers need to catch only RemoteLedgerError.
            raise RemoteLedgerError("kv transport failed: %s" % e) from e
        if not (200 <= int(status) < 300):
            raise RemoteLedgerError("kv store returned HTTP %s" % status)
        try:
            payload = json.loads(raw.decode("utf-8"))
            # AUDIT FINDING. A Redis-REST store reports a COMMAND-level failure
            # in the body, with HTTP 200: a READ-ONLY token, NOPERM, WRONGTYPE,
            # a quota refusal. Reading only `result` turned every one of those
            # into `None`, and `append` ignores its return value -- so _mirror
            # counted the record MIRRORED and the banner kept saying ON while
            # the store held nothing. Silent total data loss is precisely the
            # failure this module exists to prevent, so an error body raises.
            if isinstance(payload, dict) and payload.get("error"):
                raise RemoteLedgerError(
                    "kv store rejected %s: %s" % (args[0], payload["error"]))
            return payload.get("result")
        except RemoteLedgerError:
            raise
        except (ValueError, AttributeError, UnicodeDecodeError) as e:
            raise RemoteLedgerError("unparseable kv response") from e

    def append(self, blob):
        self._cmd("RPUSH", self.list_key, blob)

    def probe(self):
        """Prove the credential can WRITE. Raises RemoteLedgerError if it cannot.

        `hydrate` only READS, so a read-only token, a wrong database or a
        revoked write permission all boot perfectly cleanly and then mirror
        nothing -- the operator sees "mirror ON" and has no durability at all.
        One round trip on boot turns that into a loud, specific message.

        Deliberately a SEPARATE key with an expiry, never the log itself: a
        probe must not add a row an operator would later have to explain, and
        it must clean up after itself without a delete.
        """
        key = self.list_key + ":probe"
        token = base64.b64encode(os.urandom(9)).decode()
        self._cmd("SET", key, token, "EX", PROBE_TTL_SECONDS)
        got = self._cmd("GET", key)
        if got != token:
            # A store that accepts the write and cannot read it back is not one
            # to trust with the only durable copy of the ledger.
            raise RemoteLedgerError("kv write probe did not read back")

    def read_all(self, limit=None):
        """Oldest-to-newest. `limit` keeps the NEWEST `limit` rows via a negative
        start index -- dropping the tail instead would discard exactly the recent
        history the going_bad gate depends on."""
        start = "0" if not limit else str(-int(limit))
        res = self._cmd("LRANGE", self.list_key, start, "-1")
        if res is None:
            return []
        if not isinstance(res, list):
            raise RemoteLedgerError("kv LRANGE returned %s" % type(res).__name__)
        return [r if isinstance(r, str) else str(r) for r in res]


# ===========================================================================
# The ledger
# ===========================================================================
class DurableEventLedger(EventLedger):
    """EventLedger whose every append is also mirrored, encrypted, to a KV store.

    Overrides exactly two things -- the single write point (`_append`) and boot
    (`hydrate`). Everything downstream (read_events, aggregate,
    LedgerReputationSource, the going_bad gate) keeps reading the LOCAL file and
    is untouched.
    """

    def __init__(self, path, backend, key, mirror_async=True,
                 max_hydrate=DEFAULT_MAX_HYDRATE, logger=None,
                 queue_max=DEFAULT_QUEUE_MAX):
        super().__init__(path)
        self.backend = backend
        self.key = key
        self.max_hydrate = max_hydrate
        self._log = logger or (lambda msg: None)
        self.stats = {"mirrored": 0, "mirror_failures": 0,
                      "undecryptable": 0, "hydrated": 0, "dropped": 0}
        self._q = None
        self._worker = None
        if mirror_async:
            # ONE worker, not a thread per record: a thread per record reorders
            # under load and is unbounded under a burst.
            self._q = queue.Queue(maxsize=queue_max)
            self._worker = threading.Thread(target=self._drain, daemon=True)
            self._worker.start()

    # -- writing ----------------------------------------------------------
    def _mirror(self, rec):
        # AT-LEAST-ONCE, and the asymmetry is the reason. A DUPLICATE row is
        # harmless: aggregate_counterparties dedupes settlements by tx hash
        # (ledger.py:134, `if tx in a["confirmed_txs"]: continue`), so a repeat
        # cannot inflate settlement_count. A LOST row erases an outcome -- and a
        # missing dispute makes a bad counterparty look better than it is, which
        # is the unsafe direction. So a transient blip is retried.
        last = None
        for attempt in range(MIRROR_ATTEMPTS):
            try:
                self.backend.append(seal(self.key, rec))
                self.stats["mirrored"] += 1
                return
            except LedgerCryptoError:
                # Our own bug or a bad key -- retrying cannot help.
                self.stats["mirror_failures"] += 1
                self._log("ledger mirror failed to seal a record")
                return
            except Exception as e:
                last = e
                if attempt + 1 < MIRROR_ATTEMPTS:
                    time.sleep(MIRROR_BACKOFF * (2 ** attempt))
        # FAIL-OPEN. The verdict already succeeded locally; a KV outage is not
        # the payment path's problem. Counted so it is observable.
        self.stats["mirror_failures"] += 1
        self._log("ledger mirror failed after %d attempts: %s"
                  % (MIRROR_ATTEMPTS, last))

    def _drain(self):
        while True:
            rec = self._q.get()
            try:
                if rec is None:
                    return
                self._mirror(rec)
            finally:
                self._q.task_done()

    def _append(self, rec):
        # LOCAL FIRST and unconditionally: it is what every read uses, so a
        # mirror problem must never cost us the local row.
        super()._append(rec)
        if self._q is not None:
            # NEVER block the caller: this runs on the payment path.
            try:
                self._q.put_nowait(rec)
            except queue.Full:
                self.stats["dropped"] += 1
                self._log("ledger mirror backlog full; dropped a record")
        else:
            self._mirror(rec)

    # -- boot -------------------------------------------------------------
    def hydrate(self):
        """Replay the remote log into the local file. Returns rows restored.

        Writes the local file DIRECTLY rather than through _append. Going through
        _append would re-mirror everything it just read, so the remote list would
        grow by a factor of two on every restart.

        Replaces the file rather than appending to it, so a second hydrate cannot
        double every counterparty's settlement_count.

        Never raises: a KV outage at boot leaves the service running with no
        history, which reads as cold-start HOLDs -- fail-SAFE.
        """
        try:
            blobs = self.backend.read_all(limit=self.max_hydrate)
        except Exception as e:
            self._log("ledger hydrate failed (starting with local history): %s" % e)
            return 0
        recs = []
        for blob in blobs:
            try:
                recs.append(unseal(self.key, blob))
            except LedgerCryptoError:
                # A row from a rotated key or a corrupted value must not abort
                # the replay and lose every row after it.
                self.stats["undecryptable"] += 1
        tmp = self.path + ".hydrate"
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as f:
                for rec in recs:
                    f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            os.replace(tmp, self.path)      # atomic: no half-written ledger
        self.stats["hydrated"] = len(recs)
        if self.stats["undecryptable"]:
            self._log("ledger hydrate skipped %d undecryptable row(s)"
                      % self.stats["undecryptable"])
        return len(recs)

    def verify_writable(self):
        """Is the mirror actually able to persist? None if yes, else the reason.

        NEVER RAISES and never refuses the boot. A write probe failing may be a
        misconfigured token (permanent) or the KV being briefly down
        (transient), and one probe cannot tell them apart -- taking the payment
        path down over a third party's outage would be the worse error. So this
        REPORTS, loudly, and the caller decides what to print.

        A backend with no `probe` is not a failure: the injected fakes in tests
        and any future backend without a cheap round trip simply go unverified.
        """
        probe = getattr(self.backend, "probe", None)
        if probe is None:
            return None
        try:
            probe()
        except Exception as e:
            return str(e)
        return None

    # -- shutdown ---------------------------------------------------------
    def close(self, timeout=10.0):
        """Drain the mirror queue so a graceful shutdown does not lose the tail."""
        if self._q is None:
            return
        self._q.put(None)
        if self._worker is not None:
            self._worker.join(timeout)


# ===========================================================================
# Wiring
# ===========================================================================
def from_env(path, env=None, logger=None, forbid=()):
    """Build a DurableEventLedger from the environment, or return None.

    Enabled only when BLACKWALL_LEDGER_KV_URL, BLACKWALL_LEDGER_KV_TOKEN and
    BLACKWALL_LEDGER_KEY are ALL set. A misconfiguration raises rather than
    silently falling back to a local-only ledger -- an operator who set two of
    the three believes their data is durable.
    """
    env = os.environ if env is None else env
    url = env.get("BLACKWALL_LEDGER_KV_URL")
    token = env.get("BLACKWALL_LEDGER_KV_TOKEN")
    raw_key = env.get("BLACKWALL_LEDGER_KEY")
    if not any((url, token, raw_key)):
        return None
    missing = [n for n, v in (("BLACKWALL_LEDGER_KV_URL", url),
                              ("BLACKWALL_LEDGER_KV_TOKEN", token),
                              ("BLACKWALL_LEDGER_KEY", raw_key)) if not v]
    if missing:
        raise ValueError("ledger mirroring is partially configured; missing %s"
                         % ", ".join(missing))
    key = load_key(raw_key, forbid=forbid)
    # Fail at BOOT, not on the first paid request.
    ensure_cipher(key)
    backend = UpstashBackend(
        url, token,
        list_key=env.get("BLACKWALL_LEDGER_KV_KEY", "blackwall:ledger"),
        timeout=float(env.get("BLACKWALL_LEDGER_KV_TIMEOUT", "5")))
    return DurableEventLedger(
        path, backend, key, logger=logger,
        max_hydrate=int(env.get("BLACKWALL_LEDGER_MAX_HYDRATE",
                                str(DEFAULT_MAX_HYDRATE))))
