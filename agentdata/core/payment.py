"""
payment.py -- x402 SELLER-side gating.

The commodity we deliberately do NOT reinvent is the settlement network -- we
speak the x402 handshake and delegate verify/settle to a facilitator
(Coinbase / Cloudflare in prod). This module:

  * builds the `402 Payment Required` offer for a product,
  * parses the buyer's base64 `X-PAYMENT` retry header,
  * verifies + settles via a pluggable Facilitator.

Facilitators:
  * MockFacilitator  -- accepts any well-formed payment that authorizes >= the
    required amount on the right network. Runs offline; used by tests and dev.
  * HttpFacilitator  -- POSTs to a real facilitator's /verify and /settle.
    Wired via stdlib urllib; only used when config.facilitator != "mock".

NOTE: this NEVER holds a buyer's key and never signs -- it validates an
already-signed payment and asks the facilitator to settle it. Mirrors the
"allow/block, never forge" boundary from the buyer-side budget proxy.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections import namedtuple

from .money import parse_atomic_int

X402_VERSION = 1

Requirements = namedtuple("Requirements", [
    "scheme", "network", "max_amount_required", "asset", "pay_to",
    "resource", "description", "max_timeout_seconds"])

VerifyResult = namedtuple("VerifyResult", ["valid", "reason", "amount_atomic"])
SettleResult = namedtuple("SettleResult", ["settled", "tx", "reason"])


def build_requirements(product, config, resource):
    """Compose the single payment offer for a product call."""
    return Requirements(
        scheme="exact",
        network=config.network,
        max_amount_required=str(product.price_atomic(config.asset_decimals)),
        asset=config.asset,
        pay_to=config.pay_to,
        resource=resource,
        description="%s/%s" % (product.name, product.action),
        max_timeout_seconds=60,
    )


def offer_body(requirements, error=None):
    """The JSON body served with a 402 (or on payment failure)."""
    body = {
        "x402Version": X402_VERSION,
        "accepts": [{
            "scheme": requirements.scheme,
            "network": requirements.network,
            "maxAmountRequired": requirements.max_amount_required,
            "asset": requirements.asset,
            "payTo": requirements.pay_to,
            "resource": requirements.resource,
            "description": requirements.description,
            "maxTimeoutSeconds": requirements.max_timeout_seconds,
        }],
    }
    if error:
        body["error"] = error
    return body


def parse_payment_header(x_payment):
    """Decode a base64 `X-PAYMENT` header -> dict, or None (fail closed)."""
    if not x_payment or not isinstance(x_payment, str):
        return None
    raw = x_payment.strip()
    if len(raw) > 32 * 1024:
        return None
    pad = (-len(raw)) % 4
    try:
        decoded = base64.b64decode(raw + ("=" * pad), validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        doc = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _payment_amount(doc):
    """Pull the authorized atomic amount from a decoded payment payload."""
    payload = doc.get("payload")
    if isinstance(payload, dict):
        auth = payload.get("authorization")
        if isinstance(auth, dict):
            v = parse_atomic_int(auth.get("value"))
            if v is not None:
                return v
    for key in ("value", "amount", "maxAmountRequired"):
        if key in doc:
            v = parse_atomic_int(doc.get(key))
            if v is not None:
                return v
    return None


class Facilitator:
    def verify(self, payment_doc, requirements):
        raise NotImplementedError

    def settle(self, payment_doc, requirements):
        raise NotImplementedError


class MockFacilitator(Facilitator):
    """Offline facilitator. Valid iff the payment is well-formed, targets the
    right network, and authorizes at least the required amount. settle()
    returns a deterministic pseudo-tx derived from the payment (no RNG/clock,
    so tests are reproducible)."""

    def verify(self, payment_doc, requirements):
        if not isinstance(payment_doc, dict):
            return VerifyResult(False, "malformed-payment", 0)
        net = payment_doc.get("network")
        if net is not None and str(net).lower() != requirements.network.lower():
            return VerifyResult(False, "network-mismatch", 0)
        amt = _payment_amount(payment_doc)
        if amt is None:
            return VerifyResult(False, "no-amount", 0)
        required = int(requirements.max_amount_required)
        if amt < required:
            return VerifyResult(False, "insufficient-amount", amt)
        return VerifyResult(True, "ok", amt)

    def settle(self, payment_doc, requirements):
        digest = hashlib.sha256(
            json.dumps(payment_doc, sort_keys=True).encode("utf-8")
            + requirements.resource.encode("utf-8")).hexdigest()
        return SettleResult(True, "0xmock" + digest[:56], "ok")


class HttpFacilitator(Facilitator):
    """Real facilitator over HTTP (Coinbase/Cloudflare-style /verify + /settle).
    Uses stdlib urllib so there is no third-party dependency. Only exercised
    when configured; documented shape, adjust paths to your facilitator."""

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def _post(self, path, body):
        import urllib.request
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def verify(self, payment_doc, requirements):
        try:
            r = self._post("/verify", {"payment": payment_doc,
                                       "requirements": requirements._asdict()})
        except Exception as e:                       # noqa: BLE001 (fail closed)
            return VerifyResult(False, "facilitator-error:%s" % e, 0)
        return VerifyResult(bool(r.get("isValid")), r.get("reason", ""),
                            parse_atomic_int(r.get("amount")) or 0)

    def settle(self, payment_doc, requirements):
        try:
            r = self._post("/settle", {"payment": payment_doc,
                                       "requirements": requirements._asdict()})
        except Exception as e:                       # noqa: BLE001
            return SettleResult(False, None, "facilitator-error:%s" % e)
        return SettleResult(bool(r.get("success")), r.get("transaction"),
                            r.get("reason", ""))


def build_facilitator(config):
    if config.facilitator == "mock":
        return MockFacilitator()
    if not config.facilitator_url:
        raise ValueError(
            "facilitator=%r requires facilitator_url" % config.facilitator)
    return HttpFacilitator(config.facilitator_url)
