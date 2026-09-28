#!/usr/bin/env python3
"""
seller_audit.py -- the seller-side "verified merchant" tier (EARNED, not paid).

A merchant submits its endpoint; Blackwall runs an AUDIT from signals it can check
itself -- endpoint readiness (readiness.py), on-chain settlement history, a
sanctions/known-bad screen, and price-fairness vs peers. If it PASSES, the merchant
earns a signed, EXPIRING attestation that LOWERS (never zeroes) its risk
contribution and lets it clear the thin-history gate faster.

Anti-corruption rules (this is the whole point -- do NOT become a pay-to-whitelist
credit-rating trap):
  * The fee is for the AUDIT + periodic RE-AUDIT, never a guaranteed pass. A
    sanctioned / poorly-configured / gouging / disputed merchant FAILS.
  * A passing badge grants a bounded trust FLOOR (<1.0), never immunity: the live
    sanctions, price-anomaly, budget, recipient- and payload-mismatch STOPs all
    still fire at verdict time regardless of the badge.
  * It waives the thin-*count* gate (the audit substitutes for organic volume) but
    NOT the Sybil / distinct-payer gate -- an audit cannot certify distinct real
    payers, so wash-trade defense is preserved.
  * Attestations EXPIRE (re-audit) and are REVOCABLE -- a merchant that later
    misbehaves loses the badge.

SIGNING IS Ed25519 (2026-09-15; was HMAC-SHA256). The envelope is byte-compatible
with `receipt_signer`'s -- {protected, payload, signature} over
canonical_json({"payload","protected"}) -- so ONE verifier implementation reads
verdict receipts, Traceipt receipts and merchant attestations, and the key is
already published at /jwks.json and
/.well-known/blackwall-receipt-key.json. `typ` is `blackwall-seller-attestation+json`,
distinct from the verdict label, because one key signs both claim types and the
label is the only thing separating "we vouch for this merchant" from "we judged
this payment" -- `verify_attestation` checks it FIRST for that reason.

TWO DEFECTS FIXED TOGETHER, because either alone is worse than both:
  1. HMAC IS SYMMETRIC. The badge's entire selling point is that a merchant can
     show it and a buyer can check it, and only the holder of the secret could do
     either. Anyone holding that secret could also forge one.
  2. `_audit_key` FELL BACK TO A COMMITTED KEY (`_DEV_AUDIT_KEY`, in the public
     repo). With BLACKWALL_AUDIT_KEY unset -- the shipped default -- any reader of
     GitHub could forge a badge granting a trust floor. `receipt_signer.py`
     documents exactly this lesson ("a receipt signed with a committed key is
     WORSE than none -- it looks verifiable"); this module never got it. There is
     now NO fallback: an unconfigured signer RAISES `AttestationUnavailable`
     rather than issuing an unsigned or dev-signed badge. That asymmetry with
     `ReceiptSigner.sign()` returning None is deliberate -- a verdict without a
     receipt is still a valid verdict, while an unsigned attestation is pure
     assertion that would still grant a floor.
  Unexploitable in production only because `seller_registry` is never bound in
  `serve_forever`, i.e. two defects cancelling. Wiring the registry without
  fixing the signing would have activated the forgeable badge, which is why this
  landed first.

FLOATS ARE EMITTED AS DECIMAL STRINGS (`_decimalize`). The HMAC version signed raw
floats and got away with it only because we were the only possible verifier;
floats have no canonical JSON form, so the moment the badge became independently
checkable the bytes had to be reproducible by someone else's encoder.
`canonical_json` refuses them outright. So `floor` travels as "0.850000";
blackwall.py applies it through `float()` and is unaffected.

Verification for the SEED HOLDER is a re-sign-and-compare, which is sound because
Ed25519 is DETERMINISTIC, and keeps this dependency-free (`cdp_auth`'s Ed25519 is
sign-only by design). A THIRD PARTY does not call `verify_attestation` at all:
they take the envelope plus the public key from /jwks.json and use any standard
Ed25519 implementation. `test_seller_audit.TestThirdPartyVerifiable` does exactly
that against `cryptography` -- a test that was impossible to write under HMAC,
which is the whole finding.

Pure + stdlib; the audit scorer and attestation logic are unit-tested.

LIMITATIONS (audited & accepted):
  * Post-audit drift. Between issuance and expiry a merchant's behavior can
    change. The controls are (a) short-ish TTL + periodic re-audit, (b) revocation,
    and (c) a verdict-time stale-badge guard: forecast() drops the floor if the
    LIVE dispute rate has risen past the audit bar. The floor is bounded (<1.0) and
    the live sanctions/anomaly/budget/recipient/payload STOPs always fire, so a
    stale badge can never turn a bad payment into a GO -- only speed up a good one.
  * The badge is only as good as the KEY DISTRIBUTION. A third party verifies
    against /jwks.json, so it is trusting that endpoint to be ours. Anchoring the
    attestation digest via Traceipt /attest (traceipt_attest.py) adds an
    independent timestamp -- a proof rather than a key -- and is the next step,
    not a substitute.
  * `seller_registry` IS STILL NOT BOUND in `serve_forever`, so the tier remains
    inert in production: nothing issues a badge over the wire and nothing
    consults one. Revocation is in-process only (`SellerRegistry._revoked`), so a
    revoked badge stays valid to every other process. Both are the next change.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os

# Trust FLOORS a passing grade grants. Both are > GO_REPUTATION_MIN (0.70) so a
# verified merchant can clear the reputation gate, but NEVER 1.0 -- residual risk
# stays and the live anomaly/sanctions/budget gates still bite.
GRADE_FLOORS = {"A": 0.85, "B": 0.72}

# Audit gates.
MAX_AUDIT_DISPUTE_RATE = 0.05     # > 5% disputes -> fail the audit outright
PEER_AUDIT_MAX_RATIO = 2.0        # priced >= 2x the peer market -> fail (gouging)
AUDIT_A_MAX_DISPUTE = 0.01        # grade A demands <= 1% disputes
AUDIT_A_MIN_SETTLEMENTS = 20      # grade A demands real confirmed history (== THIN)
AUDIT_A_MIN_DISTINCT = 3          # ...spanning >= MIN_DISTINCT_PAYERS

DEFAULT_TTL_SECONDS = 90 * 24 * 3600     # 90 days, then re-audit
_ATTEST_VERSION = 1


class AttestationUnavailable(RuntimeError):
    """Signing is not configured, so no attestation can be issued.

    Deliberately an EXCEPTION where `ReceiptSigner.sign` returns None. Omitting
    a receipt from a verdict is honest -- the verdict stands on its own. An
    UNSIGNED attestation has no content beyond "we vouch for this merchant", and
    it would flow into `SellerRegistry` and grant a trust floor on no evidence.
    """


# ---------------------------------------------------------------------------
# The audit scorer (pure).
# ---------------------------------------------------------------------------
def _fail(reason, criteria):
    return {"passed": False, "grade": None, "floor": None,
            "reasons": [reason], "criteria": criteria}


def run_audit(*, readiness=None, record=None, peer_ratio=None):
    """Score a merchant audit -> {passed, grade, floor, reasons, criteria}.

    `readiness` = score_readiness() output ({grade, score, ...}) or None.
    `record`    = the merchant's reputation record (settlement_count,
                  confirmed_settlement_count, distinct_payers, dispute_rate,
                  sanctioned, known_bad).
    `peer_ratio`= price vs the peer-group market (>=1.0), or None.

    EARNED: every gate must pass; a bad actor / unconfigured / gouging / disputed
    merchant FAILS. Grade A needs a 'ready' endpoint + real distinct-payer history +
    <=1% disputes; anything else that passes the gates is grade B."""
    record = record if isinstance(record, dict) else {}
    criteria = {}

    if record.get("sanctioned"):
        return _fail("counterparty is on a sanctions list", criteria)
    if record.get("known_bad"):
        return _fail("counterparty is a known-bad address", criteria)

    rgrade = (readiness or {}).get("grade")
    criteria["readiness_grade"] = rgrade
    criteria["readiness_score"] = (readiness or {}).get("score")
    if rgrade not in ("ready", "close"):
        # None (unauditable / unreachable) or 'needs_work' -> no badge.
        return _fail("endpoint readiness is %r -- not auditable/ready"
                     % (rgrade or "unknown"), criteria)

    dispute = float(record.get("dispute_rate") or 0.0)
    criteria["dispute_rate"] = dispute
    if dispute > MAX_AUDIT_DISPUTE_RATE:
        return _fail("dispute rate too high (%.1f%%)" % (dispute * 100), criteria)

    if peer_ratio is not None:
        criteria["peer_ratio"] = round(float(peer_ratio), 3)
        if float(peer_ratio) >= PEER_AUDIT_MAX_RATIO:
            return _fail("priced %.1fx the peer market -- gouging" % peer_ratio,
                         criteria)

    confirmed = record.get("confirmed_settlement_count")
    if confirmed is None:
        confirmed = record.get("settlement_count", 0) or 0
    distinct = record.get("distinct_payers")
    criteria["confirmed_settlements"] = confirmed
    criteria["distinct_payers"] = distinct

    strong_history = (confirmed >= AUDIT_A_MIN_SETTLEMENTS
                      and (distinct is None or distinct >= AUDIT_A_MIN_DISTINCT))
    if rgrade == "ready" and strong_history and dispute <= AUDIT_A_MAX_DISPUTE:
        grade, why = "A", ("ready endpoint, strong distinct-payer history, "
                           "<=1% disputes, sanctions-clear, fair pricing")
    else:
        grade, why = "B", ("endpoint auditable + sanctions-clear + fair pricing "
                           "(thinner history or 'close' readiness)")
    return {"passed": True, "grade": grade, "floor": GRADE_FLOORS[grade],
            "reasons": [why], "criteria": criteria}


# ---------------------------------------------------------------------------
# Attestations: signed, expiring, verifiable, revocable.
# ---------------------------------------------------------------------------
def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


#: Float formatting for signed claims. Matches `receipt_signer.build_claims`,
#: which emits `score` the same way and for the same reason.
_FLOAT_FMT = "%.6f"


def _decimalize(obj):
    """Recursively render floats as DECIMAL STRINGS.

    Floats have no canonical JSON form, so signing one risks a claim that will
    not re-verify on another platform -- `canonical_json` refuses them outright.
    The old HMAC attestation embedded raw floats (`floor`, and the `criteria`
    rates) and got away with it only because WE were the only possible verifier;
    the moment the badge became independently checkable, the same bytes had to
    be reproducible by someone else's JSON encoder. So this is not cosmetic: it
    is what makes third-party verification actually work.
    """
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return _FLOAT_FMT % obj
    if isinstance(obj, dict):
        return {k: _decimalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_decimalize(v) for v in obj]
    return obj


def attestation_signer(seed=None, environ=None, **kw):
    """A `ReceiptSigner` bound to the ATTESTATION typ.

    Same Ed25519 key as verdict receipts, so `/jwks.json` already publishes the
    verification key and a rotation covers both. That is safe ONLY because `typ`
    lives inside the signed `protected` header, so a verdict cannot be relabelled
    as an attestation -- which is why `verify_attestation` checks it explicitly.
    """
    import receipt_signer
    return receipt_signer.ReceiptSigner(
        seed=seed, environ=environ, typ=receipt_signer.ATTESTATION_TYP, **kw)


def sign_attestation(subject, audit_result, *, issued_at, ttl=DEFAULT_TTL_SECONDS,
                     signer=None):
    """Issue an Ed25519-signed attestation for a PASSING audit.

    Raises ValueError on a failed audit (you cannot attest a merchant that did
    not earn it) and AttestationUnavailable when signing is not configured.
    """
    if not audit_result or not audit_result.get("passed"):
        raise ValueError("cannot attest a merchant that failed the audit")
    if signer is None or not getattr(signer, "available", False):
        raise AttestationUnavailable(
            "no signing seed configured (BLACKWALL_SIGNING_SEED) -- refusing to "
            "issue an unsigned merchant attestation")
    core = _decimalize({
        "v": _ATTEST_VERSION,
        "subject": str(subject).lower(),
        "grade": audit_result["grade"],
        "floor": audit_result["floor"],
        "issued_at": int(issued_at),
        "expires_at": int(issued_at) + int(ttl),
        "criteria": audit_result.get("criteria") or {},
    })
    core["attestation_id"] = "sa_" + hashlib.sha256(
        _canonical(core)).hexdigest()[:24]
    return signer.sign(core)


def verify_envelope(attestation, signer=None):
    """(ok, reason) for the TIME-INDEPENDENT half: shape, typ and signature.

    Split out of `verify_attestation` because the signature check is a
    RE-SIGN-AND-COMPARE, and running it per request put an Ed25519 signing
    operation on the verdict hot path -- measured at 221.9ms on the pure-Python
    backend against a 0.109ms verdict, reachable by any anonymous caller. The
    signature protects against a tampered attestation FILE, which is a
    load-time concern: nothing mutates the in-memory envelope between entry and
    use, so verifying once on entry is equivalent. Expiry and revocation stay
    per-request in `credential_for`, because they are functions of the clock and
    of operator state rather than of the envelope.
    """
    try:
        import receipt_signer
        if not isinstance(attestation, dict):
            return False, "not an attestation"
        protected = attestation.get("protected")
        payload = attestation.get("payload")
        signature = attestation.get("signature")
        if not isinstance(protected, dict) or not isinstance(payload, dict) \
                or not isinstance(signature, str):
            return False, "not a signed envelope"
        if protected.get("typ") != receipt_signer.ATTESTATION_TYP:
            return False, "wrong typ %r -- not a seller attestation" % (
                protected.get("typ"),)
        if signer is None or not getattr(signer, "available", False):
            return False, "no signer to verify against"
        expected = signer.sign(dict(payload))
        if expected is None:
            return False, "no signer to verify against"
        if protected != expected["protected"]:
            return False, "protected header does not match"
        if not hmac.compare_digest(signature, expected["signature"]):
            return False, "bad signature"
        return True, "ok"
    except Exception as e:
        return False, "%s" % type(e).__name__


def check_window(payload, now, revoked=None):
    """(ok, reason) for the TIME- and STATE-dependent half. PURE, microseconds,
    safe to run on every verdict."""
    try:
        if int(now) >= int((payload or {}).get("expires_at", 0)):
            return False, "expired"
        revoked = revoked or set()
        if (payload or {}).get("attestation_id") in revoked \
                or (payload or {}).get("subject") in revoked:
            return False, "revoked"
        return True, "ok"
    except Exception as e:
        return False, "%s" % type(e).__name__


def verify_attestation(attestation, *, now, signer=None, revoked=None):
    """(ok, reason). Checks typ, signature, expiry and revocation. Never raises.

    Composes `verify_envelope` (shape/typ/signature) and `check_window`
    (expiry/revocation) so there is ONE implementation of each. Still the full
    public check -- third parties and the CLI use it; the VERDICT path uses
    `check_window` alone, because the envelope was already verified on entry.
    """
    ok, reason = verify_envelope(attestation, signer)
    if not ok:
        return False, reason
    return check_window((attestation or {}).get("payload"), now, revoked)


def describe_registry(registry):
    """(level, message) for the boot banner. PURE, so the branch is testable.

    Lived inline in `blackwall.main()`, where no unit test could reach it --
    mutation testing showed the "do not say ON" guard could be deleted with
    every test still green. Extracted per this repo's convention that the
    decision-critical part is a small pure function, rather than adding a
    subprocess test to reach a branch that should not have been buried.

    level is "warn" when the tier cannot do its job and "info" otherwise, so the
    caller picks the stream without re-deciding anything.
    """
    if registry is None:
        return "info", "verified-merchant tier OFF (not configured)"
    unusable = getattr(registry, "unusable", None)
    skips = list(getattr(registry, "skip_reasons", None) or [])
    loaded = getattr(registry, "loaded", 0)
    skipped = getattr(registry, "skipped", 0)
    try:
        revoked = registry.published_revocations()["count"]
    except Exception:
        revoked = 0
    if unusable:
        msg = ("verified-merchant tier CONFIGURED BUT INERT -- %s" % unusable)
        if skips:
            msg += " [%d skipped: %s]" % (skipped, "; ".join(skips))
        return "warn", msg
    msg = ("verified-merchant tier ON (%d badge(s) loaded, %d skipped, "
           "%d revoked)" % (loaded, skipped, revoked))
    if skips:
        return "warn", msg + " -- skipped: %s" % "; ".join(skips)
    return "info", msg


def load_registry(attestations_path=None, revocations_path=None, signer=None):
    """Build a SellerRegistry from committed artifacts. Returns (registry, error).

    FAIL-OPEN ON THE BADGE SIDE, FAIL-CLOSED ON THE REVOCATION SIDE, and the
    asymmetry is the whole point. A missing or garbled attestation file means no
    merchant gets a trust FLOOR -- strictly more conservative, so it degrades
    safely and returns an empty registry with a warning. An unreadable
    REVOCATION file is the opposite: proceeding would load badges while unable
    to see which were withdrawn, i.e. granting trust the operator had removed.
    That returns (None, error) so the caller disables the tier entirely rather
    than run it half-blind.

    `None` for both paths is not an error -- it means the tier is not
    configured, which is the shipped default.
    """
    if not attestations_path and not revocations_path:
        return None, None
    store = None
    if revocations_path:
        store = FileRevocationStore(revocations_path)
        try:
            store.all()
        except Exception as e:
            return None, ("revocation list %r unreadable (%s) -- refusing to "
                          "load badges we cannot check revocation for"
                          % (revocations_path, type(e).__name__))
    try:
        registry = SellerRegistry(signer=signer, revocations=store)
    except Exception as e:
        return None, "registry unavailable (%s)" % (e,)
    #: A registry whose SIGNER is unavailable can verify nothing and issue
    #: nothing, so every badge is skipped. It fails CLOSED -- no floor is ever
    #: granted -- but the boot banner used to announce it as ON regardless, and
    #: the skip COUNT points an operator at their badge file when the real cause
    #: is a missing seed. Measured at boot: a VALID badge with no
    #: BLACKWALL_SIGNING_SEED gave "tier ON (0 loaded, 1 skipped)", and with an
    #: empty file "ON (0 loaded, 0 skipped)", which reads as healthy.
    registry.unusable = None
    if not getattr(registry._signer, "available", False):
        import receipt_signer
        registry.unusable = (
            "no signing seed (%s) -- every badge will be skipped and no trust "
            "floor can be granted" % receipt_signer.ENV_SEED)
    #: WHY a badge was skipped, not just how many. "1 skipped" cannot
    #: distinguish an unverifiable signature from malformed JSON, and those have
    #: completely different fixes.
    registry.skip_reasons = []
    loaded, skipped = 0, 0
    if attestations_path and os.path.exists(attestations_path):
        try:
            with open(attestations_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        registry.add(json.loads(line))
                        loaded += 1
                    except Exception as e:
                        # Includes an envelope that FAILS VERIFICATION: `add`
                        # raises, so a tampered row is skipped and counted
                        # rather than stored. That is the whole reason the
                        # signature check moved to entry. The REASON is kept
                        # (bounded, de-duplicated) because the count alone sends
                        # an operator to the wrong file.
                        skipped += 1
                        reason = str(e)[:160] or type(e).__name__
                        if reason not in registry.skip_reasons \
                                and len(registry.skip_reasons) < 5:
                            registry.skip_reasons.append(reason)
        except Exception as e:
            return registry, ("attestation file %r unreadable (%s) -- no "
                              "merchant gets a floor"
                              % (attestations_path, type(e).__name__))
    registry.loaded = loaded
    registry.skipped = skipped
    return registry, None


# ---------------------------------------------------------------------------
# Revocation: durable, append-only, publishable.
# ---------------------------------------------------------------------------
_REVOKE_DOMAIN = "revoke:"


def normalize_revocation_key(subject_or_id):
    """Lowercase + strip. An EVM address arrives EIP-55-checksummed from a live
    402 and lowercase from a crawl -- the join that silently missed 64 of 69
    endpoints in `advertised_prices`. Here the same slip would let a revoked
    merchant read as trusted again just by changing capitalization."""
    return str(subject_or_id or "").strip().lower()


class RevocationNotConfigured(RuntimeError):
    """No revocation secret is set, so no revoke token can be minted or checked."""


def _revoke_key(environ=None):
    """The revocation secret, with NO committed fallback.

    AUDIT FINDING (medium): this used `blackwall._receipt_key()`, which fell back
    to a placeholder constant IN THE PUBLIC REPO when BLACKWALL_RECEIPT_KEY was
    unset. MEASURED: a token forged from that constant was accepted, so any
    reader of GitHub could strip any merchant's badge. That fallback is gone --
    `hmac_key` now owns the secret for every capability.
    Bounded by this module's monotonic-safety design (revocation only ever
    REMOVES trust, so it is merchant griefing rather than escalation), which is
    why it is medium and not high -- but it is the THIRD instance of this root
    cause here, after `_DEV_AUDIT_KEY` and the reason `receipt_signer` refuses
    to have one at all.
    """
    import hmac_key
    key, ephemeral = hmac_key.load_key(environ)
    if ephemeral:
        # REVOCATION IS THE ONE CAPABILITY THAT STILL REFUSES. The other two
        # degrade acceptably on an ephemeral key -- a rejected outcome report or
        # a rejected approval fails safe. A revoke token that works only until
        # the next redeploy is worse than none: an operator would mint one, hand
        # it to whoever does the revoking, and it would silently stop working,
        # which is precisely the situation where trust needs withdrawing.
        raise RevocationNotConfigured(
            "%s is not set -- refusing to mint a revoke token from an ephemeral "
            "per-process key, which would stop working at the next restart"
            % hmac_key.ENV)
    return key


def sign_revoke_token(subject_or_id, key=None, environ=None):
    """Capability token authorizing REVOCATION of one subject/attestation.

    PER-SUBJECT, not a single admin secret: a leaked token revokes exactly one
    merchant rather than the whole registry. Domain-separated with "revoke:" so
    a report token or an approval token can never revoke a badge -- the same
    reason `approvals.sign_approval_token` carries "approve:".

    Raises `RevocationNotConfigured` when no secret is set. Explicitly NOT the
    `_receipt_key()` dev fallback -- see `_revoke_key`.
    """
    if key is None:
        key = _revoke_key(environ)
    return hmac.new(key,
                    (_REVOKE_DOMAIN + normalize_revocation_key(subject_or_id)
                     ).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def verify_revoke_token(subject_or_id, token, key=None, environ=None):
    """Constant-time check. Never raises; anything unusable -- including an
    unconfigured secret -- is False, so the HTTP path gets a refusal and not a
    500."""
    if not token or not isinstance(token, str):
        return False
    try:
        return hmac.compare_digest(
            token, sign_revoke_token(subject_or_id, key, environ))
    except Exception:
        return False


class FileRevocationStore:
    """Append-only JSONL of revoked subjects and attestation ids.

    DURABILITY IS THE WHOLE POINT. `SellerRegistry._revoked` was in-memory, so a
    redeploy -- exactly when the process restarts -- RESTORED every revoked
    badge along with its trust floor. That is not a fail-open: fail-open means
    declining to add caution, while this actively GRANTS trust the operator had
    withdrawn. Bounded by the badge TTL rather than unbounded, which makes it
    easy to under-rate.

    FAILS CLOSED ON WRITE, deliberately opposite to `reachability_ledger`'s
    fail-soft: a diagnostic that cannot log should still answer, but a
    REVOCATION that silently does not persist leaves the operator believing
    trust was withdrawn when it was not.

    NO un-revoke. Restoring trust is a deliberate operator act on the file, not
    an API call, so every reachable path is monotonically safe: whoever holds a
    token can only REMOVE trust, never grant it.
    """

    def __init__(self, path):
        self.path = path

    def add(self, subject_or_id):
        key = normalize_revocation_key(subject_or_id)
        if not key:
            raise ValueError("cannot revoke an empty subject")
        line = json.dumps({"subject": key}, sort_keys=True) + "\n"
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        return key

    def all(self):
        """Every revoked key. A junk LINE is skipped -- one bad row must not
        erase the rest -- but an unreadable FILE raises rather than returning
        an empty set, because empty means "nobody is revoked", the most
        permissive possible answer to a question about withdrawn trust. A
        MISSING file is not unreadable: it means nothing has been revoked yet."""
        out = set()
        if not os.path.exists(self.path):
            return out
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                key = normalize_revocation_key((row or {}).get("subject"))
                if key:
                    out.add(key)
        return out


# ---------------------------------------------------------------------------
# Registry: what the verdict path consults ("is this merchant verified?").
# ---------------------------------------------------------------------------
class SellerRegistry:
    """Holds issued attestations by subject and answers `credential_for` at verdict
    time. In-memory + stdlib; a deployment can persist the attestation dicts."""

    def __init__(self, signer=None, revocations=None):
        self._signer = signer if signer is not None else attestation_signer()
        self._by_subject = {}     # subject(lower) -> attestation envelope
        self._store = revocations
        #: In-process cache of the durable set. Seeded from the store at
        #: construction so a restart does not resurrect a revoked badge.
        self._revoked = set(revocations.all()) if revocations is not None \
            else set()

    def issue(self, subject, audit_result, *, issued_at, ttl=DEFAULT_TTL_SECONDS):
        att = sign_attestation(subject, audit_result, issued_at=issued_at,
                               ttl=ttl, signer=self._signer)
        self._by_subject[att["payload"]["subject"]] = att
        return att

    def add(self, attestation):
        """Store a pre-issued attestation (e.g. loaded from disk).

        Keyed off the SIGNED payload, never off a caller-supplied top-level
        field: an envelope whose outer dict claimed a different subject than the
        one inside its own signature would otherwise be filed under the address
        the attacker chose.
        """
        ok, reason = verify_envelope(attestation, self._signer)
        if not ok:
            # THE SECURITY PROPERTY MOVED HERE, so entry must enforce it. Storing
            # an unverifiable envelope would leave the signature check nowhere at
            # all, which is strictly worse than the slow per-request version.
            raise ValueError("refusing to store an unverifiable attestation: %s"
                             % reason)
        payload = (attestation or {}).get("payload") or {}
        subject = payload.get("subject")
        if not subject:
            raise ValueError("attestation has no signed subject")
        self._by_subject[str(subject).lower()] = attestation

    def revoke(self, subject_or_id):
        """Revoke by subject address OR attestation_id. Durable when a store is
        configured; in-process only otherwise (durability is an upgrade, not a
        precondition). Persists BEFORE updating the cache, so a write failure
        raises rather than leaving the operator believing it stuck.

        Works for a subject with NO loaded badge -- pre-emptive revocation: the
        operator learns a merchant went bad and must be able to act before the
        next boot loads its attestation off disk."""
        key = normalize_revocation_key(subject_or_id)
        if not key:
            raise ValueError("cannot revoke an empty subject")
        if self._store is not None:
            self._store.add(key)
        self._revoked.add(key)
        return key

    def published_revocations(self):
        """The revocation list as a publishable document.

        The completion of third-party verifiability: `sign_attestation` makes a
        badge anyone can check, and a signed badge whose revocation nobody can
        see is only as good as its TTL. Returns a COPY -- handing out the
        internal set would let a caller mutate the registry through the
        published view."""
        return {"revoked": sorted(self._revoked), "count": len(self._revoked)}

    def credential_for(self, subject, now):
        """Return {grade, floor, attestation_id} for a VALID (signed, unexpired,
        unrevoked) badge on `subject`, else None."""
        att = self._by_subject.get(str(subject).lower())
        if att is None:
            return None
        # NO SIGNATURE CHECK HERE, deliberately: the envelope was verified once
        # by `add`/`issue`. See `verify_envelope` for the measurement that
        # forced this (221.9ms per verdict on the pure-Python backend).
        ok, _ = check_window(att.get("payload"), now, self._revoked)
        if not ok:
            return None
        payload = att["payload"]
        return {"grade": payload["grade"], "floor": payload["floor"],
                "attestation_id": payload["attestation_id"]}
