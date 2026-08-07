"""
Compliance-bound receipts: bind the pre-payment verdict that approved a payment
into the receipt itself.

Everyone proves "this payment happened." Regulated payers also want to show the
payment was checked first -- reputation + sanctions (OFAC) + risk -> GO/HOLD/STOP.
A screening block binds that verdict into the receipt: the receipt carries the
verdict's digest (hash-first, so the verdict's contents stay off-receipt), and
the whole thing is signed, chained, anchored, and selectively disclosable like
any other receipt field. This needs BOTH a pre-payment verdict engine and a
neutral receipt layer -- no payment platform that only moves money can produce it.

TWO DIFFERENT STRENGTHS -- do not conflate them:
  * BINDING (cryptographically strong): the receipt is bound to ONE exact
    verdict. `verify_screening` recomputes the digest and catches any swap. This
    holds against a lying caller.
  * ORDERING ("screened BEFORE paid"): the `decided_at` field is SELF-ASSERTED
    by whoever built the screening block -- a malicious payer can backdate it, so
    `verify_screening`'s decided_at check is only a self-consistency sanity check,
    NOT trustless proof of ordering. For trustless ordering, the verdict must be
    independently timestamped -- anchored via Traceipt `/attest` -- and the
    anchor time compared to the settlement's on-chain block time. Use
    `verify_anchored_before` with those two timestamps.

`verdict_digest` is byte-identical to Black_Wall's
`traceipt_attest.verdict_digest` (sort_keys, compact separators), so a verdict
Black_Wall anchors via /attest and one bound into a Traceipt receipt share the
exact same hash. Pure + stdlib.
"""
from __future__ import annotations

import hashlib
import json


def verdict_digest(verdict_obj) -> str:
    """`sha256:<64 hex>` digest of a verdict. Matches Black_Wall's
    traceipt_attest.verdict_digest exactly (json.dumps sort_keys + compact
    separators, default ensure_ascii) so the two ecosystems agree on the hash."""
    canon = json.dumps(verdict_obj, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canon).hexdigest()


def build_screening(verdict_obj, *, decision=None, screener=None,
                    decided_at=None, attestation_ref=None) -> dict:
    """Assemble a `commerce.screening` block binding `verdict_obj` to a payment.
    Only `verdict_hash` is required by the schema; the rest are optional
    human-facing context."""
    sc = {"verdict_hash": verdict_digest(verdict_obj)}
    if decision is not None:
        sc["decision"] = decision
    if screener is not None:
        sc["screener"] = screener
    if decided_at is not None:
        sc["decided_at"] = decided_at
    if attestation_ref is not None:
        sc["attestation_ref"] = attestation_ref
    return sc


def verify_screening(receipt_payload: dict, verdict_obj):
    """Verify the BINDING of a compliance-bound receipt to the ACTUAL verdict
    object. Returns (ok, problems). This is the cryptographically strong half.

    Checks:
      * the receipt carries a screening block;
      * its verdict_hash equals the digest of the provided verdict, so the bound
        verdict cannot be swapped for a different one (holds against a lying
        caller);
      * self-consistency: if `decided_at` is present it is not AFTER the
        receipt's issued_at. NOTE: decided_at is SELF-ASSERTED, so this catches
        an obviously-wrong claim but does NOT prove the decision truly preceded
        the payment -- for trustless ordering use verify_anchored_before against
        the verdict's /attest anchor time and the settlement block time.
    """
    problems: list[str] = []
    commerce = receipt_payload.get("commerce") or {}
    sc = commerce.get("screening")
    if not isinstance(sc, dict):
        return False, ["receipt has no screening block (not compliance-bound)"]
    if sc.get("verdict_hash") != verdict_digest(verdict_obj):
        problems.append("bound verdict_hash does not match the provided verdict "
                        "(the receipt was bound to a different verdict)")
    decided = sc.get("decided_at")
    issued = receipt_payload.get("issued_at")
    if decided and issued and decided > issued:
        problems.append("screening decided_at is AFTER the receipt issued_at "
                        "(self-asserted timestamp is not even self-consistent)")
    return (not problems), problems


def verify_anchored_before(anchor_time_iso, settlement_time_iso):
    """The TRUSTLESS ordering check: was the verdict independently timestamped
    (anchored via /attest) strictly before the payment settled on-chain?
    Returns (ok, reason). Both timestamps are UTC ISO-8601 ('...Z'); the caller
    supplies the verdict's anchor time (from Traceipt's /anchors or the
    attestation's anchor) and the settlement's on-chain block time. This is the
    real 'screened before paid' proof -- decided_at alone is self-asserted."""
    if not anchor_time_iso or not settlement_time_iso:
        return False, "need both the anchor time and the settlement block time"
    if anchor_time_iso < settlement_time_iso:
        return True, "verdict anchored before settlement"
    return False, ("verdict anchor is not before settlement "
                   f"(anchored {anchor_time_iso}, settled {settlement_time_iso})")
