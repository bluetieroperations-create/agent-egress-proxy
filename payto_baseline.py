"""
payto_baseline.py -- is this the recipient this endpoint has always used?

WHY THIS EXISTS
---------------
x402 v2 made `payTo` DYNAMIC. From the v2 launch notes: per-request routing "to
addresses, roles, or callback-based payout logic", and the field "is no longer
static -- it can change on every request based on input parameters". That is a
real feature for marketplaces and multi-tenant APIs, and it is also a new attack:
a compromised or hostile endpoint returns an attacker's wallet, the agent pays the
RIGHT PRICE to the WRONG PARTY, and no spending control sees it. The amount is
within budget, the asset is correct, the signature is valid.

THE ECOSYSTEM'S ANSWER, and why it is not enough. The published mitigations for
this attack are "implement recipient allowlists" and "log and alert on first-seen
payment addresses". That is the same gap `integrations/agentcore/` documents about
AWS and `integrations/lucid/` about Lucid: a STATIC LIST A HUMAN TYPED. An
allowlist cannot say a payee is a wash-trading Sybil ring, OFAC-sanctioned,
quoting 50x its category median, silent for 90 days, or not a possible address.
And "alert on a first-seen address" is a COLD-START PROBLEM STATED AS A
MITIGATION -- it fires on every legitimate new counterparty, which is how an
alert gets turned off.

Blackwall already scores whatever `payTo` arrives, per request; it never assumed a
stable recipient. What it could not say until now is the ENDPOINT-RELATIVE fact:
*this host has only ever been seen advertising one recipient, and this is not it.*
A cold-start HOLD is not that claim -- it clears the moment the swapped address
has any history at all, and it says nothing about the endpoint.

WHERE THE DATA COMES FROM, AND WHY THAT IS THE SECURITY PROPERTY
----------------------------------------------------------------
`ecosystem_scan` already writes `data/directory.json`: per payee, the resources it
advertises. Inverting that gives host -> {payees advertised there}. The index is
built ONCE AT BOOT from OUR OWN COMMITTED CRAWL -- never from the request, never
from a live fetch on the hot path, and NEVER LEARNED FROM TRAFFIC. That last point
is load-bearing: a baseline learned from requests would let an attacker teach us
their own address and then pay it. Same rule `advertised_prices.py` states.

CALIBRATED BEFORE SHIPPING IT ON, the way sybil_ring and EXCESSIVE_GATES
graduated. Measured on the committed corpus:

    266 payees, 514 distinct hosts
    hosts advertising >1 distinct payTo:  8  (1.6%)
      api.aidress.ai 6 · blockrun.ai 3 · x402.ottoai.services 2
      browser/embed/search.gedx402.com 2 each

So 506 of 514 hosts (98.4%) have exactly one recipient on record. The remaining
1.6% are genuine multi-tenant endpoints, and for them "not the one we saw" is
NORMAL OPERATION, not an attack -- they get `multi_payee` and are NEVER gated.
`test_payto_baseline` COMPUTES those figures from the artifact rather than
restating them, because a prevalence claim another session cannot reproduce is
worth nothing (the `payee_syntax` "0 of 558" correction).

DEFAULT OFF. `PAYTO_BASELINE_GATES` is False. 1.6% is the HOST-level false-flag
ceiling; the REQUEST-level rate cannot be derived from the corpus -- a single
high-traffic multi-tenant host could dominate live traffic while being one row
here. Advisory until measured on real requests, then flip one constant.

A HOST THAT ROTATES HAS NO BASELINE -- the one judgement call
------------------------------------------------------------
The hardest case is the one that LOOKS most like the attack: a known multi-payee
host names a recipient we have never seen. That is indistinguishable, from here,
from a marketplace onboarding a new tenant. A host that has DEMONSTRATED rotation
has no stable recipient to compare against, so this declines to judge and grades
`multi_payee`. Gating it would put `api.aidress.ai` -- which advertises six --
permanently on the wrong side of the gate. This is the `payee_syntax.invalid_hex`
discipline: record what you cannot defend gating on.

HOLD-ONLY, NEVER STOP. This is inference from our own crawl, not proof: the corpus
is a snapshot, a seller may legitimately rotate between crawls, and our crawl
depth is bounded. `sanctions.py` and payload-mismatch keep the STOP authority.
Monotonically conservative -- it only ever ADDS caution, never clears one, so a
familiar recipient is never treated as evidence the payment is safe.

FAIL-OPEN IN EVERY DIRECTION, because the alternative manufactures evidence
against innocent sellers (the `reachability_ledger` rule). An unknown host, a
missing artifact, a relative resource, an absent counterparty: all `unknown`, and
`unknown` never escalates. The live ecosystem is larger than 514 hosts, so most
real hosts ARE absent, and gating on absence would HOLD nearly everything.

HONEST LIMITS -- READ BEFORE RELYING ON THIS
--------------------------------------------
1. THE `resource` FIELD IS CLIENT-SUPPLIED, and that bounds what this can promise.
   It defends an HONEST agent against a HOSTILE ENDPOINT, which is exactly the v2
   attack. It does not defend against a lying client, and more sharply: if a
   caller forwards the `resource` value out of the 402 CHALLENGE rather than the
   URL it actually DIALED, the endpoint chooses the host key and can name an
   uncrawled host to reach `unknown`. Callers should pass the DIALED url.
   `x402.canonical_resource_url` exists because we learned this field is
   attacker-influenced on our own server; the same caution applies on the way in.
2. A SELLER CAN OPT OUT by advertising two payTos in its discovery document,
   becoming `multi_payee`. That is a real evasion and it is acceptable, because
   evading this gate returns the payee to the STATUS QUO -- cold-start HOLD,
   sanctions, price anomaly and the Sybil gates all still apply. The gate is
   strictly additive, so escaping it grants nothing.
3. A HOST IS NOT AN OPERATOR. Two unrelated businesses can share a host, and one
   business can span hosts (58 of 266 corpus payees are multi-host). Keying by
   host is right for this signal -- the claim is about the ENDPOINT -- but it is
   not a claim about ownership.

Pure + stdlib. Folded into `forecast` via `signals.payto_baseline`.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

#: The artifact `ecosystem_scan` writes. Overridable for tests/alternate corpora.
DEFAULT_INDEX_PATH = "data/directory.json"

#: Env var pointing at that artifact in a deploy.
ENV_INDEX_PATH = "BLACKWALL_PAYTO_INDEX"

#: REVERSIBILITY LOCK, default OFF -- see the module docstring. Flip to True only
#: once the false-HOLD rate has been measured on real request traffic, and back to
#: False to demote instantly. HOLD-only either way. Mirrors `SYBIL_RING_GATES`,
#: `EXCESSIVE_GATES`, `ISSUER_TRUST_GATES`, `SELL_TAX_GATES`.
PAYTO_BASELINE_GATES = False

#: A host with more than this many distinct advertised recipients has demonstrated
#: rotation and therefore has no stable baseline. One, deliberately: the corpus
#: says 98.4% of hosts sit at exactly one, and any larger value would start
#: vouching for recipients on hosts that visibly rotate.
MAX_STABLE_PAYEES = 1

#: How old the crawl artifact may be and still be allowed to GATE. A seller may
#: legitimately rotate its payout wallet, and a baseline older than this turns
#: that ordinary event into a false HOLD. Matched to `settlement_velocity`'s
#: STALE_DAYS reasoning: the corpus is refreshed weekly, so two missed refreshes
#: is the point at which it stops being current.
MAX_INDEX_AGE_DAYS = 21

#: Distinguishes "age not provided" from "age explicitly UNKNOWN". Without it
#: `age_days=None` -- a caller stating it cannot date its index -- fell through
#: to dating the SHIPPED corpus instead, so a source built from an INJECTED
#: index (an alternate corpus, a test fixture) inherited an unrelated file's
#: freshness and could gate on it. FOUND BY DATING `data/directory.json`: three
#: tests that meant "unknown" had been getting it implicitly from the shipped
#: corpus being undated, and would have started passing for the wrong reason
#: the moment it was dated. Same cross-artifact confusion `meta_path` prevents.
_UNSET = object()

OK = "ok"                       # the recipient is one this host advertises
UNEXPECTED = "unexpected"       # single-recipient host, different recipient -- GATES
MULTI_PAYEE = "multi_payee"     # host rotates recipients -- recorded, never gates
UNKNOWN = "unknown"             # no opinion -- never gates


def _safe_text(text, limit=24):
    """A short, LOG-SAFE rendering of an untrusted string.

    SEVENTH instance of the untrusted-echo class in this repo. The resource url
    is harvested from a stranger's own x402 advertisement and the counterparty is
    merchant-controlled; both land in `reasons[]`, which reaches plain-text logs,
    CLI reports and the seller portal's HTML. JSON escapes a newline; a terminal
    does not. `repr` minus its quotes makes control characters visible escapes and
    leaves ordinary addresses completely readable -- the same helper shape as
    `payee_syntax._safe_hint` and `approvals._safe_text`.
    """
    if not isinstance(text, str):
        return ""
    short = text if len(text) <= limit else text[:16] + "..." + text[-4:]
    return repr(short)[1:-1]


def host_key(url):
    """Pure: a resource url -> the host key used to join, or None.

    `urlsplit(...).hostname` rather than `.netloc`, for three reasons that are
    each a bug if missed:

      * USERINFO. `https://api.example.com@evil.test/x` has netloc
        `api.example.com@evil.test` and hostname `evil.test`. Taking netloc
        verbatim would hand the attacker's host the trusted host's baseline --
        the credential trick `seller_portal.safe_probe_url` already refuses.
      * CASE. DNS is case-insensitive; `.hostname` lowercases.
      * PORT. `.hostname` drops it, so both sides of the join agree. A port is a
        service on the same host, not a different operator.

    A trailing dot is stripped: `a.b.` and `a.b` resolve identically, and keeping
    both spellings would split one host's baseline in two.

    MEASURED, because the obvious assumption is wrong and a test of mine relied
    on it: `urlsplit` strips ONLY CR, LF and TAB from a url. NUL, ESC and DEL
    pass straight through into `.hostname`, so a control character CAN reach a
    host key from a request url and the sanitizer in `assess_payto` is
    LOAD-BEARING, not defense-in-depth. A COMPLETE ANSI sequence (`\x1b[31m`)
    happens to be refused one layer down -- the `[` makes urlsplit raise
    "Invalid IPv6 URL", which is caught here as fail-open -- but that is an
    accident of a bracket, not a guard this module owns, and a bare ESC has no
    bracket. Found by mutation testing, not by reading.
    """
    if not isinstance(url, str):
        return None
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:                      # malformed IPv6 literal, bad port
        return None
    if not host:
        return None
    host = host.rstrip(".")
    return host or None


def _payee_key(address):
    """Lowercase a payee for use as a join key.

    NOT cosmetic, and here it is a SAFETY property rather than a coverage one. A
    live 402 returns an EIP-55 CHECKSUMMED `payTo` while the crawl artifact stores
    lowercase -- the join that silently missed 64 of 69 live endpoints in
    `advertised_prices`. There it read as "no catalog data"; HERE the same miss
    would read as an ATTACK, flagging the real recipient of every EIP-55 endpoint
    in the ecosystem. Tested at corpus scale for exactly that reason.
    """
    if not isinstance(address, str):
        return None
    key = address.strip().lower()
    return key or None


def build_payto_index(records):
    """Pure: [directory record] -> {host: frozenset(payee_lower)}.

    Keyed by HOST, not by payee. The claim this module makes is about an
    ENDPOINT's recipient, so inverting it the other way round would lose the
    signal entirely -- and 58 of 266 corpus payees advertise on more than one
    host, each of which needs its own baseline.

    Tolerant of the artifact's shape: it is refreshed by crawling third parties,
    `resources` has been written as both strings and dicts, and a record missing a
    payee or resources contributes nothing rather than an empty-set key. An
    absent host must stay ABSENT, because absence is what `unknown` reads.
    """
    index = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        payee = _payee_key(record.get("payee"))
        if not payee:
            continue
        resources = record.get("resources")
        if not isinstance(resources, (list, tuple)):
            continue
        for resource in resources:
            if isinstance(resource, dict):
                resource = resource.get("url") or resource.get("resource")
            host = host_key(resource)
            if not host:
                continue
            index.setdefault(host, set()).add(payee)
    return {host: frozenset(payees) for host, payees in index.items()}


def load_payto_index(path=None):
    """Load the crawl artifact into an index. Fail-open: never raises.

    A container booted without the corpus must still serve verdicts, so a missing
    or corrupt artifact yields {} -- every lookup then answers `unknown`, which is
    exactly today's behaviour.
    """
    path = path or os.environ.get(ENV_INDEX_PATH) or DEFAULT_INDEX_PATH
    try:
        with open(path) as handle:
            records = json.load(handle)
    except (OSError, ValueError):
        return {}
    if isinstance(records, dict):
        records = records.get("payees") or records.get("directory") or []
    if not isinstance(records, list):
        return {}
    return build_payto_index(records)


def meta_path(path):
    """The sidecar beside a corpus file: `directory.json` -> `directory.meta.json`.

    Derived from the path the caller actually passed, never a fixed location, or
    an operator pointing `BLACKWALL_PAYTO_INDEX` at an alternate corpus would
    silently be dated by a different file's sidecar.
    """
    if not isinstance(path, str) or not path:
        return ""
    base = path[:-5] if path.endswith(".json") else path
    return base + ".meta.json"


def _sidecar_age(path, reference):
    """Read a CONTENT-PINNED sidecar. None unless it provably describes `path`.

    WHY A SIDECAR AT ALL: `data/directory.json` is a bare LIST read by five
    modules and only two of them tolerate a dict, so carrying `generated_at`
    inline would mean changing the artifact's shape under `billing_preflight`,
    `directory_liveness`, `seller_intel` and `seller_report`.

    WHY IT PINS THE CONTENT, which is the part that matters: dating the corpus
    is what makes the gate REACHABLE, so from here the date is safety-critical.
    A sidecar's own failure mode is the forgotten refresh -- regenerate the
    corpus, leave the sidecar, and the date now describes a file that no longer
    exists, handing a stale baseline permission to gate. That is strictly worse
    than having no date at all. So the age is trusted ONLY when the recorded
    sha256 matches the bytes on disk; a mismatch, a missing hash, or an
    unreadable sidecar all read `unknown`, which means no gate. The pairing is
    guaranteed by construction rather than by remembering.
    """
    import hashlib
    meta = meta_path(path)
    try:
        with open(meta) as handle:
            blob = json.load(handle)
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict):
        return None
    # No hash is not "close enough": without it a hand-written date gates.
    if blob.get("sha256") != digest:
        return None
    return _parse_stamp(blob.get("generated_at"), reference)


def _parse_stamp(stamp, reference):
    """An ISO-8601 instant -> age in days, or None. Never raises."""
    import datetime
    if not isinstance(stamp, str):
        return None
    try:
        when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max((reference - when).total_seconds() / 86400.0, 0.0)


def index_age_days(path=None, now=None):
    """How old is the crawl artifact, in days? None when it CANNOT BE KNOWN.

    TWO sources, checked in that order: a CONTENT-PINNED sidecar beside the file
    (see `_sidecar_age` -- this is how `data/directory.json`, a bare list, gets
    dated without changing a shape five modules read), or an explicit inline
    `generated_at` for a dict-shaped corpus (the form `asset_coverage.json`
    uses). Neither available -> None, which reads as stale and gates nothing.

    DELIBERATELY NOT `os.path.getmtime`, and this is the whole point of the
    function. A container clones the repo at build time, so every committed
    artifact's mtime is the BUILD date -- an 18-month-old corpus would read as
    minutes old, and the staleness guard would confirm freshness it had no
    evidence for. That is the `chain_backfill` lesson exactly (`age_days`
    INVERTED, so Bitrefill read as a 4-day-old merchant), and the same shape as
    `payee_syntax`'s "0 malformed" meaning 0 SEEN. An undated artifact is
    UNDATED; saying so is worth more than a number nobody can trust.
    """
    import datetime
    path = path or os.environ.get(ENV_INDEX_PATH) or DEFAULT_INDEX_PATH
    reference = now or datetime.datetime.now(datetime.timezone.utc)
    age = _sidecar_age(path, reference)
    if age is not None:
        return age
    try:
        with open(path) as handle:
            blob = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict):
        return None
    return _parse_stamp(blob.get("generated_at"), reference)


def assess_payto(resource, counterparty, index):
    """Is `counterparty` the recipient `resource`'s host has always advertised?

    Returns {"grade": ok|unexpected|multi_payee|unknown, "reasons": [str],
    "host": str|None, "expected": int}. Only `unexpected` gates, and only when
    the caller passes gate=True.

    NEVER raises: this runs on every request, on two fields the merchant
    influences.
    """
    out = {"grade": UNKNOWN, "reasons": [], "host": None, "expected": 0}
    host = host_key(resource)
    payee = _payee_key(counterparty)
    if not host or not payee or not isinstance(index, dict):
        return out

    advertised = index.get(host)
    if not advertised:
        # Absent from our crawl. NOT evidence of anything: the live ecosystem is
        # larger than the corpus, so most real hosts land here.
        return out

    out["host"] = _safe_text(host, limit=64)
    out["expected"] = len(advertised)

    if payee in advertised:
        out["grade"] = OK
        return out

    if len(advertised) > MAX_STABLE_PAYEES:
        # A host that visibly rotates recipients. Indistinguishable from a
        # marketplace onboarding a tenant -- see the docstring.
        out["grade"] = MULTI_PAYEE
        out["reasons"].append(
            "endpoint %s advertises %d different payment recipients in our "
            "crawl, so it has no single expected recipient -- recorded rather "
            "than gated" % (out["host"], len(advertised)))
        return out

    out["grade"] = UNEXPECTED
    (only,) = tuple(advertised)
    out["reasons"].append(
        "endpoint %s has only ever advertised %s as its payment recipient in "
        "our crawl, and this payment names %s -- x402 v2 allows a per-request "
        "payTo, so a swapped recipient is paid at the correct price to the "
        "wrong party"
        % (out["host"], _safe_text(only), _safe_text(counterparty)))
    return out


def apply_payto_baseline(verdict, signal, gate=None):
    """PURE fold: record `signals.payto_baseline`, escalate GO->HOLD on
    `unexpected` when the lock is on.

    CONSERVATIVE-ONLY -- never upgrades a verdict, never produces a STOP, never
    downgrades an existing STOP, and `unknown`/`multi_payee` never escalate.
    Non-mutating. Mirrors `apply_payee_syntax`.
    """
    if not isinstance(signal, dict):
        return verdict
    grade = signal.get("grade")
    if grade not in (OK, UNEXPECTED, MULTI_PAYEE, UNKNOWN):
        return verdict
    if not isinstance(verdict, dict):
        return verdict

    gate = PAYTO_BASELINE_GATES if gate is None else gate

    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    v["signals"]["payto_baseline"] = {
        "grade": grade,
        "host": signal.get("host"),
        "advertised_recipients": signal.get("expected", 0),
        "gated": bool(gate and grade == UNEXPECTED
                      and not signal.get("stale_baseline")),
        "stale_baseline": bool(signal.get("stale_baseline")),
    }

    # `list(...)` on a str would splay it into characters. Cannot happen through
    # `assess_payto`, but this fold is exported -- the same guard
    # `apply_payee_syntax` carries.
    existing = v.get("reasons")
    reasons = list(existing) if isinstance(existing, (list, tuple)) else []
    found = signal.get("reasons")
    reasons.extend(list(found) if isinstance(found, (list, tuple)) else [])

    if signal.get("stale_baseline"):
        # RECORD, NEVER GATE. The mismatch is still reported -- that is the
        # traffic the lock is calibrated on -- but a baseline we cannot date, or
        # one older than MAX_INDEX_AGE_DAYS, cannot distinguish an attacker's
        # swapped recipient from a seller who changed wallets last week.
        gate = False
    if grade == UNEXPECTED and gate and v.get("verdict") == "GO":
        v["verdict"] = "HOLD"
        reasons.append("escalated GO->HOLD: the payment recipient is not the one "
                       "this endpoint advertises")
    v["reasons"] = reasons
    return v


def write_meta(path, generated_at):
    """Write the content-pinned sidecar for the corpus at `path`.

    `generated_at` is REQUIRED and there is deliberately NO default to "now".
    Defaulting would let a caller date an artifact it did not generate, which
    manufactures exactly the lie `_sidecar_age`'s hash guard exists to catch --
    and unlike a forgotten refresh, that one would VERIFY, because the hash
    would match a file whose date is simply wrong. The hash protects the pairing
    of date to bytes; only the caller can vouch for the date itself.

    Returns the sidecar path. Raises on an unwritable location, because a
    silently-absent sidecar reads as "undated" -- safe, but it would look like
    the write succeeded.
    """
    import hashlib
    if not isinstance(generated_at, str) or not generated_at.strip():
        raise ValueError("generated_at is required -- refusing to date an "
                         "artifact with an assumed timestamp")
    with open(path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    target = meta_path(path)
    with open(target, "w") as handle:
        json.dump({"generated_at": generated_at.strip(), "sha256": digest},
                  handle, indent=2)
        handle.write("\n")
    return target


class PayToBaselineSource:
    """Precomputed host -> recipients snapshot, O(1) on the hot path.

    The `issuer_trust_gate` pattern: building the index walks the whole corpus, so
    it happens ONCE at boot and never per request. The index is READ-ONLY after
    construction -- `check` never writes to it, because a baseline learned from
    traffic would let an attacker teach us their address and then pay it.
    """

    def __init__(self, index=None, path=None, age_days=_UNSET, max_age_days=None):
        self.index = index if index is not None else load_payto_index(path)
        # `age_days=None` means "explicitly UNKNOWN" and is honoured as such;
        # only OMITTING it falls back to dating the file. See `_UNSET` -- treating
        # the two alike let an injected index inherit the shipped corpus's
        # freshness, which is a stale baseline gating on somebody else's date.
        self.age_days = (index_age_days(path) if age_days is _UNSET else age_days)
        self.max_age_days = (MAX_INDEX_AGE_DAYS if max_age_days is None
                             else max_age_days)

    @classmethod
    def from_path(cls, path=None):
        return cls(path=path)

    def __len__(self):
        return len(self.index)

    @property
    def stale(self):
        """True when the baseline may not be current -- INCLUDING when its age
        is unknown. Unknown is treated as stale on purpose: the alternative is
        gating on a corpus that could be any age, which is how a safety feature
        becomes the bug."""
        return self.age_days is None or self.age_days > self.max_age_days

    def check(self, resource, counterparty):
        """Fail-open: any unexpected input answers `unknown`, never raises."""
        try:
            out = assess_payto(resource, counterparty, self.index)
        except Exception:
            return {"grade": UNKNOWN, "reasons": [], "host": None,
                    "expected": 0, "stale_baseline": True}
        out["stale_baseline"] = self.stale
        if self.stale and out["grade"] == UNEXPECTED:
            out["reasons"] = list(out["reasons"]) + [
                "recorded only: the endpoint directory this compares against is "
                "%s, so a seller that changed payout wallets is indistinguishable "
                "from a swapped recipient"
                % ("undated" if self.age_days is None
                   else "%.0f days old" % self.age_days)]
        return out
