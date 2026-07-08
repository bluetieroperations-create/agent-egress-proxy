#!/usr/bin/env python3
"""
x402.py -- Blackwall's own x402 billing layer (spec step 3).

Blackwall is itself an x402 resource: the calling agent pays per forecast (or
funds a session of many forecasts). The elegant symmetry -- the service that
judges x402 payments is itself paid via x402.

    Agent -> POST /v1/forecast-payment            (no payment)
    Blackwall -> 402 { x402Version:2, resource, accepts:[{amount, asset, network, payTo}], ... }
    Agent -> retry with `X-PAYMENT` / `PAYMENT-SIGNATURE: <base64 payload>`
    Blackwall -> verify (FACILITATOR) + match + anti-replay -> serve verdict

PROTOCOL VERSION: x402 v2 (x402Version == 2). The v2 wire format (per the
x402-foundation spec, specs/x402-specification-v2.md + transports-v2/http.md):

  * networks are CAIP-2 (`eip155:8453` for Base mainnet, `eip155:84532` for
    Base Sepolia), not the bare `base` / `base-sepolia` string.
  * each `accepts[]` entry uses `amount` (atomic units), NOT `maxAmountRequired`;
    `resource`/`description`/`mimeType` move OUT of each accept into a top-level
    `resource` ResourceInfo object on the PaymentRequired body.
  * the payment payload is `{x402Version:2, accepted:{<the chosen requirements>},
    payload:{signature, authorization:{from,to,value,validAfter,validBefore,
    nonce}}}` -- the EIP-3009 authorization nests under `payload.authorization`
    exactly as v1, so the replay/match core is unchanged.
  * the facilitator envelope stays `{x402Version, paymentPayload,
    paymentRequirements}` and its responses (`isValid`/`invalidReason`,
    `success`/`transaction`/`errorReason`) are version-independent.

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
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from addresses import addresses_equal, is_evm_address

X402_VERSION = 2
DEFAULT_SCHEME = "exact"          # EIP-3009 transferWithAuthorization
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"          # Base mainnet
BASE_SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"  # Base Sepolia (Circle testnet)

# CAIP-2 network identifiers (x402 v2 requires CAIP-2, not the bare chain name).
# The billing layer keeps the human name internally ("base"/"base-sepolia") and
# converts to CAIP-2 at the protocol edge (the 402 challenge + payment matching).
_CAIP2 = {
    "base": "eip155:8453",
    "base-mainnet": "eip155:8453",
    "base-sepolia": "eip155:84532",
    "ethereum": "eip155:1",
    "mainnet": "eip155:1",
}


def to_caip2(network):
    """Map a human/legacy network name to its CAIP-2 id. A value that already
    looks like CAIP-2 (`namespace:reference`) is passed through unchanged, so an
    operator can pin an exotic chain directly. Unknown bare names are returned
    as-is (fail-visible: the facilitator will reject `invalid_network` rather
    than us silently mislabeling the chain)."""
    if not network or not isinstance(network, str):
        return network
    n = network.strip()
    if ":" in n:                       # already CAIP-2 (e.g. eip155:8453)
        return n
    return _CAIP2.get(n.lower(), n)

# EIP-712 domain (name, version) of each supported asset -- read on-chain from
# the token's name()/version(). The "exact" scheme carries this in the 402's
# `extra` so the facilitator can reconstruct the domain and verify the EIP-3009
# signature. (USDC contracts' domains are stable.)
EIP712_DOMAINS = {
    BASE_USDC.lower():         {"name": "USD Coin", "version": "2"},
    BASE_SEPOLIA_USDC.lower(): {"name": "USDC", "version": "2"},
}

# The forecast endpoint's input schema + output example, embedded in the v2 402
# challenge's Bazaar extension so x402scan can INVOKE the endpoint (a missing
# input schema => the resource is marked non-invocable / skipped). Mirrors the
# openapi.json requestBody and the MCP tool's args.
DEFAULT_FORECAST_INPUT_SCHEMA = {
    "type": "object",
    "required": ["counterparty", "amount", "asset", "chain"],
    "properties": {
        "counterparty": {"type": "string"},
        "amount": {"type": "string"},
        "asset": {"type": "string"},
        "chain": {"type": "string"},
        "payer": {"type": "string"},
        "resource": {"type": "string"},
    },
}
DEFAULT_FORECAST_OUTPUT_EXAMPLE = {
    "verdict": "GO", "score": 0.99,
    "reasons": ["reputable counterparty", "price within norms"],
    "receipt_id": "bw_...",
}

# A CONCRETE example request body -- Coinbase CDP Bazaar's discovery extension
# (`bazaar.info`) wants example values, not a JSON Schema. x402scan reads the
# schema; CDP reads this example. We emit both (see build_bazaar_extension).
DEFAULT_FORECAST_INPUT_EXAMPLE = {
    "counterparty": "0x0000000000000000000000000000000000000abc",
    "amount": "2.50",
    "asset": "USDC",
    "chain": "base",
}


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
def build_requirements(price_atomic, pay_to, resource=None, asset=BASE_USDC,
                       network="base", scheme=DEFAULT_SCHEME,
                       max_timeout_seconds=120, description=None):
    """One entry of the v2 `accepts` array (x402 v2 PaymentRequirements).

    v2 vs v1: amount is `amount` (was `maxAmountRequired`); `network` is CAIP-2;
    the resource/description/mimeType are NOT here (they live in the top-level
    ResourceInfo on the PaymentRequired body -- see make_402_body). `resource`
    and `description` remain accepted args for call-site compatibility but are
    ignored at this level.
    """
    req = {
        "scheme": scheme,
        "network": to_caip2(network),
        "amount": str(price_atomic),
        "asset": asset,
        "payTo": pay_to,
        "maxTimeoutSeconds": max_timeout_seconds,
    }
    # `extra` carries the asset's EIP-712 domain for the exact scheme; the
    # facilitator needs it to verify the EIP-3009 signature (else it rejects with
    # missing_eip712_domain). Omitted for unknown assets (facilitator may read it
    # on-chain itself).
    domain = EIP712_DOMAINS.get((asset or "").lower())
    if domain:
        req["extra"] = dict(domain)
    return req


MAX_RESOURCE_URL = 2048  # cap the (attacker-controlled) resource url


def build_resource_info(url, description="Blackwall payment forecast",
                        mime_type="application/json", service_name="Blackwall",
                        tags=None):
    """The v2 top-level ResourceInfo object describing the protected resource.

    `serviceName` is capped at 32 printable-ASCII chars and `tags` at 5 entries
    (each <=32 chars) per the v2 spec, so a mis-set config can't produce a body
    a strict validator (x402scan) rejects.

    The `url` is the request's `resource` field, which is ATTACKER-CONTROLLED
    (the client sends it). It flows into the base64 PAYMENT-REQUIRED response
    header, so an unbounded url produces an oversized header that some proxies /
    clients silently drop (the discovery library warns HEADERS_OVERFLOW above
    16 KB). Cap it at MAX_RESOURCE_URL (the v2 iconUrl bound) so a crafted
    resource can't bloat the header."""
    if isinstance(url, str) and len(url) > MAX_RESOURCE_URL:
        url = url[:MAX_RESOURCE_URL]
    info = {"url": url}
    if description:
        info["description"] = description
    if mime_type:
        info["mimeType"] = mime_type
    if service_name:
        info["serviceName"] = str(service_name)[:32]
    if tags:
        info["tags"] = [str(t)[:32] for t in list(tags)[:5]]
    return info


