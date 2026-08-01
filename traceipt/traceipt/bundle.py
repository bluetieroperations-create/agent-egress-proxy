"""
Verifiable lifecycle bundle: verify the whole decide -> pay -> record story as one
coherent set of W3C Verifiable Credentials, and package it as a W3C Verifiable
Presentation.

Individually each credential proves one thing (a signed risk verdict, a signed
settlement-verified receipt, a signed anchoring). What a verifier actually needs
is to confirm they are ONE coherent decision: the verdict a receipt was bound to
== the verdict issued == the verdict anchored, every credential authentic, and
(optionally) the verdict anchored before the payment settled. That is what
verify_lifecycle checks -- built on the issuer-bound vc.verify_vc.

A holder transmits the bundle as a W3C VerifiablePresentation (make_presentation);
each contained credential carries its own proof, so the unsigned presentation is
sufficient to verify. Pure + stdlib (uses vc.py + screening.py).
"""
from __future__ import annotations

from . import vc as _vc
from .screening import verify_anchored_before

VP_CONTEXT = "https://www.w3.org/ns/credentials/v2"


def make_presentation(credentials, *, holder: str | None = None,
                      vp_id: str | None = None) -> dict:
    """Bundle VCs into a W3C VerifiablePresentation. Unsigned: each contained VC
    carries its own data-integrity proof, which is what a verifier checks."""
    vp = {
        "@context": [VP_CONTEXT],
        "type": ["VerifiablePresentation"],
        "verifiableCredential": [c for c in credentials if c is not None],
    }
    if vp_id is not None:
        vp["id"] = vp_id
    if holder is not None:
        vp["holder"] = holder
    return vp


def _dig(obj, *path):
    cur = obj
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def verify_lifecycle(*, receipt_vc, verdict_vc=None, attestation_vc=None,
                     settlement_time=None, expected_issuers=None):
    """Verify a decide -> pay -> record bundle. Returns (ok, report).

    Checks:
      1. every supplied VC's proof -- authenticity incl. issuer binding, and an
         optional expected issuer per role (expected_issuers = {"receipt": did,
         "verdict": did, "attestation": did});
      2. the cross-credential linkage -- the verdict digest bound in the receipt
         == the verdict credential's digest == the anchored attestation's digest,
         so it is provably ONE decision, not three unrelated documents;
      3. optional trustless ordering -- the anchor time precedes the settlement
         block time (the real 'screened before paid'), if both are supplied.

    A missing verdict/attestation is allowed (a bare receipt still verifies); but
    if either is supplied, the receipt must be compliance-bound and the digests
    must match.
    """
    expected_issuers = expected_issuers or {}
    report = {"problems": [], "verified": {}, "links": {}}

    def _check(name, cred):
        if cred is None:
            return
        ok = _vc.verify_vc(cred, expected_issuer=expected_issuers.get(name))
        report["verified"][name] = ok
        if not ok:
            report["problems"].append(
                f"{name} VC failed verification (bad proof or unexpected issuer)")

    _check("receipt", receipt_vc)
    _check("verdict", verdict_vc)
    _check("attestation", attestation_vc)

    r_hash = _dig(receipt_vc, "credentialSubject", "commerce", "screening", "verdict_hash")
    v_hash = _dig(verdict_vc, "credentialSubject", "verdictHash")
    a_hash = _dig(attestation_vc, "credentialSubject", "digest")
    report["links"] = {"receiptVerdictHash": r_hash, "verdictHash": v_hash,
                       "attestationDigest": a_hash}

    if verdict_vc is not None or attestation_vc is not None:
        if r_hash is None:
            report["problems"].append(
                "receipt is not compliance-bound (no screening verdict hash) but "
                "a verdict/attestation was supplied")
        present = [h for h in (r_hash, v_hash, a_hash) if h is not None]
        if len(set(present)) > 1:
            report["problems"].append(
                "verdict digests do not match across receipt/verdict/attestation "
                "-- not one coherent decision")

    if attestation_vc is not None and settlement_time is not None:
        anchored_at = _dig(attestation_vc, "credentialSubject", "anchor", "anchoredAt")
        ord_ok, reason = verify_anchored_before(anchored_at, settlement_time)
        report["links"]["screenedBeforePaid"] = ord_ok
        if not ord_ok:
            report["problems"].append("ordering: " + reason)

    return (not report["problems"]), report


def verify_presentation(vp: dict, **kwargs):
    """Convenience: pull the receipt / verdict / attestation VCs out of a W3C
    VerifiablePresentation (by their credential type) and run verify_lifecycle."""
    creds = vp.get("verifiableCredential") or []
    by_type = {}
    for c in creds:
        for t in (c.get("type") or []):
            by_type.setdefault(t, c)
    return verify_lifecycle(
        receipt_vc=by_type.get(_vc.RECEIPT_TYPE),
        verdict_vc=by_type.get(_vc.VERDICT_TYPE),
        attestation_vc=by_type.get(_vc.ATTESTATION_TYPE),
        **kwargs)
