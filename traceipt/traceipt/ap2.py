"""
AP2 linkage: bind a Traceipt receipt to the AP2 Mandate that authorized the payment.

Google's AP2 represents a purchase as signed W3C VC Mandates (Intent -> Cart ->
Payment). The Payment Mandate is the INSTRUCTION ("the agent may pay X to Y");
Traceipt issues the settlement-VERIFIED receipt afterwards. Linking the two
closes the loop: the receipt references the exact AP2 Mandate it fulfilled, by
hash, through the existing hash-first `commerce.evidence` slot -- so a verifier
holding both can prove the settled payment corresponds to the authorized one,
and neither can be swapped.

Traceipt never stores the Mandate, only its digest (privacy-first, like all
evidence). The agent computes the digest and attaches it at POST /receipts time:

    commerce["evidence"] = [ap2.mandate_evidence(payment_mandate)]

No schema change -- schema.validate_evidence already accepts {type, hash, ref}.
Pure + stdlib.
"""
from __future__ import annotations

import hashlib
import json

DEFAULT_MANDATE_TYPE = "ap2-payment-mandate"


def mandate_hash(mandate) -> str:
    """Deterministic `sha256:<64hex>` digest of an AP2 Mandate object. Uses
    json.dumps(sort_keys, compact) -- the same canonicalization as
    screening.verdict_digest -- so it tolerates the numeric amounts AP2 Mandates
    carry (which Traceipt's own canonical JSON rejects) and both parties agree on
    the digest of the same Mandate."""
    canon = json.dumps(mandate, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canon).hexdigest()


def mandate_evidence(mandate, mandate_type: str = DEFAULT_MANDATE_TYPE,
                     ref: str | None = None) -> dict:
    """One `commerce.evidence` entry linking an AP2 Mandate to a receipt. Fits
    schema.validate_evidence exactly: {type, hash, ref}."""
    e = {"type": mandate_type[:40], "hash": mandate_hash(mandate)}
    if ref is not None:
        e["ref"] = str(ref)[:300]
    return e


def verify_mandate_link(receipt_payload: dict, mandate, *,
                        mandate_type: str | None = None):
    """Given a receipt and the ACTUAL AP2 Mandate, confirm the receipt is bound
    to it. Returns (ok, problems). ok iff the Mandate's digest appears in the
    receipt's commerce.evidence (optionally under `mandate_type`), so the linked
    Mandate cannot be swapped for a different one.

    NOTE: this proves the BINDING. Confirming the Mandate AUTHORIZED this exact
    payment (amount / recipient match) is an app-level cross-check to add once the
    AP2 Mandate field schema is pinned -- kept out of here to avoid guessing AP2
    internals and over-claiming."""
    commerce = receipt_payload.get("commerce") or {}
    evidence = commerce.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return False, ["receipt carries no evidence (no AP2 Mandate linked)"]
    want = mandate_hash(mandate)
    for e in evidence:
        if not isinstance(e, dict):
            continue
        if mandate_type is not None and e.get("type") != mandate_type:
            continue
        if e.get("hash") == want:
            return True, []
    return False, ["no evidence entry matches this Mandate's digest "
                   "(not linked, or a different Mandate)"]
