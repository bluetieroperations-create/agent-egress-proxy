#!/usr/bin/env python3
"""
traceipt_ingest.py -- feed Traceipt receipts into Black_Wall reputation.

A Traceipt receipt is an on-chain-VERIFIED, payer/payee-bound settlement -- exactly
the confirmed-outcome signal Black_Wall's reputation moat wants, but FRAUD-RESISTANT:
Traceipt has already verified the settlement on-chain and bound it to what was
bought. Mapping receipts into the reputation store compounds the flywheel
(verdict -> payment -> receipt -> better next verdict).

Map a receipt's `settlement` block to the transfer shape
`reputation_store.ingest_transfers` consumes: {to, from, amount, tx_hash, timestamp}.

Guards (only clean, real settlements feed reputation):
  * only `kind == "payment"` receipts (credit notes / refunds are skipped);
  * only `settlement.verified is True` (never `unverified`);
  * amounts converted atomic base-units -> DECIMAL token amount (USDC 6dp) so they
    match the units the verdict engine and the on-chain ingest path use;
  * addresses lowercased so they land on the same reputation key the verdict engine
    and the store's lookup use (both lowercase EVM addresses).

Pure + stdlib; unit-tested. `ingest_receipts()` is a thin convenience over the
store's existing `ingest_transfers`.

⚠️ SECURITY -- AUTHENTICITY IS THE CALLER'S JOB. This module TRUSTS the receipt's
`settlement.verified` flag; it does NOT check the receipt's Ed25519 signature. A
forged receipt with `verified: true` would poison a counterparty's reputation.
Therefore: only feed receipts whose signature has been verified against Traceipt's
published JWKS. Pass a `verify(receipt) -> bool` callback (that does the JWKS
Ed25519 check) to `receipts_to_transfers` / `ingest_receipts`; without it, only
ingest receipts from a source you already trust end-to-end. Wiring the JWKS
signature check is the REQUIRED hardening before this touches untrusted input.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

USDC_DECIMALS = 6


def receipt_to_transfer(receipt):
    """Map one Traceipt receipt -> a reputation transfer dict
    {to, from, amount, tx_hash, timestamp}, or None if it must NOT feed reputation
    (not a verified payment, malformed, missing fields, non-positive amount)."""
    if not isinstance(receipt, dict):
        return None
    if receipt.get("kind") != "payment":          # skip credit notes / refunds
        return None
    s = receipt.get("settlement")
    if not isinstance(s, dict) or s.get("verified") is not True:
        return None
    payee, payer, tx = s.get("payee"), s.get("payer"), s.get("tx_hash")
    if not payee or not tx:
        return None
    try:
        amt = Decimal(str(s.get("amount_base_units"))) / (Decimal(10) ** USDC_DECIMALS)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not amt.is_finite() or amt <= 0:
        return None
    return {
        "to": str(payee).lower(),                  # counterparty (reputation key)
        "from": str(payer).lower() if payer else None,
        "amount": format(amt, "f"),                # decimal token units, e.g. "0.05"
        "tx_hash": tx,
        "timestamp": receipt.get("issued_at"),
    }


def receipts_to_transfers(receipts, verify=None):
    """Map many receipts; drop the ones that must not feed reputation.

    `verify(receipt) -> bool`, when provided, is an AUTHENTICITY gate (verify the
    Ed25519 signature against Traceipt's JWKS) applied BEFORE mapping -- a receipt
    that fails it is dropped. Strongly recommended for any untrusted source (see
    the module SECURITY note)."""
    out = []
    for r in receipts or []:
        if verify is not None and not _safe_verify(verify, r):
            continue
        t = receipt_to_transfer(r)
        if t:
            out.append(t)
    return out


def _safe_verify(verify, receipt):
    """A raising verify() must fail CLOSED (drop the receipt), never crash ingest."""
    try:
        return bool(verify(receipt))
    except Exception:
        return False


def ingest_receipts(store, receipts, verify=None):
    """Convenience: fold verified Traceipt payment receipts into a reputation
    store. Returns the number of NEW settlements ingested (idempotent on tx_hash
    via the store). No-op-safe on an empty/all-filtered list. Pass `verify` to
    gate on the receipt's JWKS signature (see the module SECURITY note)."""
    transfers = receipts_to_transfers(receipts, verify=verify)
    if not transfers:
        return 0
    return store.ingest_transfers(transfers)
