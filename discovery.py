#!/usr/bin/env python3
"""
discovery.py -- Blackwall's x402 service descriptor (spec step 5: listing).

A machine-readable service card so x402 service-discovery and agents can find
and understand Blackwall programmatically. Served by the verdict server at
GET /.well-known/x402 and GET /v1/discovery, and reproduced for human
submission to awesome-x402 / the ecosystem directory (see DISCOVERY.md).

`build_descriptor` is pure (config in -> dict out) and unit-tested. Stdlib only;
standalone (no imports from the server modules) to avoid import cycles.
"""
from __future__ import annotations

from decimal import Decimal

# Compact input schema for the forecast resource (mirrors mcp_server's tool).
_FORECAST_INPUT = {
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

DESCRIPTION = ("Pre-signature x402 payment guardrail: returns a GO / HOLD / STOP "
               "verdict before an agent signs a payment, from behavioral "
               "counterparty reputation and price-anomaly signals.")


def human_price(price_atomic, decimals=6):
    """Atomic units -> human decimal string (for the descriptor)."""
    if price_atomic is None:
        return None
    return str(Decimal(int(price_atomic)) / (Decimal(10) ** int(decimals)))


def build_descriptor(pay_to=None, price=None, asset="USDC", network="base",
                     mcp=True):
    """
    The x402 service card. `price`/`pay_to` are present only when billing is on
    (otherwise the resource is advertised as unpriced).
    """
    accepts = None
    if pay_to and price is not None:
        accepts = [{
            "scheme": "exact",
            "network": network,
            "maxAmountRequired": price,
            "asset": asset,
            "payTo": pay_to,
        }]
    resource = {
        "method": "POST",
        "path": "/v1/forecast-payment",
        "description": "Forecast a payment: GO / HOLD / STOP + signed receipt.",
        "input": _FORECAST_INPUT,
        "outputVerdicts": ["GO", "HOLD", "STOP"],
        "accepts": accepts,
    }
    descriptor = {
        "name": "Blackwall",
        "description": DESCRIPTION,
        "x402Version": 1,
        "category": "payment-risk",
        "tags": ["x402", "payments", "risk", "reputation", "agent-guardrail",
                 "base", "usdc"],
        "resources": [resource],
        "mcp": ({"transport": "stdio", "tool": "forecast_payment"}
                if mcp else None),
        "custody": False,  # verdict, not custody -- the clean regulatory posture
    }
    return descriptor
