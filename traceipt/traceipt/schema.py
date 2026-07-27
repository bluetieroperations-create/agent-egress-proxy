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

# v0.2 adds: `kind` (payment|credit), credit-note receipts, and optional
# commerce blocks `evidence` (links to external action receipts, e.g.
# AAR/acta), `principal`, and `authorization`. All additions are optional or
# defaulted, so v0.1 receipts remain verifiable.
SPEC = "x402-receipt/v0.2"

_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_AMOUNT_RE = re.compile(r"^[0-9]+$")  # base units, non-negative integer string
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_RECEIPT_ID_RE = re.compile(r"^rcpt_[0-9a-f]{20}$")

SUPPORTED_CHAINS = {"base", "base-sepolia"}
VERIFICATION_METHODS = {"rpc", "mock", "unverified"}
RECEIPT_KINDS = {"payment", "credit"}
MAX_EVIDENCE = 16


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
    if "evidence" in c:
        validate_evidence(c["evidence"])
    if "principal" in c:
        validate_principal(c["principal"])
    if "authorization" in c:
        validate_authorization(c["authorization"])


def validate_evidence(ev):
    """Links to external signed artifacts (action receipts such as AAR/acta,
    logs, attestations) that this payment is evidence FOR. Hashes only —
    Traceipt never stores the artifact itself."""
    _require(isinstance(ev, list) and 0 < len(ev) <= MAX_EVIDENCE,
             f"commerce.evidence must be a list of 1..{MAX_EVIDENCE} entries")
    for i, e in enumerate(ev):
        _require(isinstance(e, dict), f"evidence[{i}] must be an object")
        t = e.get("type", "")
        _require(isinstance(t, str) and 0 < len(t) <= 40,
                 f"evidence[{i}].type must be a short string "
                 "(e.g. 'aar-receipt', 'acta-receipt', 'action-receipt')")
        _require(bool(_HASH_RE.match(e.get("hash", ""))),
                 f"evidence[{i}].hash must look like sha256:<64 hex>")
        if "ref" in e:
            _require(isinstance(e["ref"], str) and 0 < len(e["ref"]) <= 300,
                     f"evidence[{i}].ref must be a string (<=300 chars)")
        _require(set(e) <= {"type", "hash", "ref"},
                 f"evidence[{i}] allows only type/hash/ref")


def validate_principal(p):
    """Who the agent was spending on behalf of. Prefer id_hash over id for
    privacy; either is accepted."""
    _require(isinstance(p, dict), "commerce.principal must be an object")
    _require(set(p) <= {"id", "id_hash", "type"},
             "commerce.principal allows only id/id_hash/type")
    _require(("id" in p) or ("id_hash" in p),
             "commerce.principal needs id or id_hash")
    if "id" in p:
        _require(isinstance(p["id"], str) and 0 < len(p["id"]) <= 200,
                 "principal.id must be a string (<=200 chars)")
    if "id_hash" in p:
        _require(bool(_HASH_RE.match(p["id_hash"])),
                 "principal.id_hash must look like sha256:<64 hex>")
    if "type" in p:
        _require(isinstance(p["type"], str) and 0 < len(p["type"]) <= 40,
                 "principal.type must be a short string (human/org/service/...)")


def validate_authorization(a):
    """Under what authority the spend happened: scopes and a reference (or
    hash) of the grant/mandate. Hashes preferred for privacy."""
    _require(isinstance(a, dict), "commerce.authorization must be an object")
    _require(set(a) <= {"scopes", "grant_ref", "grant_hash", "delegation_depth"},
             "commerce.authorization allows only scopes/grant_ref/grant_hash/"
             "delegation_depth")
    if "scopes" in a:
        s = a["scopes"]
        _require(isinstance(s, list) and 0 < len(s) <= 32 and
                 all(isinstance(x, str) and 0 < len(x) <= 100 for x in s),
                 "authorization.scopes must be a list of 1..32 short strings")
    if "grant_ref" in a:
        _require(isinstance(a["grant_ref"], str) and 0 < len(a["grant_ref"]) <= 300,
                 "authorization.grant_ref must be a string (<=300 chars)")
    if "grant_hash" in a:
        _require(bool(_HASH_RE.match(a["grant_hash"])),
                 "authorization.grant_hash must look like sha256:<64 hex>")
    if "delegation_depth" in a:
        _require(isinstance(a["delegation_depth"], int) and
                 0 <= a["delegation_depth"] <= 32,
                 "authorization.delegation_depth must be an int 0..32")


