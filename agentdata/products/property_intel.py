"""
property_intel.py -- PRODUCT #1: Property / Parcel Intelligence.

One paid call: `{address}` -> clean, enriched property record with permit and
violation history, zoning/flood, an LLM risk summary, derived flags, a
confidence score, and source attributions.

The raw records are scattered and messy (that is the moat); the enriched
verdict is the $0.20 an agent pays instead of aggregating it themselves.

This file is intentionally small: all the money/transport/cache/analytics
machinery lives in core/. Adding product #2 or #3 means writing a sibling file
like this one and registering it.
"""
from __future__ import annotations

from ..core.product import DataProduct, ProductError

_SOURCE_KEY = "property"     # ctx.sources[_SOURCE_KEY] -> a DataSource


class PropertyIntel(DataProduct):
    name = "property"
    action = "lookup"
    version = "1"
    price = "0.20"
    cache_ttl_seconds = 7 * 24 * 3600      # property records move slowly
    description = ("Enriched property/parcel intelligence for a US address: "
                  "permits, code violations, zoning, flood zone, ownership, "
                  "last sale, plus a risk summary, flags, and confidence.")
    input_fields = {
        "address": {"type": "string", "required": True,
                    "desc": "Full street address, e.g. '123 Main St, Austin TX'"},
    }

    # address is the identity -> normalize it hard so equivalent spellings
    # share a cache entry (the flywheel).
    def normalize_input(self, payload):
        addr = str(payload.get("address", "")).strip().lower()
        addr = " ".join(addr.replace(",", " ").split())     # collapse whitespace/commas
        return {"address": addr}

    def subject_label(self, payload):
        return "Property at %s" % payload.get("address", "unknown")

    def validate_input(self, payload):
        payload = super().validate_input(payload)
        if len(str(payload.get("address", "")).strip()) < 5:
            raise ProductError("address looks too short to be valid")
        return payload

    def fetch(self, payload, ctx):
        source = ctx.sources.get(_SOURCE_KEY)
        if source is None:
            raise ProductError("no property data source configured")
        raw = source.fetch({"address": payload["address"]})
        if not raw:
            return {"found": False, "address": payload["address"]}
        raw["found"] = True
        raw["address"] = payload["address"]
        raw["_sources"] = [source.name]
        return raw

    # derived risk/interest flags -> feed both the summary and the agent's own
    # decision logic.
    def compute_flags(self, raw):
        if not raw.get("found"):
            return {"no_records_found": True}
        return {
            "open_permits": raw.get("open_permits", 0) > 0,
            "has_code_violations": raw.get("code_violations", 0) > 0,
            "unpermitted_work_suspected": bool(raw.get("unpermitted_work_suspected")),
            "high_flood_risk": str(raw.get("flood_zone", "")).startswith(("A", "AE")),
        }

    def enrich(self, raw, payload, ctx):
        result = super().enrich(raw, payload, ctx)
        # attach source attribution + a machine-actionable recommendation
        result.meta["sources"] = raw.get("_sources", [])
        flags = result.data.get("flags", {})
        risk = sum(1 for on in flags.values() if on)
        result.data["recommendation"] = (
            "review" if risk >= 2 else "proceed" if risk == 0 else "note")
        result.data.pop("_sources", None)
        return result
