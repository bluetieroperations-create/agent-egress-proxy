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
import urllib.error
import urllib.request
from collections import namedtuple

X402_VERSION = 2  # protocol version emitted in the facilitator envelope

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
    version = payload.get("x402Version")
    if version not in (1, 2):
        raise ValueError("unsupported x402Version")
    # Both versions nest the EIP-3009 authorization under `payload`.
    if not isinstance(payload.get("payload"), dict):
        raise ValueError("X-PAYMENT missing inner payload")
    if version == 2:
        # v2 carries the chosen requirements (scheme/network/asset) under
        # `accepted`; the top level has no scheme/network.
        if not isinstance(payload.get("accepted"), dict):
            raise ValueError("X-PAYMENT (v2) missing accepted requirements")
    else:
        # v1 legacy: scheme/network live at the top level.
        if not isinstance(payload.get("scheme"), str) or not isinstance(
            payload.get("network"), str
        ):
            raise ValueError("X-PAYMENT missing scheme/network")
    return payload


def _authorization(payment: dict) -> dict:
    """The EIP-3009 authorization inside a payment payload, type-safely.

    The nested payload/authorization are fully attacker-controlled (only the
    top level is guaranteed a dict by decode_payment_header), so a non-dict at
    any level returns {} rather than raising."""
    if not isinstance(payment, dict):
        return {}
    inner = payment.get("payload")
    if not isinstance(inner, dict):
        return {}
    auth = inner.get("authorization")
    return auth if isinstance(auth, dict) else {}


def payer_from_payload(payload: dict) -> str | None:
    """Best-effort extraction of the paying address from the authorization,
    used only as a cross-check; the facilitator's verified `payer` is
    authoritative."""
    frm = _authorization(payload).get("from")
    return frm.lower() if isinstance(frm, str) else None


def payment_satisfies(payment: dict, requirements: dict) -> tuple[bool, str]:
    """Defense-in-depth: does the payment's ACTUAL signed authorization match
    the terms we require (recipient + exact amount), independent of what the
    facilitator reports? The `exact` scheme (EIP-3009 transferWithAuthorization)
    signs a fixed {to, value, nonce}; we assert those against our own
    requirements so we never rely solely on the facilitator to enforce that a
    caller paid the right address the right amount. Returns (ok, reason).

    Checks are applied only for requirement fields that are present, so a
    minimal requirements dict (e.g. in a unit test) still passes; a real
    challenge always carries payTo + amount."""
    auth = _authorization(payment)

    pay_to = requirements.get("payTo")
    if pay_to is not None:
        to = auth.get("to")
        if not isinstance(to, str) or to.lower() != str(pay_to).lower():
            return False, "payment recipient does not match payTo"

    required_raw = requirements.get("amount", requirements.get("maxAmountRequired"))
    if required_raw is not None:
        try:
            value = int(str(auth.get("value")))
            required = int(str(required_raw))
        except (TypeError, ValueError):
            return False, "missing/invalid payment value"
        # exact scheme: the authorization must move EXACTLY the required amount.
        if value != required:
            return False, "underpaid" if value < required else "overpaid"

    # A real EIP-3009 authorization always carries a nonce.
    if not auth.get("nonce"):
        return False, "missing authorization nonce"
    return True, "ok"


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
            # A Cloudflare-fronted facilitator answers 403 to the default
            # Python-urllib UA, which would fail the gate with "facilitator
            # unreachable"; send a plain identifying UA + explicit accept.
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Traceipt/0.2 x402-gate",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Real facilitators (observed on Base Sepolia) return a STRUCTURED
            # x402 error with a non-2xx status — e.g. 500 + {isValid:false,
            # invalidReason:...} when on-chain signature recovery reverts.
            # Surface that body instead of treating it as opaque/unreachable,
            # so the caller sees the real reason rather than a 502.
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                raise  # genuinely opaque error -> propagate -> caller fails safe

    def verify(self, payment: dict, requirements: dict) -> dict:
        return self._transport("/verify", {
            "x402Version": X402_VERSION,
            "paymentPayload": payment,
            "paymentRequirements": requirements,
        })

    def settle(self, payment: dict, requirements: dict) -> dict:
        return self._transport("/settle", {
            "x402Version": X402_VERSION,
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

    # Defense-in-depth: check the signed authorization matches OUR terms
    # (recipient + exact amount) BEFORE spending a facilitator round-trip and
    # before trusting its verdict. A payment that pays the wrong address or the
    # wrong amount is rejected here, not on the facilitator's good behavior.
    ok, reason = payment_satisfies(payment, requirements)
    if not ok:
        return _reject(402, {**challenge_body,
                             "error": f"payment does not satisfy terms: {reason}"})

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
