"""
Receipt construction and validation — pure functions, unit-tested first.

A receipt binds three things together:
  1. an on-chain x402 settlement (chain, tx hash, asset, amount, payer, payee)
  2. the commercial context (what was bought, at what quoted price, from whom)
  3. its position in the issuing seller's tamper-evident chain
     (sequence + prev_receipt_hash)

Amounts are strings of base units (USDC has 6 decimals: "5000" = $0.005).
No floats anywhere — see canonical.py.
"""
from __future__ import annotations

import re

from .canonical import GENESIS_HASH, HASH_PREFIX, hash_obj

SPEC = "x402-receipt/v0.1"

_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_AMOUNT_RE = re.compile(r"^[0-9]+$")  # base units, non-negative integer string
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

SUPPORTED_CHAINS = {"base", "base-sepolia"}
VERIFICATION_METHODS = {"rpc", "mock", "unverified"}


def _require(cond: bool, msg: str):
    if not cond:
        raise ValueError(msg)


def validate_settlement(s: dict):
    _require(isinstance(s, dict), "settlement must be an object")
    _require(s.get("chain") in SUPPORTED_CHAINS,
             f"settlement.chain must be one of {sorted(SUPPORTED_CHAINS)}")
    _require(bool(_TX_HASH_RE.match(s.get("tx_hash", ""))),
             "settlement.tx_hash must be a 0x-prefixed 32-byte hex hash")
    _require(s.get("asset") == "USDC", "settlement.asset must be 'USDC' (v0.1)")
    _require(bool(_ADDR_RE.match(s.get("asset_contract", ""))),
             "settlement.asset_contract must be a 0x address")
    _require(bool(_AMOUNT_RE.match(s.get("amount_base_units", ""))),
             "settlement.amount_base_units must be a non-negative integer string")
    _require(bool(_ADDR_RE.match(s.get("payer", ""))),
             "settlement.payer must be a 0x address")
    _require(bool(_ADDR_RE.match(s.get("payee", ""))),
             "settlement.payee must be a 0x address")
    _require(s.get("verification_method") in VERIFICATION_METHODS,
             f"settlement.verification_method must be one of {sorted(VERIFICATION_METHODS)}")
    _require(isinstance(s.get("verified"), bool), "settlement.verified must be bool")


def validate_commerce(c: dict):
    _require(isinstance(c, dict), "commerce must be an object")
    res = c.get("resource", "")
    _require(isinstance(res, str) and res.startswith(("https://", "http://")),
             "commerce.resource must be an http(s) URL")
    _require(isinstance(c.get("description"), str) and 0 < len(c["description"]) <= 500,
             "commerce.description must be a non-empty string (<=500 chars)")
    _require(bool(_AMOUNT_RE.match(c.get("quoted_amount_base_units", ""))),
             "commerce.quoted_amount_base_units must be an integer string")
    for opt in ("request_hash", "response_hash"):
        if opt in c:
            _require(bool(_HASH_RE.match(c[opt])),
                     f"commerce.{opt} must look like sha256:<64 hex>")
    if "seller_entity" in c:
        ent = c["seller_entity"]
        _require(isinstance(ent, dict), "commerce.seller_entity must be an object")
        for k, v in ent.items():
            _require(isinstance(k, str) and isinstance(v, str),
                     "commerce.seller_entity must be a flat string map")


def build_receipt(*, seller_id: str, sequence: int, prev_receipt_hash: str,
                  issued_at: str, settlement: dict, commerce: dict) -> dict:
    """Assemble + validate a receipt payload. Deterministic: receipt_id is
    derived from the content hash, so the same inputs always produce the
    same receipt."""
    _require(isinstance(seller_id, str) and 0 < len(seller_id) <= 200,
             "seller_id must be a non-empty string")
    _require(isinstance(sequence, int) and sequence >= 1, "sequence must be int >= 1")
    if sequence == 1:
        _require(prev_receipt_hash == GENESIS_HASH,
                 "first receipt must chain from the genesis hash")
    else:
        _require(bool(_HASH_RE.match(prev_receipt_hash)),
                 "prev_receipt_hash must look like sha256:<64 hex>")
    _require(bool(_ISO_RE.match(issued_at)),
             "issued_at must be UTC ISO-8601 ending in Z")
    validate_settlement(settlement)
    validate_commerce(commerce)

    core = {
        "spec": SPEC,
        "seller_id": seller_id,
        "sequence": sequence,
        "prev_receipt_hash": prev_receipt_hash,
        "issued_at": issued_at,
        "settlement": settlement,
        "commerce": commerce,
    }
    receipt_id = "rcpt_" + hash_obj(core)[len(HASH_PREFIX):][:20]
    return {"receipt_id": receipt_id, **core}


def receipt_hash(payload: dict) -> str:
    """The hash that the NEXT receipt in this seller's chain must reference."""
    return hash_obj(payload)