def build_bazaar_extension(input_schema=None, output_example=None,
                           input_example=None):
    """The `extensions.bazaar` block that makes a v2 402 challenge discoverable.

    TWO ecosystems read this block in DIFFERENT shapes, so we emit BOTH:

    1. x402scan (@agentcash/discovery 1.7.5, extractSchemas2) reads a JSON SCHEMA:
        extensions.bazaar.schema.properties.input.properties.body   -> input schema
        extensions.bazaar.schema.properties.output.properties.example -> output example
       Missing it -> SCHEMA_INPUT_MISSING, resource marked `skipped`.

    2. Coinbase CDP Bazaar (declareDiscoveryExtension) reads a CONCRETE EXAMPLE:
        extensions.bazaar.info.input  = {type:"http", method, bodyType, body:<example>}
        extensions.bazaar.info.output = {example:<example>}
       Missing it -> CDP will NOT catalog the endpoint even after a settlement.
       (This is why a settled endpoint with only `.schema` never lists on Bazaar.)

    We build both under one `bazaar` key so a single 402 satisfies both catalogs."""
    bazaar = {}
    # --- x402scan: JSON Schema ---
    props = {}
    if input_schema is not None:
        props["input"] = {"properties": {"body": input_schema}}
    if output_example is not None:
        props["output"] = {"properties": {"example": output_example}}
    if props:
        bazaar["schema"] = {"properties": props}
    # --- CDP Bazaar: concrete example ---
    info = {}
    if input_example is not None:
        info["input"] = {"type": "http", "method": "POST",
                         "bodyType": "json", "body": input_example}
    if output_example is not None:
        info["output"] = {"example": output_example}
    if info:
        bazaar["info"] = info
    return {"bazaar": bazaar}


