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

Signing mirrors blackwall.sign_receipt (HMAC-SHA256, canonical JSON), domain-
separated. Pure + stdlib; the audit scorer and attestation logic are unit-tested.

LIMITATIONS (audited & accepted):
  * Post-audit drift. Between issuance and expiry a merchant's behavior can
    change. The controls are (a) short-ish TTL + periodic re-audit, (b) revocation,
    and (c) a verdict-time stale-badge guard: forecast() drops the floor if the
    LIVE dispute rate has risen past the audit bar. The floor is bounded (<1.0) and
    the live sanctions/anomaly/budget/recipient/payload STOPs always fire, so a
    stale badge can never turn a bad payment into a GO -- only speed up a good one.
  * HMAC (shared-secret) attestations verify in-process against Blackwall's own
    key; they are NOT publicly verifiable. For third-party-verifiable badges, anchor
    the attestation via Traceipt /attest (traceipt_attest.py) -- proofs, not the key.
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
_ATTEST_DOMAIN = "seller-audit:v1:"
_DEV_AUDIT_KEY = b"blackwall-dev-audit-key-not-for-prod"


def _audit_key(key=None):
    if key is not None:
        return key if isinstance(key, bytes) else str(key).encode("utf-8")
    return os.environ.get("BLACKWALL_AUDIT_KEY", "").encode("utf-8") or _DEV_AUDIT_KEY


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


def _sign(core, key):
    return hmac.new(_audit_key(key), _ATTEST_DOMAIN.encode() + _canonical(core),
                    hashlib.sha256).hexdigest()


def sign_attestation(subject, audit_result, *, issued_at, ttl=DEFAULT_TTL_SECONDS,
                     key=None):
    """Issue a signed attestation for a PASSING audit. Raises on a failed audit
    (you cannot attest a merchant that didn't earn it)."""
    if not audit_result or not audit_result.get("passed"):
        raise ValueError("cannot attest a merchant that failed the audit")
    subj = str(subject).lower()
    core = {
        "v": _ATTEST_VERSION,
        "subject": subj,
        "grade": audit_result["grade"],
        "floor": audit_result["floor"],
        "issued_at": int(issued_at),
        "expires_at": int(issued_at) + int(ttl),
        "criteria": audit_result.get("criteria") or {},
    }
    att_id = "sa_" + hashlib.sha256(_canonical(core)).hexdigest()[:24]
    return dict(core, attestation_id=att_id, sig=_sign(core, key))


def verify_attestation(attestation, *, now, key=None, revoked=None):
    """(ok, reason). Checks the HMAC signature, expiry, and revocation (by
    attestation_id OR subject). Constant-time signature compare; never raises."""
    try:
        if not isinstance(attestation, dict):
            return False, "not an attestation"
        core = {k: attestation.get(k) for k in
                ("v", "subject", "grade", "floor", "issued_at", "expires_at",
                 "criteria")}
        sig = attestation.get("sig")
        if not isinstance(sig, str) or not hmac.compare_digest(sig, _sign(core, key)):
            return False, "bad signature"
        if int(now) >= int(attestation.get("expires_at", 0)):
            return False, "expired"
        revoked = revoked or set()
        if attestation.get("attestation_id") in revoked \
                or attestation.get("subject") in revoked:
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

    def __init__(self, key=None):
        self._key = key
        self._by_subject = {}     # subject(lower) -> attestation
        self._revoked = set()     # attestation_ids and/or subjects

    def issue(self, subject, audit_result, *, issued_at, ttl=DEFAULT_TTL_SECONDS):
        att = sign_attestation(subject, audit_result, issued_at=issued_at, ttl=ttl,
                               key=self._key)
        self._by_subject[att["subject"]] = att
        return att

    def add(self, attestation):
        """Store a pre-issued attestation (e.g. loaded from disk)."""
        self._by_subject[str(attestation.get("subject")).lower()] = attestation

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
        ok, _ = verify_attestation(att, now=now, key=self._key, revoked=self._revoked)
        if not ok:
            return None
        return {"grade": att["grade"], "floor": att["floor"],
                "attestation_id": att["attestation_id"]}
