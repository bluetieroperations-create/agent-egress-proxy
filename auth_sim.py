#!/usr/bin/env python3
"""
auth_sim.py -- simulate the ACTUAL EIP-3009 authorization, not a proxy transfer.

`settlement_sim.py` simulates `transfer(payer -> payee)`. That is a good proxy for "is
either party frozen", but x402 does NOT settle with `transfer`: a facilitator submits
**`transferWithAuthorization`** carrying the agent's signature. Three failure modes live
in that call which a plain-transfer simulation STRUCTURALLY cannot see:

  1. REPLAY   -- the authorization's nonce was already used (or cancelled). The token
                 rejects it, so the payment silently fails. Detected DIRECTLY by reading
                 `authorizationState(authorizer, nonce)`, not inferred from a revert.
  2. EXPIRY   -- `validBefore` has passed, or `validAfter` is still in the future. A pure
                 clock check needing no chain call at all.
  3. EXECUTION-- the real call reverts anyway (bad signature, blacklist, insufficient
                 balance). Caught by `eth_call`ing the genuine 9-argument function.

This closes the gap between "a transfer would work" and "THIS authorization would work".

RELATIONSHIP TO payload_sim.py: that module verifies the SHOWN authorization matches the
claim and that the signature recovers to the stated payer -- and it owns the hard STOP for
a mismatch. This module asks the complementary question the chain alone can answer: would
the network actually accept it *right now*? Consistent with the rest of the stack it is
HOLD-only, never STOP, fail-open, and opt-in.

ABI NOTE: `rwa_readiness.eth_call_data` handles address/uint only -- a `bytes32` nonce
would fall through its numeric branch and encode as ZEROS, silently simulating the wrong
authorization. So the encoding here is explicit and separately tested.

Stdlib. Pure encoders/assessors first, then an injected-transport source.
"""
from __future__ import annotations

import time

from addresses import is_evm_address
from rwa_readiness import selector

TRANSFER_WITH_AUTHORIZATION_SIG = (
    "transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,"
    "uint8,bytes32,bytes32)")
AUTHORIZATION_STATE_SIG = "authorizationState(address,bytes32)"

# Grades.
VALID = "valid"
REPLAYED = "replayed"
EXPIRED = "expired"
NOT_YET_VALID = "not_yet_valid"
WOULD_REVERT = "would_revert"
AUTH_UNKNOWN = "unknown"

_GRADES = (VALID, REPLAYED, EXPIRED, NOT_YET_VALID, WOULD_REVERT, AUTH_UNKNOWN)
# Everything that means "this payment cannot land" gates. `valid`/`unknown` never do.
_GATING = (REPLAYED, EXPIRED, NOT_YET_VALID, WOULD_REVERT)


# --------------------------------------------------------------------------
# ABI encoding (explicit -- see the ABI NOTE above)
# --------------------------------------------------------------------------

def encode_uint(value):
    """uint -> a 64-hex ABI word. Out-of-range/junk -> zeros. NEVER raises.

    Catches Exception, not a tuple: `int(float("inf"))` raises OverflowError, which an
    enumerated (TypeError, ValueError) missed. Same bug class CI found in dex_price on
    the 3.12 leg -- a defensive encoder has ONE correct behaviour for every bad input,
    so enumerating exception types is a standing hazard for no benefit."""
    try:
        v = int(value)
    except Exception:
        return "0" * 64
    if v < 0 or v >= (1 << 256):
        return "0" * 64
    return format(v, "x").rjust(64, "0")


def encode_address(value):
    """address -> a 64-hex ABI word (left-padded). NEVER raises."""
    if not is_evm_address(value):
        return "0" * 64
    return value[2:].lower().rjust(64, "0")