def make_402_body(requirements_list, error=None, resource=None, extensions=None):
    """The JSON body of a v2 402 Payment Required response (PaymentRequired).

    v2 shape: {x402Version:2, error?, resource:{ResourceInfo}, accepts:[...],
    extensions:{}}. `resource` is a ResourceInfo dict (build_resource_info); when
    omitted it is derived from the first requirement's `resource`-less accept and
    a bare url so the body still validates. `extensions` carries the Bazaar
    input/output schema (build_bazaar_extension) so the endpoint is INVOCABLE
    rather than skipped by x402scan.
    """
    body = {"x402Version": X402_VERSION}
    if error:
        body["error"] = error
    if resource is not None:
        body["resource"] = resource
    body["accepts"] = requirements_list
    body["extensions"] = extensions if isinstance(extensions, dict) else {}
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
    """Pull the EIP-3009 authorization out of a payment payload, type-safely.

    The nested payload/authorization are fully attacker-controlled (only the top
    level is guaranteed a dict by decode_payment_header). A non-dict here must
    return {} -- never raise -- so a crafted X-PAYMENT can't crash the gate."""
    if not isinstance(payment, dict):
        return {}
    payload = payment.get("payload")
    if not isinstance(payload, dict):
        return {}
    auth = payload.get("authorization")
    return auth if isinstance(auth, dict) else {}


def _accepted(payment):
    """The v2 chosen-requirements object (`accepted`), type-safely -> dict.

    v2 carries the client's chosen PaymentRequirements under `accepted`; v1 put
    scheme/network at the payment's top level. Return {} for a non-dict so
    scheme/network reads below can't raise on a crafted payload."""
    if not isinstance(payment, dict):
        return {}
    acc = payment.get("accepted")
    return acc if isinstance(acc, dict) else {}


def _req_amount(req):
    """The required atomic amount from a requirements dict, v2 (`amount`) with a
    v1 (`maxAmountRequired`) fallback so a mixed call site still works."""
    if "amount" in req:
        return req["amount"]
    return req.get("maxAmountRequired")


