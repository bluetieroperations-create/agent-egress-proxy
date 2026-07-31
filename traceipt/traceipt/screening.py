"""
Compliance-bound receipts: prove a payment was SCREENED before it was made.

Everyone proves "this payment happened." Regulated payers also need to prove
"this payment was checked first" -- reputation + sanctions (OFAC) + risk ->
GO/HOLD/STOP -- BEFORE the money moved. A screening block binds that verdict
into the receipt: the receipt carries the verdict's digest (hash-first, so the
verdict's contents stay off-receipt), and the whole thing is signed, chained,
anchored, and selectively disclosable like any other receipt field.

The single artifact then proves the whole decide -> pay -> record chain, and it
is the one capability that needs BOTH a pre-payment verdict engine and a neutral
receipt layer -- no payment platform that only moves money can produce it.

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
    """Verify a compliance-bound receipt against the ACTUAL verdict object.
    Returns (ok, problems).

    Checks:
      * the receipt carries a screening block;
      * its verdict_hash equals the digest of the provided verdict (so the
        bound verdict cannot be swapped for a different one);
      * if a decided_at is present, it is no later than the receipt's issued_at
        (the decision was made before the payment was recorded -- the offline
        "screened before paid" ordering; the strong form additionally checks
        the verdict's /attest anchor timestamp predates the settlement block).
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
                        "(not screened before the payment was recorded)")
    return (not problems), problems
