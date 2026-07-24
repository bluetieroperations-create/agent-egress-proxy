"""
product.py -- the DataProduct plug-in contract and the registry.

A product is the unit of extension. Property Intelligence is the first; Legal
Citation Verification and Semantic Change-Watch drop in later by subclassing
DataProduct -- no change to the server, MCP layer, payment gating, cache, or
analytics. That is the whole point of laying the foundation first.

Lifecycle of one paid call (orchestrated by the server, not the product):
    validate_input -> [x402 gate] -> cache_key -> fetch (cache-aware) ->
    enrich -> ProductResult -> [settle] -> JSON + sales-log line

A product implements the data-specific bits (validate/fetch/enrich); the
platform owns money, transport, caching, and logging.
"""
from __future__ import annotations

import hashlib
import json

from . import money


class ProductError(Exception):
    """Raised by a product for a bad request (mapped to HTTP 400)."""


class Context:
    """Shared services handed to every product call."""
    __slots__ = ("cache", "enricher", "sources", "logstore", "config")

    def __init__(self, cache, enricher, sources, logstore, config):
        self.cache = cache
        self.enricher = enricher
        self.sources = sources          # {name: DataSource}
        self.logstore = logstore
        self.config = config


class ProductResult:
    __slots__ = ("data", "meta")

    def __init__(self, data, meta=None):
        self.data = data
        self.meta = meta or {}

    def to_dict(self):
        return {"data": self.data, "meta": self.meta}


class DataProduct:
    # ---- identity / pricing (override in subclass) ----
    name = "base"
    action = "lookup"
    version = "1"
    price = "0.20"                       # decimal USDC string
    cache_ttl_seconds = 24 * 3600
    description = "base product"
    # input schema: {field: {"type": "string", "required": bool, "desc": str}}
    input_fields = {}

    # ---- pricing helper ----
    def price_atomic(self, decimals):
        return money.to_atomic(self.price, decimals)

    # ---- input validation (override to add domain rules) ----
    def validate_input(self, payload):
        if not isinstance(payload, dict):
            raise ProductError("body must be a JSON object")
        for field, spec in self.input_fields.items():
            if spec.get("required") and not payload.get(field):
                raise ProductError("missing required field: %s" % field)
        return payload

    # ---- cache key: product identity + normalized input ----
    def cache_key(self, payload):
        norm = self.normalize_input(payload)
        blob = json.dumps([self.name, self.version, norm],
                          sort_keys=True, separators=(",", ":"))
        return "%s:%s:%s" % (self.name, self.version,
                             hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32])

    def normalize_input(self, payload):
        """Canonicalize input so equivalent queries share a cache entry.
        Default: lowercase+strip string values. Override for domain rules
        (e.g. address normalization)."""
        out = {}
        for k in sorted(payload):
            v = payload[k]
            out[k] = v.strip().lower() if isinstance(v, str) else v
        return out

    # ---- resource URI (for the x402 offer + logging) ----
    def resource_uri(self, config, payload):
        return "%s/v1/%s/%s" % (config.resource_base.rstrip("/"),
                                self.name, self.action)

    # ---- data acquisition (override) ----
    def fetch(self, payload, ctx):
        """Return the raw normalized facts for `payload`. Should use
        ctx.sources; the server handles cache read/write around this."""
        raise NotImplementedError

    # ---- enrichment (override; default builds a generic summary) ----
    def enrich(self, raw, payload, ctx):
        flags = self.compute_flags(raw)
        subject = self.subject_label(payload)
        summary = ctx.enricher.summarize(
            kind=self.name, subject=subject, facts=raw, flags=flags)
        return ProductResult(
            data={**raw, "flags": flags, "summary": summary["summary"]},
            meta={"confidence": summary["confidence"],
                  "model": summary["model"],
                  "product": self.name, "version": self.version})

    def compute_flags(self, raw):
        """Boolean risk/interest flags derived from raw facts. Override."""
        return {}

    def subject_label(self, payload):
        return self.name

    # ---- MCP tool schema (derived from input_fields) ----
    def tool_schema(self):
        props, required = {}, []
        for field, spec in self.input_fields.items():
            props[field] = {"type": spec.get("type", "string"),
                            "description": spec.get("desc", "")}
            if spec.get("required"):
                required.append(field)
        return {
            "name": "%s_%s" % (self.name, self.action),
            "description": self.description,
            "inputSchema": {"type": "object", "properties": props,
                            "required": required},
        }


class Registry:
    def __init__(self):
        self._by_name = {}

    def register(self, product):
        key = (product.name, product.action)
        if key in self._by_name:
            raise ValueError("duplicate product %s/%s" % key)
        self._by_name[key] = product
        return product

    def get(self, name, action):
        return self._by_name.get((name, action))

    def all(self):
        return list(self._by_name.values())