def payment_satisfies(payment, req):
    """
    PURE: does this decoded v2 payment satisfy the requirements *at the protocol
    level*? Returns (ok, reason). This is Blackwall's job; the cryptographic
    validity (signature/balance) is the facilitator's and is NOT checked here.

    Checks: scheme, network, recipient (payTo) and amount (>= required amount),
    and asset when the payment declares one. v2 reads scheme/network/asset from
    the payment's `accepted` block, falling back to the v1 top-level fields.
    """
    if not isinstance(payment, dict):
        return False, "payment is not an object"

    acc = _accepted(payment)
    scheme = acc.get("scheme", payment.get("scheme"))
    network = acc.get("network", payment.get("network"))
    if scheme != req["scheme"]:
        return False, "scheme mismatch"
    # Match networks in CAIP-2 space so a client that (legally) sends the bare
    # name still matches a CAIP-2 requirement, and vice-versa.
    if to_caip2(network) != to_caip2(req["network"]):
        return False, "network mismatch"

    auth = _authorization(payment)
    pay_to = auth.get("to")
    if not pay_to or not addresses_equal(pay_to, req["payTo"]):
        return False, "recipient (payTo) mismatch"

    # asset, if the payment declares it, must match. v2: accepted.asset;
    # v1 fallbacks: top-level asset or payload.asset.
    asset = (acc.get("asset") or payment.get("asset")
             or (payment.get("payload") or {}).get("asset"))
    if asset is not None and not addresses_equal(asset, req["asset"]):
        return False, "asset mismatch"

    try:
        value = int(str(auth.get("value")))
    except (TypeError, ValueError):
        return False, "missing/invalid payment value"
    required = int(_req_amount(req))
    # `exact` scheme (spec 6.1.2): value must EQUAL the required amount -- an
    # overpay is a dead-on-arrival acceptance the real facilitator rejects, so we
    # fail it here with a clear reason. (An `upto` scheme, if added, allows <=.)
    if req.get("scheme") == "exact":
        if value != required:
            return False, "underpaid" if value < required else "overpaid"
    elif value < required:
        return False, "underpaid"

    # A real EIP-3009 authorization always carries a nonce; one without it is
    # malformed. Reject it here so Blackwall's local replay guard never has to
    # wave a nonce-less payment through (NonceLedger.add(None) is a no-op).
    if not auth.get("nonce"):
        return False, "missing authorization nonce"

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

    def __init__(self, base_url, timeout=20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _auth_headers(self, path):
        """Per-request auth headers -- empty for a keyless facilitator. Subclasses
        that authenticate (CdpFacilitator) override this; `path` is passed so the
        token can bind to the exact endpoint (/verify vs /settle)."""
        return {}

    def _post(self, path, payment, requirements):
        import urllib.error
        import urllib.request
        body = json.dumps({"x402Version": X402_VERSION,
                           "paymentPayload": payment,
                           "paymentRequirements": requirements}).encode("utf-8")
        # Some facilitators sit behind Cloudflare, which tarpits/blocks the
        # default Python-urllib UA (manifests as a timeout). Send a browser UA.
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json",
                   "User-Agent": "Mozilla/5.0 (Blackwall x402 facilitator client)"}
        headers.update(self._auth_headers(path))
        req = urllib.request.Request(self.base_url + path, data=body,
                                     headers=headers)
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


# CDP (Coinbase Developer Platform) mainnet facilitator.
CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
_CDP_HOST_SUFFIX = "cdp.coinbase.com"


def _is_cdp_host(url):
    """
    True iff `url`'s REGISTRABLE HOST is cdp.coinbase.com or a subdomain of it.

    This is the security boundary for choose_facilitator: it decides whether a
    CDP-authenticated Bearer JWT may be minted for and sent to `url`. It must be
    a real hostname parse -- a substring test ("cdp.coinbase.com" in url) is
    bypassable by suffix hosts (cdp.coinbase.com.evil.com), userinfo tricks
    (cdp.coinbase.com@evil.com), and substrings in the path/query, any of which
    would leak an auth token to an attacker-controlled host.
    """
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    return host == _CDP_HOST_SUFFIX or host.endswith("." + _CDP_HOST_SUFFIX)


class CdpFacilitator(HttpFacilitator):
    """
    The CDP-hosted facilitator. Same verify/settle contract as HttpFacilitator,
    but every request carries a per-call `Authorization: Bearer <JWT>` (EdDSA,
    bound to METHOD+host+path, 120s TTL -- see cdp_auth.build_cdp_jwt). This is
    the ONLY facilitator whose settlements Coinbase Bazaar catalogs, so it is what
    turns a settled payment into a marketplace listing; keyless community
    facilitators settle on Base mainnet but never list.

    Credentials come from CDP_API_KEY_ID / CDP_API_KEY_SECRET (Ed25519 secret,
    base64). If either is missing the token can't be minted and requests fail
    CLOSED (verify/settle return valid/success False via the base class's
    exception handling) rather than hitting CDP unauthenticated.
    """

    def __init__(self, key_id, key_secret, base_url=CDP_FACILITATOR_URL,
                 timeout=20.0):
        super().__init__(base_url, timeout=timeout)
        self.key_id = key_id
        self.key_secret = key_secret

    def _auth_headers(self, path):
        # Import here so the module has no hard dependency on cdp_auth unless the
        # CDP path is actually used.
        from cdp_auth import build_cdp_jwt
        token = build_cdp_jwt(self.key_id, self.key_secret, "POST",
                              self.base_url + path)
        return {"Authorization": "Bearer " + token}


