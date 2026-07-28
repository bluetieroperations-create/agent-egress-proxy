#!/usr/bin/env python3
"""
aa_cosigner.py -- Blackwall as an ERC-4337 / ERC-7579 co-signer (the "mandatory
guard" bet). Off-chain half, pure-Python + stdlib.

Today Blackwall is verdict-ONLY: advisory, never in the money path, no custody. This
module implements the deliberate ALTERNATIVE from docs/STRATEGY_REVIEW.md #4 -- a
smart account that CANNOT broadcast a payment without Blackwall's signature:

    account.validateUserOp(userOp) -> the on-chain Blackwall validator ecrecovers a
    co-signature from userOp.signature and checks it is Blackwall's authorized key.
    Blackwall returns that signature ONLY when the verdict is GO (or a human approved
    a HOLD); on a STOP it WITHHOLDS the signature -> validation fails -> no payment.

What lives here (off-chain, what Blackwall runs):
  * user_op_hash(): the exact ERC-4337 v0.7 EntryPoint userOpHash the account signs.
  * decode_execute(): pull the inner (to, value, data) from a standard account
    `execute(address,uint256,bytes)` callData, so we screen the REAL on-chain call
    (via calldata.py Phase 3) -- not a self-reported claim.
  * cosign_user_op(): run the verdict + calldata screen, then SIGN the userOpHash on
    GO / approved-HOLD, or REFUSE on STOP. Explicit fail-open vs fail-closed when the
    verdict engine is unreachable (the load-bearing availability decision).
  * recover_cosigner(): what the on-chain validator does -- ecrecover the address.

⚠️ POSTURE CHANGE (do not skip): as a co-signer Blackwall sits IN the transaction
path and holds a signing key -- a hard availability dependency and a
money-transmitter/liability posture shift vs. verdict-only. Prototype on a testnet
ERC-7579 account before committing; see docs/AA_COSIGNING.md for the reference
on-chain validator and the fail-open/closed tradeoff. The `userOpHash` MUST be
validated against your target EntryPoint version before production.
"""
from __future__ import annotations

from eip712 import enc_address, enc_uint, pubkey_to_address
from keccak import keccak256
from secp256k1 import ecdsa_recover, ecdsa_sign

# Availability policy when the verdict engine is UNREACHABLE (not when it says STOP).
FAIL_CLOSED = "fail_closed"   # withhold the signature -> payments HALT (safest)
FAIL_OPEN = "fail_open"       # sign anyway -> degrade to advisory (preserves liveness)

# Standard SimpleAccount/Kernel/etc. execution entrypoint.
EXECUTE_SELECTOR = keccak256(b"execute(address,uint256,bytes)")[:4]


# ---------------------------------------------------------------------------
# ERC-4337 v0.7 userOpHash (PackedUserOperation).
# ---------------------------------------------------------------------------
def _to_bytes(v):
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    s = str(v)
    s = s[2:] if s.lower().startswith("0x") else s
    return bytes.fromhex(s) if s else b""


def _pack128(hi, lo):
    """bytes32 packing of two uint128 (verification||call, or maxPriority||maxFee)."""
    return ((int(hi) << 128) | int(lo)).to_bytes(32, "big")


def _bytes32(v):
    b = _to_bytes(v)
    if len(b) > 32:
        raise ValueError("bytes32 too long")
    return b.rjust(32, b"\x00")


