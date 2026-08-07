"""
Provable completeness: prove a seller's receipt chain is WHOLE, not just that
individual receipts are real.

Everyone in this space can prove "this payment happened." The harder, more
auditable claim is "here is EVERY payment, in order, with nothing inserted,
deleted, or hidden." Traceipt's per-seller chain already makes that provable:

  * sequence is dense (1..N) so a missing number is a visible hole;
  * each receipt commits to the previous one's hash (prev_receipt_hash), so the
    HEAD hash transitively commits to the entire history -- alter, insert, or
    drop any receipt and the head hash changes;
  * the issuer signs a CHECKPOINT over {count, head_hash} -- a non-repudiable
    commitment to exactly how many receipts exist and what the head is. The
    issuer can no longer quietly show a shorter (or different) chain later.

An auditor verifies the checkpoint signature, then checks the manifest is dense
and linked from genesis through to the signed head. Full per-receipt
authenticity stays a separate, existing step (fetch each receipt and verify its
own signature); this module proves the SET is complete and that the issuer
committed to it. Pure + stdlib.
"""
from __future__ import annotations

from .canonical import GENESIS_HASH

CHECKPOINT_TYPE = "chain-checkpoint"


def build_checkpoint(seller_id: str, count: int, head_hash: str, as_of: str,
                     genesis: str = GENESIS_HASH) -> dict:
    """The payload an issuer signs to commit to its chain length + head."""
    return {
        "type": CHECKPOINT_TYPE,
        "seller_id": seller_id,
        "count": count,
        "genesis": genesis,
        "head_receipt_hash": head_hash,
        "as_of": as_of,
    }


def verify_completeness(report: dict, verify_checkpoint, genesis: str = GENESIS_HASH,
                        expected_seller: str | None = None):
    """Verify a completeness report. Returns (ok, problems).

    `verify_checkpoint(envelope) -> payload` must AUTHENTICATE the signed
    checkpoint against the issuer's JWKS and return its payload (or raise). Pass
    a closure over signing.verify_envelope bound to the fetched JWKS, so a
    forged checkpoint is rejected before any structural check.

    `expected_seller`, when given, binds the proof to the seller the auditor
    actually cares about -- so a validly-signed checkpoint for a DIFFERENT
    seller cannot be passed off as this one.

    Checks, in order:
      * the checkpoint signature authenticates;
      * it is a chain-checkpoint for a matching genesis (and seller);
      * the manifest has exactly `count` rows with dense sequences 1..count;
      * prev links chain from genesis through to the signed head hash.
    A single failure still collects the rest, so the report lists every defect.

    NOTE: this proves the issuer SIGNED a commitment to exactly this set, dense
    and linked. It does not by itself prove each listed receipt is authentic --
    for that, fetch each at /receipts/{id} and verify its own signature.
    """
    problems: list[str] = []
    manifest = report.get("manifest")
    if not isinstance(manifest, list):
        return False, ["manifest missing or not a list"]

    try:
        cp = verify_checkpoint(report.get("checkpoint"))
    except Exception as e:  # forged / unsigned / wrong key -> fail closed
        return False, [f"checkpoint signature INVALID ({e})"]
    if not isinstance(cp, dict):
        return False, ["checkpoint did not authenticate to a payload"]

    if cp.get("type") != CHECKPOINT_TYPE:
        problems.append("signed payload is not a chain-checkpoint")
    if cp.get("genesis", genesis) != genesis:
        problems.append("checkpoint genesis mismatch")
    # Bind the seller: the signed checkpoint's seller must match the report's
    # claimed seller, and (if the auditor named one) the expected seller.
    cp_seller = cp.get("seller_id")
    if report.get("seller_id") is not None and cp_seller != report.get("seller_id"):
        problems.append("checkpoint seller_id does not match the report's seller_id")
    if expected_seller is not None and cp_seller != expected_seller:
        problems.append(f"checkpoint is for seller {cp_seller!r}, not the "
                        f"expected {expected_seller!r}")

    count = cp.get("count")
    if count != len(manifest):
        problems.append(f"signed count {count} != manifest length {len(manifest)} "
                        "(receipts hidden or added)")

    expected_prev = genesis
    for i, row in enumerate(manifest):
        if not isinstance(row, dict):
            problems.append(f"manifest row {i} is not an object")
            continue
        seq = row.get("sequence")
        if seq != i + 1:
            problems.append(f"gap: expected sequence {i + 1}, found {seq}")
        if row.get("prev_receipt_hash") != expected_prev:
            problems.append(f"seq {seq}: prev link broken (chain not contiguous)")
        expected_prev = row.get("receipt_hash")

    head = cp.get("head_receipt_hash")
    if manifest and expected_prev != head:
        problems.append("manifest head does not match the signed checkpoint head")
    if not manifest and count not in (0, None):
        problems.append(f"empty manifest but signed count is {count}")

    return (not problems), problems