def choose_facilitator(facilitator_url, cdp_id, cdp_secret):
    """
    Pick the facilitator from config, returning (facilitator_or_None, note).

    When CDP creds are present we use the AUTHENTICATED CdpFacilitator and route
    it to the CDP endpoint -- even if BLACKWALL_FACILITATOR still holds a stale
    community URL. Sending a CDP Bearer JWT to a community facilitator would both
    misroute settlement and leak an auth token, so a non-CDP `facilitator_url` is
    explicitly ignored (with a note) rather than silently honored. An explicit
    CDP host in `facilitator_url` (e.g. a staging endpoint) IS honored.
    """
    if cdp_id and cdp_secret:
        if facilitator_url and _is_cdp_host(facilitator_url):
            url = facilitator_url
            note = "CDP facilitator (authenticated) at %s -- Bazaar-eligible" % url
        else:
            url = CDP_FACILITATOR_URL
            if facilitator_url:
                note = ("CDP creds set -- using CDP facilitator %s and IGNORING "
                        "non-CDP BLACKWALL_FACILITATOR=%s" % (url, facilitator_url))
            else:
                note = "CDP facilitator (authenticated) at %s -- Bazaar-eligible" % url
        return CdpFacilitator(cdp_id, cdp_secret, base_url=url), note
    if facilitator_url:
        return (HttpFacilitator(facilitator_url),
                "HTTP facilitator at %s (keyless -- settles but NOT Bazaar-listed)"
                % facilitator_url)
    return None, "built-in mock facilitator (no --facilitator, no CDP creds)"


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
class PricingPolicy:
    """
    Value-aligned per-forecast pricing: the fee tracks the amount-at-risk (the
    payment being forecast), NOT a flat rate.

    Priced on AMOUNT, not the verdict -- x402 is pay-before-serve, so the verdict
    isn't known when the 402 is issued; the amount IS known (it's in the request).
    The agent can't game it: declaring a smaller amount to dodge the fee just gets
    a verdict for that smaller amount (which is what the price-anomaly check uses).

      * amount <= free_below  -> FREE (no 402). The loss-leader that feeds the
        reputation moat -- and it kills the "why pay a cent to protect a cent"
        objection for micro-payments (most of today's x402 traffic).
      * else  -> bps of the amount, clamped to [min_fee, max_fee]. A big payment
        carries a (still tiny) fee proportional to the loss being prevented.
      * unknown amount -> min_fee (can't assess value; charge the floor).
    """

    def __init__(self, decimals=6, free_below="1.00", bps=10,
                 min_fee="0.001", max_fee="0.10"):
        self.decimals = int(decimals)
        self.free_below = Decimal(str(free_below))
        self.bps = Decimal(str(bps))            # basis points: 10 = 0.1%
        self.min_fee = Decimal(str(min_fee))
        self.max_fee = Decimal(str(max_fee))
        self._q = Decimal(1).scaleb(-self.decimals)  # quantization unit

    def fee_atomic(self, amount_at_risk):
        """Return the fee in atomic units for protecting `amount_at_risk` (0=free)."""
        try:
            amt = Decimal(str(amount_at_risk))
            if not amt.is_finite():
                amt = None
        except (InvalidOperation, ValueError, ArithmeticError, TypeError):
            amt = None
        if amt is None or amt <= 0:
            fee = self.min_fee
        elif amt <= self.free_below:
            return 0
        else:
            fee = amt * self.bps / Decimal(10000)
            if fee < self.min_fee:
                fee = self.min_fee
            elif fee > self.max_fee:
                fee = self.max_fee
        fee = fee.quantize(self._q, rounding=ROUND_HALF_UP)
        return int(fee * (Decimal(10) ** self.decimals))