def validate_credit(cr: dict):
    """The credit block of a credit-note receipt: which receipt it reverses
    and why. The refund settlement itself lives in the standard settlement
    block (direction reversed, verified on chain like any settlement)."""
    _require(isinstance(cr, dict), "credit must be an object")
    _require(set(cr) <= {"credits_receipt_id", "reason"},
             "credit allows only credits_receipt_id/reason")
    _require(bool(_RECEIPT_ID_RE.match(cr.get("credits_receipt_id", ""))),
             "credit.credits_receipt_id must look like rcpt_<20 hex>")
    _require(isinstance(cr.get("reason"), str) and 0 < len(cr["reason"]) <= 500,
             "credit.reason must be a non-empty string (<=500 chars)")


def validate_attestation_request(a: dict):
    """An anchoring-as-a-service submission: a digest to fold into the next
    Merkle batch. We never receive the artifact, only its hash."""
    _require(isinstance(a, dict), "attestation request must be an object")
    _require(set(a) <= {"hash", "type", "ref"},
             "attestation allows only hash/type/ref")
    _require(bool(_HASH_RE.match(a.get("hash", ""))),
             "hash must look like sha256:<64 hex>")
    if "type" in a:
        _require(isinstance(a["type"], str) and 0 < len(a["type"]) <= 40,
                 "type must be a short string (e.g. 'aar-chain-head')")
    if "ref" in a:
        _require(isinstance(a["ref"], str) and 0 < len(a["ref"]) <= 300,
                 "ref must be a string (<=300 chars)")


def build_receipt(*, seller_id: str, sequence: int, prev_receipt_hash: str,
                  issued_at: str, settlement: dict, commerce: dict | None = None,
                  kind: str = "payment", credit: dict | None = None) -> dict:
    """Assemble + validate a receipt payload. Deterministic: receipt_id is
    derived from the content hash, so the same inputs always produce the
    same receipt.

    kind='payment' documents a settlement and requires `commerce`.
    kind='credit' documents a verified REFUND and requires `credit`
    (which receipt it reverses, and why); its settlement is the refund
    transfer. Credit notes join the same per-seller sequential chain."""
    _require(isinstance(seller_id, str) and 0 < len(seller_id) <= 200,
             "seller_id must be a non-empty string")
    _require(kind in RECEIPT_KINDS, f"kind must be one of {sorted(RECEIPT_KINDS)}")
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

    core = {
        "spec": SPEC,
        "kind": kind,
        "seller_id": seller_id,
        "sequence": sequence,
        "prev_receipt_hash": prev_receipt_hash,
        "issued_at": issued_at,
        "settlement": settlement,
    }
    if kind == "payment":
        _require(commerce is not None, "payment receipts require commerce")
        _require(credit is None, "payment receipts must not carry a credit block")
        validate_commerce(commerce)
        core["commerce"] = commerce
    else:
        _require(credit is not None, "credit receipts require a credit block")
        _require(commerce is None, "credit receipts must not carry commerce")
        validate_credit(credit)
        core["credit"] = credit

    receipt_id = "rcpt_" + hash_obj(core)[len(HASH_PREFIX):][:20]
    return {"receipt_id": receipt_id, **core}


def receipt_hash(payload: dict) -> str:
    """The hash that the NEXT receipt in this seller's chain must reference."""
    return hash_obj(payload)
