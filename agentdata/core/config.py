"""
config.py -- platform configuration (seller wallet, network, facilitator, ...).

Loaded from a JSON file and/or environment. Kept tiny and explicit; every
field has a safe default so the platform runs out-of-the-box in dev mode
(facilitator=mock, require_payment can be toggled).
"""
from __future__ import annotations

import json
import os


class Config:
    __slots__ = ("pay_to", "network", "asset", "asset_decimals",
                 "facilitator", "facilitator_url", "require_payment",
                 "db_path", "log_path", "host", "port", "enricher",
                 "resource_base")

    def __init__(self, **kw):
        # Seller wallet the buyer pays. In dev this is a placeholder.
        self.pay_to = kw.get("pay_to", "0xSELLER_WALLET_PLACEHOLDER")
        self.network = kw.get("network", "base")
        self.asset = kw.get("asset", "USDC")
        self.asset_decimals = int(kw.get("asset_decimals", 6))
        # mock | coinbase | cloudflare  (only 'mock' runs with no network/keys)
        self.facilitator = kw.get("facilitator", "mock")
        self.facilitator_url = kw.get("facilitator_url", "")
        # In dev you may run with payment gating OFF to iterate on data logic.
        self.require_payment = bool(kw.get("require_payment", True))
        self.db_path = kw.get("db_path", "agentdata_cache.sqlite")
        self.log_path = kw.get("log_path", "agentdata_sales.log")
        self.host = kw.get("host", "127.0.0.1")
        self.port = int(kw.get("port", 8402))
        self.enricher = kw.get("enricher", "stub")   # stub | claude
        # Base URI used to build the x402 `resource` field of an offer.
        self.resource_base = kw.get("resource_base", "https://api.local")

    @classmethod
    def load(cls, path=None):
        data = {}
        path = path or os.environ.get("AGENTDATA_CONFIG")
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        # env overrides (only the operationally common ones)
        for env, key, cast in (
            ("AGENTDATA_PAY_TO", "pay_to", str),
            ("AGENTDATA_NETWORK", "network", str),
            ("AGENTDATA_FACILITATOR", "facilitator", str),
            ("AGENTDATA_REQUIRE_PAYMENT", "require_payment",
             lambda v: v.lower() in ("1", "true", "yes")),
            ("AGENTDATA_PORT", "port", int),
            ("AGENTDATA_DB", "db_path", str),
            ("AGENTDATA_LOG", "log_path", str),
            ("AGENTDATA_ENRICHER", "enricher", str),
        ):
            if env in os.environ:
                data[key] = cast(os.environ[env])
        return cls(**data)