def user_op_hash(user_op, entry_point, chain_id):
    """The ERC-4337 v0.7 EntryPoint.getUserOpHash for a PackedUserOperation.

    `user_op` is a dict; gas fields may be given packed (`accountGasLimits`,
    `gasFees` as bytes32/hex) OR unpacked (`verificationGasLimit`+`callGasLimit`,
    `maxPriorityFeePerGas`+`maxFeePerGas`) -- the latter are packed here. `initCode`,
    `callData`, `paymasterAndData` default to empty. Returns the hash as int (the
    value ecrecover/ecdsa operate on)."""
    u = user_op if isinstance(user_op, dict) else {}
    if "accountGasLimits" in u:
        account_gas = _bytes32(u["accountGasLimits"])
    else:
        account_gas = _pack128(u.get("verificationGasLimit", 0),
                               u.get("callGasLimit", 0))
    if "gasFees" in u:
        gas_fees = _bytes32(u["gasFees"])
    else:
        gas_fees = _pack128(u.get("maxPriorityFeePerGas", 0),
                            u.get("maxFeePerGas", 0))
    encoded = (
        enc_address(u.get("sender") or 0)
        + enc_uint(u.get("nonce", 0))
        + keccak256(_to_bytes(u.get("initCode", b"")))
        + keccak256(_to_bytes(u.get("callData", b"")))
        + account_gas
        + enc_uint(u.get("preVerificationGas", 0))
        + gas_fees
        + keccak256(_to_bytes(u.get("paymasterAndData", b"")))
    )
    inner = keccak256(encoded)
    outer = keccak256(inner + enc_address(entry_point) + enc_uint(chain_id))
    return int.from_bytes(outer, "big")


# ---------------------------------------------------------------------------
# Decode the account's execute() to screen the REAL inner call.
# ---------------------------------------------------------------------------
def decode_execute(call_data):
    """Decode `execute(address dest,uint256 value,bytes func)` -> {to, value, data}
    or None if the callData isn't that shape. NEVER raises."""
    try:
        raw = _to_bytes(call_data)
        if len(raw) < 4 + 96 or raw[:4] != EXECUTE_SELECTOR:
            return None
        body = raw[4:]
        dest = "0x" + body[12:32].hex()
        value = int.from_bytes(body[32:64], "big")
        off = int.from_bytes(body[64:96], "big")
        if off + 32 > len(body):
            return None
        ln = int.from_bytes(body[off:off + 32], "big")
        data = body[off + 32:off + 32 + ln]
        if len(data) != ln:
            return None
        return {"to": dest, "value": value, "data": "0x" + data.hex()}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# The co-signer.
# ---------------------------------------------------------------------------
class CosignResult:
    """Outcome of a co-sign request."""
    def __init__(self, *, approved, verdict, reasons, user_op_hash, signature=None,
                 signer=None, action=None):
        self.approved = approved         # True -> `signature` is a valid co-signature
        self.verdict = verdict           # GO | HOLD | STOP | None(unavailable)
        self.reasons = reasons
        self.user_op_hash = user_op_hash
        self.signature = signature       # 0x r||s||v (65 bytes) or None (refused)
        self.signer = signer             # the co-signer address (when signed)
        self.action = action             # "signed" | "refused" | "confirmed"

    def summary(self):
        return "%s -> %s (%s)" % (self.verdict or "UNAVAILABLE",
                                  "SIGNED" if self.approved else "REFUSED",
                                  "; ".join(str(r) for r in (self.reasons or [])[:3]))


def _sign_hash(hash_int, signer_key):
    r, s, rec = ecdsa_sign(hash_int, int(signer_key))
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([27 + rec])
    return "0x" + sig.hex(), pubkey_to_address(ecdsa_recover(hash_int, r, s, rec))


def recover_cosigner(hash_int, signature):
    """What the on-chain validator does: ecrecover the co-signer address from a
    65-byte signature over the userOpHash. Returns 0x-address or None."""
    try:
        raw = _to_bytes(signature)
        if len(raw) != 65:
            return None
        r = int.from_bytes(raw[0:32], "big")
        s = int.from_bytes(raw[32:64], "big")
        v = raw[64]
        rec = v - 27 if v >= 27 else v
        q = ecdsa_recover(int(hash_int), r, s, rec)
        return pubkey_to_address(q) if q else None
    except Exception:
        return None


