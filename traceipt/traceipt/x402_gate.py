"""
Real x402 payment gate + payer binding.

This turns the service from "dev gate accepts any header" into a paying
endpoint, AND closes the last audit finding (payer authentication).

x402 flow (the "exact" EVM scheme):
  1. server returns 402 with `accepts` = payment requirements.
  2. client signs an EIP-3009 transfer authorization and resubmits with the
     `X-PAYMENT` header = base64(JSON payment payload).
  3. server asks a *facilitator* to `/verify` the payload (the facilitator
     checks the secp256k1 signature, balance, nonce, expiry — so we never
     reimplement chain crypto) and then `/settle` it on-chain.

Payer binding (the security fix): the `X-PAYMENT` proves the caller controls
the wallet paying for the receipt. If `bind_payer` is on, we require that
wallet to equal the settlement's `payer`. Then a receipt can only be minted
by the very wallet that made the payment being documented — an attacker can
no longer claim a third party's transfer, because they cannot sign an
`X-PAYMENT` from that wallet.

The facilitator transport is injectable, so the whole gate is testable with a
fake facilitator (no funds, no network).
"""
from __future__ import annotations

import base64
import binascii
import json
import urllib.request
from collections import namedtuple

MAX_PAYMENT_HEADER = 8 * 1024  # base64 payload cap (untrusted input)

# What the gate hands back to the service.
#   ok=True  -> proceed; `payer` is the verified paying address (or None for
#               modes that don't authenticate a payer); `payment` is the
#               decoded X-PAYMENT to be settled AFTER the receipt is issued
#   ok=False -> send `code`/`body` to the client verbatim
GateDecision = namedtuple(
    "GateDecision", ["ok", "code", "body", "payer", "payment"]
)


def _proceed(payer=None, payment=None) -> GateDecision:
    return GateDecision(True, 0, None, payer, payment)


def _reject(code: int, body: dict) -> GateDecision:
    return GateDecision(False, code, body, None, None)


def decode_payment_header(value: str) -> dict:
    """Decode a base64 X-PAYMENT header into its JSON payload.

    Raises ValueError on anything malformed — never returns junk.
    """
    if not value or len(value) > MAX_PAYMENT_HEADER:
        raise ValueError("missing or oversized X-PAYMENT header")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("X-PAYMENT is not valid base64")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("X-PAYMENT does not contain valid JSON")
    if not isinstance(payload, dict):
        raise ValueError("X-PAYMENT payload must be a JSON object")
    if payload.get("x402Version") != 1:
        raise ValueError("unsupported x402Version")
    if not isinstance(payload.get("scheme"), str) or not isinstance(
        payload.get("network"), str
    ):
        raise ValueError("X-PAYMENT missing scheme/network")
    if not isinstance(payload.get("payload"), dict):
        raise ValueError("X-PAYMENT missing inner payload")
    return payload


def payer_from_payload(payload: dict) -> str | None:
    """Best-effort extraction of the paying address from the authorization,
    used only as a cross-check; the facilitator's verified `payer` is
    authoritative."""
    auth = payload.get("payload", {}).get("authorization", {})
    frm = auth.get("from")
    return frm.lower() if isinstance(frm, str) else None


class Facilitator:
    """Client for an x402 facilitator's /verify and /settle endpoints."""

    def __init__(self, base_url: str, transport=None, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport or self._http_transport

    def _http_transport(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def verify(self, payment: dict, requirements: dict) -> dict:
        return self._transport("/verify", {
            "x402Version": 1,
            "paymentPayload": payment,
            "paymentRequirements": requirements,
        })

    def settle(self, payment: dict, requirements: dict) -> dict:
        return self._transport("/settle", {
            "x402Version": 1,
            "paymentPayload": payment,
            "paymentRequirements": requirements,
        })


def gate_verify(facilitator: Facilitator, headers, requirements: dict,
                challenge_body: dict) -> GateDecision:
    """Verify a payment WITHOUT settling it, to gate access up front.

    Settling (moving the money) is deferred until the receipt has actually
    been issued — see `gate_settle` — so a caller is never charged for a
    receipt that fails to issue. `challenge_body` is the 402 body returned
    when payment is absent or invalid.
    """
    raw_header = headers.get("X-PAYMENT", "")
    if not raw_header.strip():
        return _reject(402, challenge_body)
    try:
        payment = decode_payment_header(raw_header)
    except ValueError as e:
        return _reject(402, {**challenge_body, "error": f"invalid X-PAYMENT: {e}"})

    try:
        v = facilitator.verify(payment, requirements)
    except Exception as e:
        return _reject(502, {"error": f"facilitator verify unreachable: {e}"})
    if not v.get("isValid"):
        reason = v.get("invalidReason", "payment did not verify")
        return _reject(402, {**challenge_body, "error": reason})

    # Authoritative payer: facilitator's verified value, else the signed `from`.
    payer = v.get("payer") or payer_from_payload(payment)
    payer = payer.lower() if isinstance(payer, str) else None
    return _proceed(payer=payer, payment=payment)


def gate_settle(facilitator: Facilitator, payment: dict,
                requirements: dict) -> tuple[bool, dict, str]:
    """Settle a previously-verified payment. Returns (ok, settle_response,
    error). Never raises."""
    try:
        s = facilitator.settle(payment, requirements)
    except Exception as e:
        return False, {}, f"facilitator settle unreachable: {e}"
    if not s.get("success"):
        return False, s, s.get("errorReason") or s.get("error", "settlement failed")
    return True, s, ""


def encode_payment_response(settle_response: dict) -> str:
    """base64 of the settle result, for the X-PAYMENT-RESPONSE header."""
    return base64.b64encode(
        json.dumps(settle_response).encode("utf-8")
    ).decode("ascii")
