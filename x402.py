#!/usr/bin/env python3
"""
x402.py -- Blackwall's own x402 billing layer (spec step 3).

Blackwall is itself an x402 resource: the calling agent pays per forecast (or
funds a session of many forecasts). The elegant symmetry -- the service that
judges x402 payments is itself paid via x402.

    Agent -> POST /v1/forecast-payment            (no payment)
    Blackwall -> 402 { accepts:[{price, asset, chain, payTo}], ... }
    Agent -> retry with `X-PAYMENT: <base64 payload>`
    Blackwall -> verify (FACILITATOR) + match + anti-replay -> serve verdict

DIVISION OF LABOR (spec 5.4): the FACILITATOR does signature + balance + on-chain
replay for free, so we DO NOT rebuild those -- they sit behind the `Facilitator`
seam (mocked here; HttpFacilitator is the real-deployment stub). Blackwall owns
the PROTOCOL envelope: building the 402 challenge, decoding/MATCHING the payment
to the requirements (right recipient/asset/chain/amount), local idempotency (do
not serve one payment's verdict twice), and SESSION tokens (fund-once, many
checks -- the latency/cost answer from the spec).

The pure functions (build_requirements / decode_payment_header /
payment_satisfies / session token sign+verify) are the tested core. Stdlib only.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import threading
import time
from decimal import Decimal, InvalidOperation

from addresses import addresses_equal, is_evm_address

X402_VERSION = 1
DEFAULT_SCHEME = "exact"          # EIP-3009 transferWithAuthorization
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


# ===========================================================================
# Amounts
# ===========================================================================
def to_atomic(amount, decimals=6):
    """Human amount ('0.001') -> atomic integer units. None on bad input."""
    try:
        d = Decimal(str(amount))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not d.is_finite() or d < 0:
        return None
    scaled = d * (Decimal(10) ** int(decimals))
    if scaled != scaled.to_integral_value():
        # more precision than the asset supports -> reject rather than silently round
        return None
    return int(scaled)


# ===========================================================================
# PURE protocol core (unit-tested)
# ===========================================================================
def build_requirements(price_atomic, pay_to, resource, asset=BASE_USDC,
                       network="base", scheme=DEFAULT_SCHEME,
                       max_timeout_seconds=120, description="Blackwall payment forecast"):
    """One entry of the 402 `accepts` array (x402 payment requirements)."""
    return {
        "scheme": scheme,
        "network": network,
        "maxAmountRequired": str(price_atomic),
        "resource": resource,
        "description": description,
        "mimeType": "application/json",
        "payTo": pay_to,
        "maxTimeoutSeconds": max_timeout_seconds,
        "asset": asset,
    }


def make_402_body(requirements_list, error=None):
    """The JSON body of a 402 Payment Required response."""
    body = {"x402Version": X402_VERSION, "accepts": requirements_list}
    if error:
        body["error"] = error
    return body


def decode_payment_header(header_value):
    """
    Decode an `X-PAYMENT` header (base64-encoded JSON) -> dict, or None.

    Tolerant of missing padding; rejects non-base64, non-JSON, non-object.
    """
    if not header_value or not isinstance(header_value, str):
        return None
    raw = header_value.strip()
    try:
        pad = "=" * (-len(raw) % 4)
        decoded = base64.b64decode(raw + pad, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        obj = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _authorization(payment):
    """Pull the EIP-3009 authorization fields out of an x402 payment payload."""
    payload = payment.get("payload") or {}
    auth = payload.get("authorization") or {}
    return auth


def payment_satisfies(payment, req):
    """
    PURE: does this decoded payment satisfy the requirements *at the protocol
    level*? Returns (ok, reason). This is Blackwall's job; the cryptographic
    validity (signature/balance) is the facilitator's and is NOT checked here.

    Checks: scheme, network, recipient (payTo) and amount (>= maxAmountRequired),
    and asset when the payment declares one.
    """
    if not isinstance(payment, dict):
        return False, "payment is not an object"
    if payment.get("scheme") != req["scheme"]:
        return False, "scheme mismatch"
    if payment.get("network") != req["network"]:
        return False, "network mismatch"

    auth = _authorization(payment)
    pay_to = auth.get("to")
    if not pay_to or not addresses_equal(pay_to, req["payTo"]):
        return False, "recipient (payTo) mismatch"

    # asset, if the payment declares it, must match.
    asset = payment.get("asset") or (payment.get("payload") or {}).get("asset")
    if asset is not None and not addresses_equal(asset, req["asset"]):
        return False, "asset mismatch"

    try:
        value = int(str(auth.get("value")))
    except (TypeError, ValueError):
        return False, "missing/invalid payment value"
    if value < int(req["maxAmountRequired"]):
        return False, "underpaid"

    return True, "ok"


def payment_nonce(payment):
    """A stable idempotency key for a payment (the EIP-3009 authorization nonce)."""
    return _authorization(payment).get("nonce")


# ===========================================================================
# Facilitator seam (signature/balance/on-chain replay live here -- not rebuilt)
# ===========================================================================
class MockFacilitator:
    """Approves (or, configurably, rejects) -- stands in for a real facilitator."""

    def __init__(self, approve=True, reason="mock-approve", settle_ok=True):
        self.approve = approve
        self.reason = reason
        self.settle_ok = settle_ok

    def verify(self, payment, requirements):
        return {"valid": bool(self.approve), "reason": self.reason}

    def settle(self, payment, requirements):
        return {"success": bool(self.settle_ok),
                "transaction": "0xmocktx" if self.settle_ok else None,
                "reason": "mock-settle" if self.settle_ok else "mock-settle-fail"}


class HttpFacilitator:
    """
    Real x402 facilitator over HTTP. POST {x402Version, paymentPayload,
    paymentRequirements} to /verify and /settle; normalize the x402 response
    shape (isValid/invalidReason, success/transaction/errorReason) into the seam
    contract the BillingGate expects ({valid,reason} / {success,transaction,
    reason}). A non-2xx / network error fails CLOSED (valid/success False).

    In production, base_url is a hosted facilitator; for dev/tests it is the
    local facilitator_sim. The facilitator does signature/balance/settlement --
    Blackwall does not (spec 5.4).
    """

    def __init__(self, base_url, timeout=10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, path, payment, requirements):
        import urllib.error
        import urllib.request
        body = json.dumps({"x402Version": X402_VERSION,
                           "paymentPayload": payment,
                           "paymentRequirements": requirements}).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=body,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Real facilitators return a STRUCTURED x402 error with a non-2xx
            # status (observed on Base Sepolia: 500 + {isValid:false,
            # invalidReason:...} when on-chain signature recovery reverts).
            # Surface that body instead of treating it as opaque/unreachable.
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                raise  # genuinely opaque error -> propagate -> fail closed

    def verify(self, payment, requirements):
        try:
            resp = self._post("/verify", payment, requirements)
        except Exception as e:  # network / HTTP / decode -> fail closed
            return {"valid": False, "reason": "facilitator unreachable: %s" % e}
        return {"valid": bool(resp.get("isValid")),
                "reason": resp.get("invalidReason") or "ok"}

    def settle(self, payment, requirements):
        try:
            resp = self._post("/settle", payment, requirements)
        except Exception as e:  # fail closed -- never report captured if unsure
            return {"success": False, "reason": "facilitator unreachable: %s" % e}
        return {"success": bool(resp.get("success")),
                "transaction": resp.get("transaction"),
                "reason": resp.get("errorReason") or "ok"}


# ===========================================================================
# Idempotency (do not serve one payment's verdict twice)
# ===========================================================================
class NonceLedger:
    """Thread-safe set of spent payment nonces. add() -> False if already spent."""

    def __init__(self):
        self._seen = set()
        self._lock = threading.Lock()

    def add(self, nonce):
        if nonce is None:
            return True  # nothing to dedupe on; let the facilitator's replay catch it
        with self._lock:
            if nonce in self._seen:
                return False
            self._seen.add(nonce)
            return True

    def release(self, nonce):
        """Un-reserve a nonce (e.g. settlement failed) so a legit retry works."""
        if nonce is None:
            return
        with self._lock:
            self._seen.discard(nonce)


# ===========================================================================
# Sessions (fund-once, many checks) -- x402 V2 reusable session
# ===========================================================================
def sign_session_token(session_id, key):
    """`<sid>.<hmac>` -- authenticates a session id without server lookup of secrets."""
    mac = hmac.new(key, session_id.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return "%s.%s" % (session_id, mac)


def verify_session_token(token, key):
    """Return the session_id iff the token's HMAC is valid, else None (constant-time)."""
    if not token or not isinstance(token, str) or "." not in token:
        return None
    sid, _, mac = token.rpartition(".")
    if not sid:
        return None
    expected = hmac.new(key, sid.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    if hmac.compare_digest(mac, expected):
        return sid
    return None


class SessionStore:
    """Thread-safe session credits: open(credits, ttl) -> token; consume(token)."""

    def __init__(self, key=None):
        self.key = key or os.urandom(16)
        self._sessions = {}  # sid -> {"credits": int, "expires": float}
        self._lock = threading.Lock()

    def open(self, credits, ttl_seconds, now=None):
        now = time.time() if now is None else now
        sid = os.urandom(12).hex()
        with self._lock:
            self._sessions[sid] = {"credits": int(credits),
                                   "expires": now + float(ttl_seconds)}
        return sign_session_token(sid, self.key)

    def consume(self, token, now=None):
        """Spend one credit. Returns (ok, remaining_or_reason)."""
        now = time.time() if now is None else now
        sid = verify_session_token(token, self.key)
        if sid is None:
            return False, "bad session token"
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return False, "unknown session"
            if s["expires"] <= now:
                self._sessions.pop(sid, None)
                return False, "session expired"
            if s["credits"] <= 0:
                return False, "session exhausted"
            s["credits"] -= 1
            return True, s["credits"]


# ===========================================================================
# Billing gate -- orchestrates the above for the HTTP layer
# ===========================================================================
class BillingConfig:
    def __init__(self, price="0.001", asset=BASE_USDC, network="base",
                 pay_to=None, decimals=6, session_credits=1000,
                 session_price=None, session_ttl=86400):
        if not is_evm_address(pay_to or ""):
            raise ValueError("BillingConfig.pay_to must be a valid EVM address")
        self.price_atomic = to_atomic(price, decimals)
        if self.price_atomic is None or self.price_atomic <= 0:
            raise ValueError("invalid price")
        self.asset = asset
        self.network = network
        self.pay_to = pay_to
        self.decimals = decimals
        self.session_credits = int(session_credits)
        # default: a session costs credits x per-call price (no bulk discount yet).
        self.session_price_atomic = (to_atomic(session_price, decimals)
                                     if session_price is not None
                                     else self.price_atomic * self.session_credits)
        self.session_ttl = session_ttl


class BillingResult:
    """paid -> serve; else status/body carry the 402 (or 401-style) response."""
    def __init__(self, paid, status=None, body=None, session_remaining=None,
                 via=None, settlement=None):
        self.paid = paid
        self.status = status
        self.body = body
        self.session_remaining = session_remaining
        self.via = via  # "payment" | "session"
        self.settlement = settlement  # settlement tx hash when paid per-call


class BillingGate:
    def __init__(self, config, facilitator=None, nonces=None, sessions=None):
        self.cfg = config
        self.facilitator = facilitator or MockFacilitator()
        self.nonces = nonces or NonceLedger()
        self.sessions = sessions or SessionStore()

    def _requirements(self, resource, price_atomic=None):
        return build_requirements(
            price_atomic if price_atomic is not None else self.cfg.price_atomic,
            self.cfg.pay_to, resource, asset=self.cfg.asset, network=self.cfg.network)

    def _challenge(self, resource, error=None, price_atomic=None):
        return BillingResult(
            False, status=402,
            body=make_402_body([self._requirements(resource, price_atomic)], error))

    def check(self, resource, x_payment=None, x_session=None, price_atomic=None):
        """
        Decide whether a forecast call is paid. Order: session token, else
        per-call X-PAYMENT, else a 402 challenge.
        """
        if x_session:
            ok, info = self.sessions.consume(x_session)
            if ok:
                return BillingResult(True, session_remaining=info, via="session")
            return self._challenge(resource, error="session: %s" % info)

        if x_payment:
            return self._verify_payment(resource, x_payment, price_atomic)

        return self._challenge(resource)

    def _verify_payment(self, resource, x_payment, price_atomic=None):
        req = self._requirements(resource, price_atomic)
        payment = decode_payment_header(x_payment)
        if payment is None:
            return self._challenge(resource, error="malformed X-PAYMENT header",
                                  price_atomic=price_atomic)
        ok, reason = payment_satisfies(payment, req)
        if not ok:
            return self._challenge(resource, error=reason, price_atomic=price_atomic)
        # Delegate signature/balance to the facilitator (not rebuilt here).
        v = self.facilitator.verify(payment, req)
        if not v.get("valid"):
            return self._challenge(resource,
                                  error="facilitator rejected: %s" % v.get("reason"),
                                  price_atomic=price_atomic)
        # Local idempotency: reserve the nonce BEFORE settling, so a replayed
        # payment can never trigger a second settlement.
        nonce = payment_nonce(payment)
        if not self.nonces.add(nonce):
            return self._challenge(resource, error="payment already spent (replay)",
                                  price_atomic=price_atomic)
        # Capture funds (facilitator). On failure, release the nonce so the agent
        # can retry the same payment rather than burning it.
        s = self.facilitator.settle(payment, req)
        if not s.get("success"):
            self.nonces.release(nonce)
            return self._challenge(resource,
                                  error="settlement failed: %s" % s.get("reason"),
                                  price_atomic=price_atomic)
        return BillingResult(True, via="payment", settlement=s.get("transaction"))

    def open_session(self, resource, x_payment):
        """A payment covering the session price opens a credit bundle; returns a token."""
        result = self._verify_payment(resource, x_payment,
                                      price_atomic=self.cfg.session_price_atomic)
        if not result.paid:
            return result
        token = self.sessions.open(self.cfg.session_credits, self.cfg.session_ttl)
        return BillingResult(True, status=200,
                             body={"session_token": token,
                                   "credits": self.cfg.session_credits,
                                   "ttl_seconds": self.cfg.session_ttl},
                             via="payment")
