"""
platform.py -- wiring + the one canonical request pipeline.

Both transports (HTTP in server.py, MCP in mcp.py) call Platform.serve_call so
the money/cache/enrich/log lifecycle is defined exactly once. Adding a product
or a transport never duplicates this logic.

serve_call stages:
    lookup product -> validate -> [x402 gate] -> cache-read ->
    fetch+enrich (on miss) -> cache-write -> [settle] -> log -> Outcome
"""
from __future__ import annotations

import base64
import json
import time

from . import payment as pay
from .config import Config
from .enrich import build_enricher
from .product import Context, ProductError, Registry
from .storage import LogStore, MemoryCache, SqliteCache


class Outcome:
    """Transport-neutral result of a call."""
    __slots__ = ("status", "body", "headers", "kind")

    def __init__(self, status, body, headers=None, kind="ok"):
        self.status = status          # HTTP-style status code
        self.body = body              # dict (JSON-serializable)
        self.headers = headers or {}
        self.kind = kind              # ok | payment_required | error | not_found

    @property
    def paid_ok(self):
        return self.kind == "ok" and self.status == 200


class Platform:
    def __init__(self, config, registry, cache=None, enricher=None,
                 sources=None, logstore=None, facilitator=None):
        self.config = config
        self.registry = registry
        self.cache = cache if cache is not None else (
            SqliteCache(config.db_path) if config.db_path else MemoryCache())
        self.enricher = enricher or build_enricher(config.enricher)
        self.sources = sources or {}
        self.logstore = logstore or LogStore(config.log_path)
        self.facilitator = facilitator or pay.build_facilitator(config)
        self.ctx = Context(self.cache, self.enricher, self.sources,
                           self.logstore, config)

    # -- the pipeline --------------------------------------------------------
    def serve_call(self, name, action, payload, x_payment=None, now=None):
        now = time.time() if now is None else now
        product = self.registry.get(name, action)
        if product is None:
            return Outcome(404, {"error": "no such product: %s/%s" % (name, action)},
                           kind="not_found")

        # 1. validate input
        try:
            payload = product.validate_input(payload)
        except ProductError as e:
            return Outcome(400, {"error": str(e)}, kind="error")

        resource = product.resource_uri(self.config, payload)
        requirements = pay.build_requirements(product, self.config, resource)

        # 2. x402 gate
        settle_tx = None
        if self.config.require_payment:
            gate = self._gate(x_payment, requirements)
            if gate is not None:                 # a 402 Outcome
                self._log(product, payload, resource, "unpaid",
                          gate.body.get("error"), now, None, False)
                return gate
            settle = self.facilitator.settle(
                pay.parse_payment_header(x_payment), requirements)
            if not settle.settled:
                out = self._offer(requirements, "settlement-failed:%s" % settle.reason)
                self._log(product, payload, resource, "settle_fail",
                          settle.reason, now, None, False)
                return out
            settle_tx = settle.tx

        # 3. cache-aware fetch + enrich
        key = product.cache_key(payload)
        cached = self.cache.get(key, now=now)
        if cached is not None:
            body, cache_hit = cached, True
        else:
            try:
                raw = product.fetch(payload, self.ctx)
                result = product.enrich(raw, payload, self.ctx)
            except ProductError as e:
                return Outcome(400, {"error": str(e)}, kind="error")
            body = result.to_dict()
            self.cache.set(key, body, ttl_seconds=product.cache_ttl_seconds, now=now)
            cache_hit = False

        headers = {}
        if settle_tx:
            headers["X-PAYMENT-RESPONSE"] = base64.b64encode(
                json.dumps({"success": True, "transaction": settle_tx,
                            "network": self.config.network}).encode()).decode()

        self._log(product, payload, resource, "served", None, now, settle_tx,
                  cache_hit)
        out_body = dict(body)
        out_body.setdefault("meta", {})["cache_hit"] = cache_hit
        return Outcome(200, out_body, headers=headers, kind="ok")

    # -- helpers -------------------------------------------------------------
    def _gate(self, x_payment, requirements):
        """Return a 402 Outcome if payment is missing/invalid, else None."""
        if not x_payment:
            return self._offer(requirements, None)
        doc = pay.parse_payment_header(x_payment)
        if doc is None:
            return self._offer(requirements, "malformed-x-payment")
        v = self.facilitator.verify(doc, requirements)
        if not v.valid:
            return self._offer(requirements, v.reason)
        return None

    def _offer(self, requirements, error):
        return Outcome(402, pay.offer_body(requirements, error),
                       headers={"Content-Type": "application/json"},
                       kind="payment_required")

    def _log(self, product, payload, resource, decision, reason, now, tx, cache_hit):
        self.logstore.record({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "product": product.name, "action": product.action,
            "resource": resource, "decision": decision, "reason": reason,
            "price": product.price, "asset": self.config.asset,
            "network": self.config.network, "tx": tx, "cache_hit": cache_hit,
        })


# ---- default assembly: config -> a ready Platform with product #1 wired -----
def build_default_platform(config=None):
    from ..products.property_intel import PropertyIntel
    from ..data_sources.property_stub import StubPropertySource

    config = config or Config.load()
    registry = Registry()
    registry.register(PropertyIntel())
    sources = {"property": StubPropertySource()}
    # Future products register here and add their own sources:
    #   registry.register(LegalCitation()); sources["legal"] = CourtListenerSource()
    #   registry.register(ChangeWatch());   sources["watch"] = CrawlSource()
    return Platform(config, registry, sources=sources)