def screen_user_op(user_op, claim):
    """Screen the userOp's inner call against the claim (calldata.py Phase 3).
    Returns {mismatches, warnings}. If callData isn't a decodable execute(), we
    can't verify the on-chain call -> a warning (rely on the verdict/claim)."""
    from calldata import screen_transaction
    inner = decode_execute((user_op or {}).get("callData"))
    if inner is None:
        return {"mismatches": [], "warnings": [
            "userOp.callData is not a decodable execute(address,uint256,bytes) -- "
            "cannot screen the on-chain call; relying on the claim/verdict"]}
    res = screen_transaction(claim, {"to": inner["to"], "data": inner["data"],
                                     "value": inner["value"]})
    return {"mismatches": res["mismatches"], "warnings": res["warnings"],
            "inner": inner}


def cosign_user_op(user_op, claim, *, entry_point, chain_id, verdict_fn, signer_key,
                   policy=FAIL_CLOSED, confirm=None, screen=True):
    """Decide whether to co-sign `user_op`, and sign it iff the verdict permits.

    `verdict_fn(claim) -> verdict_dict` obtains the Blackwall verdict (may raise if
    the engine is unreachable). `signer_key` is Blackwall's ECDSA private key (int).
    On GO (and no calldata mismatch) -> SIGN. On HOLD -> `confirm(result)->bool`
    gates the signature. On STOP or a calldata mismatch -> REFUSE (no signature). If
    `verdict_fn` raises: FAIL_CLOSED refuses, FAIL_OPEN signs (advisory) -- the
    load-bearing availability choice. Never raises."""
    uoh = user_op_hash(user_op, entry_point, chain_id)

    def refused(verdict, reasons):
        return CosignResult(approved=False, verdict=verdict, reasons=reasons,
                            user_op_hash=uoh, action="refused")

    def signed(verdict, reasons, action="signed"):
        sig, signer = _sign_hash(uoh, signer_key)
        return CosignResult(approved=True, verdict=verdict, reasons=reasons,
                            user_op_hash=uoh, signature=sig, signer=signer,
                            action=action)

    # Screen the actual on-chain call; a drainer/recipient mismatch is a hard refuse.
    screen_res = screen_user_op(user_op, claim) if screen else {"mismatches": [],
                                                                "warnings": []}
    if screen_res["mismatches"]:
        return refused("STOP", ["calldata mismatch: " + m
                                for m in screen_res["mismatches"]])

    # Obtain the verdict; handle an unreachable engine per policy.
    try:
        verdict = verdict_fn(claim) or {}
    except Exception as e:
        if policy == FAIL_OPEN:
            return signed(None, ["verdict unavailable (%s) -- FAIL_OPEN co-signed "
                                 "advisory" % e] + screen_res["warnings"],
                          action="signed")
        return refused(None, ["verdict unavailable (%s) -- FAIL_CLOSED withheld "
                              "signature" % e])

    v = verdict.get("verdict")
    reasons = list(verdict.get("reasons") or []) + screen_res["warnings"]
    if v == "STOP":
        return refused("STOP", reasons)
    if v == "GO":
        return signed("GO", reasons)
    # HOLD (or unknown) -> require explicit approval before signing.
    if callable(confirm) and confirm(CosignResult(
            approved=False, verdict=v, reasons=reasons, user_op_hash=uoh)):
        return signed(v, reasons + ["human-approved HOLD"], action="confirmed")
    return refused(v, reasons + ["HOLD not approved"])


def make_verdict_fn(reputation_source, **forecast_kwargs):
    """Convenience: a `verdict_fn` backed by blackwall.forecast (raises on a bad
    request so the co-signer treats it as engine-unavailable per policy)."""
    import blackwall

    def _fn(claim):
        resp, err = blackwall.forecast(dict(claim or {}), reputation_source,
                                       **forecast_kwargs)
        if err is not None:
            raise ValueError(err)
        return resp
    return _fn
