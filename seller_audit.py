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


def verify_attestation(attestation, *, now, signer=None, revoked=None):
    """(ok, reason). Checks typ, signature, expiry and revocation. Never raises.

    THE SIGNATURE CHECK IS A RE-SIGN AND COMPARE, which works because Ed25519 is
    DETERMINISTIC: the same seed over the same bytes yields the same signature.
    That keeps this dependency-free -- `cdp_auth`'s Ed25519 is sign-only by
    design -- and is the seed holder's path. A THIRD PARTY does not use this
    function at all: they take the envelope and the public key from
    `/jwks.json` and verify with any standard Ed25519 implementation, which is
    the whole point of moving off HMAC.

    `typ` IS CHECKED FIRST and is not optional. One key signs verdicts and
    attestations, so a bare signature check would accept either for the other --
    and any anonymous caller can get a verdict signed.
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
        # Re-sign under OUR protected header and compare both halves, so a
        # tampered kid/typ cannot be smuggled through by signing the payload
        # alone.
        if protected != expected["protected"]:
            return False, "protected header does not match"
        if not hmac.compare_digest(signature, expected["signature"]):
            return False, "bad signature"
        if int(now) >= int(payload.get("expires_at", 0)):
            return False, "expired"
        revoked = revoked or set()
        if payload.get("attestation_id") in revoked \
                or payload.get("subject") in revoked:
            return False, "revoked"
        return True, "ok"
    except Exception as e:
        return False, "%s" % type(e).__name__


# ---------------------------------------------------------------------------
# Registry: what the verdict path consults ("is this merchant verified?").
# ---------------------------------------------------------------------------
class SellerRegistry:
    """Holds issued attestations by subject and answers `credential_for` at verdict
    time. In-memory + stdlib; a deployment can persist the attestation dicts."""

    def __init__(self, signer=None):
        self._signer = signer if signer is not None else attestation_signer()
        self._by_subject = {}     # subject(lower) -> attestation envelope
        self._revoked = set()     # attestation_ids and/or subjects

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
        payload = (attestation or {}).get("payload") or {}
        subject = payload.get("subject")
        if not subject:
            raise ValueError("attestation has no signed subject")
        self._by_subject[str(subject).lower()] = attestation

    def revoke(self, subject_or_id):
        """Revoke a badge by subject address OR attestation_id."""
        self._revoked.add(str(subject_or_id).lower()
                          if str(subject_or_id).startswith("0x") else subject_or_id)

    def credential_for(self, subject, now):
        """Return {grade, floor, attestation_id} for a VALID (signed, unexpired,
        unrevoked) badge on `subject`, else None."""
        att = self._by_subject.get(str(subject).lower())
        if att is None:
            return None
        ok, _ = verify_attestation(att, now=now, signer=self._signer,
                                   revoked=self._revoked)
        if not ok:
            return None
        payload = att["payload"]
        return {"grade": payload["grade"], "floor": payload["floor"],
                "attestation_id": payload["attestation_id"]}