def encode_bytes32(value):
    """bytes32 -> a 64-hex ABI word. Accepts 0x-hex (padded/truncated to 32 bytes) or
    raw bytes. Junk -> zeros. NEVER raises. THIS is the encoder eth_call_data lacks."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)[:32].hex().ljust(64, "0")
    if not isinstance(value, str):
        return "0" * 64
    h = value[2:] if value[:2].lower() == "0x" else value
    try:
        int(h, 16)
    except (TypeError, ValueError):
        return "0" * 64
    # bytes32 is RIGHT-padded (it is a byte string, not a number).
    return h.lower()[:64].ljust(64, "0")


def split_signature(sig):
    """65-byte 0x signature -> (v, r_hex, s_hex), or None. Accepts v as 27/28 or 0/1
    (normalized to 27/28). NEVER raises."""
    if isinstance(sig, (bytes, bytearray)):
        raw = bytes(sig)
    elif isinstance(sig, str):
        h = sig[2:] if sig[:2].lower() == "0x" else sig
        try:
            raw = bytes.fromhex(h)
        except (TypeError, ValueError):
            return None
    else:
        return None
    if len(raw) != 65:
        return None
    r, s, v = raw[0:32], raw[32:64], raw[64]
    if v in (0, 1):
        v += 27
    if v not in (27, 28):
        return None
    return (v, r.hex(), s.hex())


def encode_authorization_state(authorizer, nonce):
    """Calldata for `authorizationState(address,bytes32)` -- the DIRECT replay read."""
    return (selector(AUTHORIZATION_STATE_SIG)
            + encode_address(authorizer) + encode_bytes32(nonce))


def encode_transfer_with_authorization(auth, signature):
    """Calldata for the real `transferWithAuthorization(...)`, or None when the
    authorization/signature is unusable. NEVER raises."""
    if not isinstance(auth, dict):
        return None
    parts = split_signature(signature)
    if parts is None:
        return None
    v, r, s = parts
    return (selector(TRANSFER_WITH_AUTHORIZATION_SIG)
            + encode_address(auth.get("from"))
            + encode_address(auth.get("to"))
            + encode_uint(auth.get("value"))
            + encode_uint(auth.get("validAfter"))
            + encode_uint(auth.get("validBefore"))
            + encode_bytes32(auth.get("nonce"))
            + encode_uint(v) + encode_bytes32(r) + encode_bytes32(s))


# --------------------------------------------------------------------------
# Pure assessment
# --------------------------------------------------------------------------

def check_window(valid_after, valid_before, now=None):
    """PURE: EXPIRED / NOT_YET_VALID / None (in-window or undeterminable). Needs no chain
    call. Absent or unparseable bounds are treated as 'not determined' -- fail-open, never
    a fabricated failure. NEVER raises."""
    t = int(now if now is not None else time.time())

    def _i(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return None
    va, vb = _i(valid_after), _i(valid_before)
    # validBefore == 0 conventionally means "no expiry".
    if vb is not None and vb > 0 and t >= vb:
        return EXPIRED
    if va is not None and t < va:
        return NOT_YET_VALID
    return None


def assess_authorization(probe):
    """PURE: a normalized probe -> the authorization signal.

    probe (all optional; None = not determined):
      {used: bool|None,            # authorizationState -> already used/cancelled
       window: EXPIRED|NOT_YET_VALID|None,
       execution_ok: bool|None,    # eth_call of the real transferWithAuthorization
       revert_reason: str|None}

    Ordering is deliberate: REPLAY and WINDOW are checked FIRST because they are
    definitive and explain the revert; only then do we blame execution. NEVER raises."""
    p = probe if isinstance(probe, dict) else {}
    if p.get("used") is True:
        return {"grade": REPLAYED, "reason": "authorization nonce already used or "
                                             "cancelled on-chain"}
    win = p.get("window")
    if win in (EXPIRED, NOT_YET_VALID):
        return {"grade": win,
                "reason": ("authorization has expired (validBefore has passed)"
                           if win == EXPIRED else
                           "authorization is not yet valid (validAfter is in the future)")}
    if p.get("execution_ok") is False:
        return {"grade": WOULD_REVERT,
                "reason": p.get("revert_reason") or "the authorization call reverts"}
    if p.get("execution_ok") is True or p.get("used") is False:
        return {"grade": VALID, "reason": None}
    return {"grade": AUTH_UNKNOWN, "reason": p.get("revert_reason")}


def apply_auth_sim(verdict, signal):
    """PURE fold. CONSERVATIVE-ONLY: escalates GO -> HOLD when the signed authorization
    could not land (replayed / outside its window / reverts). NEVER upgrades, NEVER STOPs
    (payload_sim keeps the STOP authority for a mismatched or forged authorization), never
    mutates the input. NEVER raises."""
    if not signal or not isinstance(signal, dict) or not isinstance(verdict, dict):
        return verdict
    grade = signal.get("grade")
    if grade not in _GRADES:
        return verdict

    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    v["signals"]["auth_sim"] = {"grade": grade, "gated": grade in _GATING,
                                "reason": signal.get("reason")}
    reasons = list(v.get("reasons") or [])
    detail = (" -- %s" % signal["reason"]) if signal.get("reason") else ""

    if grade == REPLAYED:
        reasons.append("authorization simulation: this EIP-3009 nonce is ALREADY USED "
                       "on-chain%s -- resubmitting it settles nothing" % detail)
    elif grade in (EXPIRED, NOT_YET_VALID):
        reasons.append("authorization simulation: the signed authorization is outside "
                       "its validity window%s" % detail)
    elif grade == WOULD_REVERT:
        reasons.append("authorization simulation: the real transferWithAuthorization "
                       "call REVERTS%s" % detail)
    elif grade == VALID:
        reasons.append("authorization simulation: the signed authorization would settle")

    if grade in _GATING and v.get("verdict") == "GO":
        v["verdict"] = "HOLD"
        reasons.append("escalated GO->HOLD: the signed payment authorization cannot "
                       "land on-chain as-is -- do not submit it; re-sign a fresh, "
                       "in-window authorization")
    v["reasons"] = reasons
    return v


# --------------------------------------------------------------------------
# Live source
# --------------------------------------------------------------------------

class AuthorizationSimSource:
    """Checks a SIGNED x402 authorization against the chain. `check(claim, payment)`
    -> signal or None (no-op when there is no authorization to check).

    Costs at most two eth_calls, cheapest-first: the `authorizationState` replay read is
    a tiny view, and the full execution simulation runs only if that comes back clean.
    NEVER raises."""

    def __init__(self, rpc_by_chain=None, tokens=None, simulator=None, timeout=4.0,
                 now=None):
        from settlement_sim import USDC_BY_CHAIN
        self.rpc_by_chain = rpc_by_chain or {}
        self.tokens = tokens or USDC_BY_CHAIN
        self.timeout = timeout
        self._simulator = simulator
        self._now = now

    def _sim_for(self, chain):
        if self._simulator is not None:
            return self._simulator
        from transfer_sim import TransferSimulator
        url = self.rpc_by_chain.get(chain)
        return TransferSimulator(rpc_url=url, timeout=self.timeout) if url else None

    def check(self, claim, payment):
        from transfer_sim import decode_revert_error
        from x402 import _authorization
        if not isinstance(claim, dict):
            return None
        auth = _authorization(payment)
        if not auth or not auth.get("nonce"):
            return None                       # nothing signed to check -- no-op
        chain = (claim.get("chain") or "").strip().lower()
        token = self.tokens.get(chain)
        sim = self._sim_for(chain)
        if not token or sim is None:
            return None

        probe = {"used": None, "window": None, "execution_ok": None,
                 "revert_reason": None}
        # 1) WINDOW -- free, no chain call.
        probe["window"] = check_window(auth.get("validAfter"), auth.get("validBefore"),
                                       now=self._now)
        # 2) REPLAY -- one cheap view call, definitive.
        authorizer = auth.get("from")
        if is_evm_address(authorizer):
            try:
                res = sim._call({"to": token,
                                 "data": encode_authorization_state(authorizer,
                                                                    auth.get("nonce"))})
            except Exception:
                res = None
            if isinstance(res, dict) and isinstance(res.get("result"), str):
                r = res["result"]
                if len(r) >= 3:
                    try:
                        probe["used"] = int(r, 16) != 0
                    except (TypeError, ValueError):
                        probe["used"] = None
        if probe["used"] is True or probe["window"]:
            return assess_authorization(probe)   # definitive; skip the costly call

        # 3) EXECUTION -- simulate the real 9-arg call.
        data = encode_transfer_with_authorization(auth, _signature_of(payment))
        if data is None:
            return assess_authorization(probe)
        try:
            res = sim._call({"to": token, "from": claim.get("counterparty") or authorizer,
                             "data": data})
        except Exception:
            return assess_authorization(probe)
        if isinstance(res, dict):
            if res.get("error"):
                probe["execution_ok"] = False
                probe["revert_reason"] = decode_revert_error(res["error"])
            elif "result" in res:
                probe["execution_ok"] = True
        return assess_authorization(probe)


def _signature_of(payment):
    """The 65-byte signature from an x402 payment payload. NEVER raises."""
    if not isinstance(payment, dict):
        return None
    payload = payment.get("payload")
    if isinstance(payload, dict) and payload.get("signature"):
        return payload["signature"]
    return payment.get("signature")