class BillingConfig:
    def __init__(self, price="0.001", asset=BASE_USDC, network="base",
                 pay_to=None, decimals=6, session_credits=1000,
                 session_price=None, session_ttl=86400,
                 service_name="Blackwall",
                 resource_description="Blackwall payment forecast",
                 resource_tags=("x402", "payments", "risk"),
                 input_schema=None, output_example=None, input_example=None,
                 resource_url=None):
        if not is_evm_address(pay_to or ""):
            raise ValueError("BillingConfig.pay_to must be a valid EVM address")
        # Canonical URL of THIS paid endpoint (e.g. the forecast-payment URL). When
        # set, it is injected as paymentPayload.resource on the verify/settle call
        # so the CDP facilitator knows which resource to catalog in the Bazaar
        # (x402 v2 moved `resource` out of paymentRequirements, so the facilitator
        # sees it nowhere else). None => no injection (unchanged behavior).
        self.resource_url = resource_url or None
        self.service_name = service_name
        self.resource_description = resource_description
        self.resource_tags = resource_tags
        # The forecast input schema (x402scan) + a concrete input example (CDP
        # Bazaar) for the 402's discovery extension, so BOTH catalogs can invoke.
        self.input_schema = (input_schema if input_schema is not None
                             else DEFAULT_FORECAST_INPUT_SCHEMA)
        self.output_example = (output_example if output_example is not None
                              else DEFAULT_FORECAST_OUTPUT_EXAMPLE)
        self.input_example = (input_example if input_example is not None
                             else DEFAULT_FORECAST_INPUT_EXAMPLE)
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
    def __init__(self, config, facilitator=None, nonces=None, sessions=None,
                 pricing=None):
        self.cfg = config
        self.facilitator = facilitator or MockFacilitator()
        self.nonces = nonces or NonceLedger()
        self.sessions = sessions or SessionStore()
        self.pricing = pricing  # PricingPolicy or None (flat cfg.price)

    def _price_for(self, amount_at_risk):
        if self.pricing is not None:
            return self.pricing.fee_atomic(amount_at_risk)
        return self.cfg.price_atomic

    def _requirements(self, resource, price_atomic=None):
        return build_requirements(
            price_atomic if price_atomic is not None else self.cfg.price_atomic,
            self.cfg.pay_to, resource, asset=self.cfg.asset, network=self.cfg.network)

    def _challenge(self, resource, error=None, price_atomic=None):
        # v2: the resource/description/mimeType live in a top-level ResourceInfo,
        # not inside each accept. Build it from the resource URL being paid for.
        info = build_resource_info(
            resource,
            description=self.cfg.resource_description,
            service_name=self.cfg.service_name,
            tags=self.cfg.resource_tags)
        # Bazaar input/output schema -> the challenge is INVOCABLE (x402scan does
        # not mark it `skipped` for a missing input schema).
        ext = None
        if self.cfg.input_schema is not None:
            ext = build_bazaar_extension(self.cfg.input_schema,
                                         self.cfg.output_example,
                                         self.cfg.input_example)
        return BillingResult(
            False, status=402,
            body=make_402_body([self._requirements(resource, price_atomic)],
                               error, resource=info, extensions=ext))

    def check(self, resource, x_payment=None, x_session=None, amount_at_risk=None,
              price_atomic=None):
        """
        Decide whether a forecast call is paid. With a PricingPolicy the price is
        derived from `amount_at_risk` (value-aligned); micro-payments are FREE.
        Order: free fast-path, session token, per-call X-PAYMENT, else a 402.
        """
        if price_atomic is None:
            price_atomic = self._price_for(amount_at_risk)

        # Value-aligned free path: a fee of 0 (micro-payment) is served WITHOUT a
        # 402 and without consuming a session credit.
        if price_atomic <= 0:
            return BillingResult(True, via="free")

        if x_session:
            ok, info = self.sessions.consume(x_session)
            if ok:
                return BillingResult(True, session_remaining=info, via="session")
            # Carry the price override so the fallback 402 advertises the real
            # (premium) price -- otherwise the agent is quoted the base price,
            # pays it, and gets rejected as underpaid (unsatisfiable loop).
            return self._challenge(resource, error="session: %s" % info,
                                  price_atomic=price_atomic)

        if x_payment:
            return self._verify_payment(resource, x_payment, price_atomic)

        return self._challenge(resource, price_atomic=price_atomic)

    def _verify_payment(self, resource, x_payment, price_atomic=None):
        req = self._requirements(resource, price_atomic)
        payment = decode_payment_header(x_payment)
        if payment is None:
            return self._challenge(resource, error="malformed X-PAYMENT header",
                                  price_atomic=price_atomic)
        # CDP Bazaar catalogs the PAID endpoint from paymentPayload.resource on the
        # verify/settle call (v2 moved `resource` out of paymentRequirements, so the
        # facilitator never sees it otherwise). Inject our canonical endpoint URL so
        # a CDP settlement lists THIS service; community facilitators ignore it. NB:
        # NOT the `resource` arg -- that is the agent-supplied thing they're paying
        # ABOUT, not the endpoint being paid for. `resource` is metadata, not part
        # of the signed EIP-3009 authorization, so setting it can't break the sig.
        if self.cfg.resource_url:
            payment["resource"] = self.cfg.resource_url
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
