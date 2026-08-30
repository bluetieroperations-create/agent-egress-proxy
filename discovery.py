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

# CAIP-2 mapping for the descriptor's advertised network (x402 v2). Kept local
# so discovery.py stays standalone (no import from the server modules).
_CAIP2 = {
    "base": "eip155:8453",
    "base-mainnet": "eip155:8453",
    "base-sepolia": "eip155:84532",
    "ethereum": "eip155:1",
    "mainnet": "eip155:1",
}


def to_caip2(network):
    """Human/legacy network name -> CAIP-2 id (v2). CAIP-2 or unknown names pass
    through unchanged (fail-visible rather than mislabeling)."""
    if not network or not isinstance(network, str):
        return network
    n = network.strip()
    if ":" in n:
        return n
    return _CAIP2.get(n.lower(), n)

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

# WHY THIS TEXT IS SHAPED THIS WAY. Two products share the Blackwall name -- this
# x402 payment engine and the generalized pre-action gate at blackwalltier.com
# (see docs/REGISTRIES.md, "The identity split"). That doc's discriminators are
# AUTH and SCOPE, which is right for someone choosing which to install.
#
# It is not enough for someone DESCRIBING us. Measured 2026-08-30: a third-party
# AI summary of "Black_Wall x402" credited this engine with a "remote safety LLM
# forecasting loop" -- the generalized product's architecture, and the exact
# INVERSE of this one's, which is deterministic, stdlib-only and has no model in
# the verdict path at all. It also rated token safety as "basic context checking"
# while `holder_concentration.py` and `dex_price.py` implement the very checks it
# awarded to a competitor.
#
# The blend was possible because nothing we publish states the METHOD. So METHOD
# is the third discriminator, and it leads. A summarizer that reads this cannot
# reach for "LLM" without contradicting the first clause.
DESCRIPTION = ("Deterministic pre-signature x402 payment guardrail: returns a "
               "GO / HOLD / STOP verdict before an agent signs a payment. No model "
               "in the verdict path -- every gate is an auditable, mutation-tested "
               "check over counterparty reputation, price anomaly, Sybil structure, "
               "sanctions, and simulation of the payment itself.")


def human_price(price_atomic, decimals=6):
    """Atomic units -> human decimal string (for the descriptor)."""
    if price_atomic is None:
        return None
    return str(Decimal(int(price_atomic)) / (Decimal(10) ** int(decimals)))


def build_descriptor(pay_to=None, price=None, asset="USDC", network="base",
                     mcp=True, sanctions_screening=False, endpoint_readiness=False,
                     settlement_simulation=False, honeypot_check=False,
                     rwa_readiness=False, market_peg=False,
                     holder_concentration=False):
    """
    The x402 service card. `price`/`pay_to` are present only when billing is on
    (otherwise the resource is advertised as unpriced).
    """
    accepts = None
    if pay_to and price is not None:
        # v2 PaymentRequirements, mirroring the authoritative 402: CAIP-2 network,
        # `amount` in ATOMIC units (spec 5.1.2, e.g. "1000"), and the asset CONTRACT
        # address. Callers pass already-atomic price + contract so the descriptor's
        # accepts is not off by 10^decimals vs the 402 challenge.
        accepts = [{
            "scheme": "exact",
            "network": to_caip2(network),
            "amount": price,
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
    # What the verdict covers -- a SUPERSET of the free facilitator baseline:
    # sanctions screening (what KYT does) PLUS the signals it doesn't.
    #
    # ALWAYS-ON: these run in every verdict with no configuration, so advertising
    # them is a statement of fact about any deployment. Previously only the first
    # two were listed, which understated the engine by an order of magnitude and
    # left a comparison-shopper with nothing to compare -- the concrete way the
    # description gap above became a public misdescription.
    signals = ["counterparty-reputation", "price-anomaly", "sybil-structure",
               "payload-simulation", "permit2-allowance", "calldata-drainer",
               "secret-exfiltration", "payee-syntax", "evidence-confidence"]
    if sanctions_screening:
        signals.insert(0, "sanctions-ofac")
    # CONFIGURED: opt-in, so advertised only when actually wired. Claiming a gate
    # this deployment does not run would be the same defect in the other
    # direction.
    if endpoint_readiness:
        signals.append("endpoint-readiness")
    for flag, label in ((settlement_simulation, "settlement-simulation"),
                        (honeypot_check, "honeypot-exit-check"),
                        (rwa_readiness, "transfer-restriction-readiness"),
                        (market_peg, "dex-market-peg"),
                        (holder_concentration, "holder-concentration")):
        if flag:
            signals.append(label)
    descriptor = {
        "name": "Blackwall",
        "description": DESCRIPTION,
        "x402Version": 2,  # v2 (see build_openapi below for the openapi.json discovery doc)
        "category": "payment-risk",
        # MACHINE-READABLE discriminator. A crawler building a comparison table
        # reads fields before prose; leaving method implicit is what let one be
        # invented for us.
        "method": "deterministic",
        "modelInVerdictPath": False,
        "tags": ["x402", "payments", "risk", "reputation", "agent-guardrail",
                 "base", "usdc", "deterministic", "pre-signature", "keyless"],
        "signals": signals,
        "screening": (["sanctions-ofac"] if sanctions_screening else []),
        "resources": [resource],
        "mcp": ({"transport": "stdio", "tool": "forecast_payment"}
                if mcp else None),
        "custody": False,  # verdict, not custody -- the clean regulatory posture
    }
    return descriptor


# ===========================================================================
# OpenAPI discovery document (x402scan probes GET /openapi.json at the origin)
# ===========================================================================
# Contract (Merit-Systems/x402scan docs/DISCOVERY.md + the published
# @agentcash/discovery parser, verified against v1.7.5):
#   * required top-level: openapi, info.title, info.version, paths
#   * each PAID operation: an `x-payment-info` with protocols as an array of
#     OBJECTS [{"x402": {}}] (NOT strings -- the parser's PaymentInfoSchema types
#     protocols as array(record); a string array fails the structured parse and
#     drops the price hint) + a `price` block ({mode:"fixed",...} or
#     {mode:"dynamic", min, max}), a 402 response, and OpenAPI `security`
#     referencing an x402 securityScheme.
#   * FREE endpoints set `security: []` so probers skip them (no false paid).
#   * ownership: info.contact.email + optional x-discovery.ownershipProofs.
CONTACT_EMAIL = "bluetier.operations@gmail.com"


# --------------------------------------------------------------------------
# THE ROUTE TABLE -- one source of truth.
#
# There used to be FOUR hand-maintained copies of this list (blackwall.py's
# do_GET, do_HEAD and the do_POST rate-limit guard, plus build_openapi's `paths`
# literal) which had to agree and did not. Measured in production 2026-08-29:
#   - openapi.json advertised 4 of 13 served routes;
#   - HEAD 404'd on /jwks.json, /.well-known/blackwall-receipt-key.json and
#     /stats while GET returned 200 for all three;
#   - /v1/verify-signer was omitted from the rate-limit guard despite its own
#     docstring claiming otherwise -- and it is the most expensive route served.
# Adding a route meant remembering four places; nobody did.
#
# These tuples are the ROUTE INVENTORY, not a pricing statement: they also drive
# do_HEAD and the do_POST rate-limit guard. /v1/forecast-payment appears in
# PUBLIC_POST_ROUTES for those purposes but is the one route that can be PAID --
# build_openapi builds its operation separately so it carries the x-payment-info
# band, and skips it in the free loop. Every other route is free and
# unauthenticated (security: [] so x402scan probers exclude it).
PUBLIC_GET_ROUTES = (
    "/healthz",
    "/.well-known/x402",
    "/v1/discovery",
    "/jwks.json",
    "/.well-known/blackwall-receipt-key.json",
    "/v1/price-index",
    "/openapi.json",
    "/stats",
)

# Served, HEAD-able, and a real route -- but deliberately NOT catalogued in
# openapi.json. /stats carries only aggregate counters (requests, verdict_*,
# errors, rate_limited): no addresses, no PII. It does however publish traffic
# VOLUME and the GO/HOLD/STOP mix, and openapi.json is indexed by x402scan and
# other crawlers, so being served is not the same as being advertised -- that is
# an operator decision, not a routing one.
#
# It stays in PUBLIC_GET_ROUTES on purpose. That tuple also drives do_HEAD and
# the dispatch-parity test; removing it there would 404 HEAD /stats and
# reintroduce exactly the GET/HEAD drift this table was built to kill.
UNADVERTISED_ROUTES = frozenset({"/stats"})

PUBLIC_POST_ROUTES = (
    "/v1/forecast-payment",
    "/v1/verify-signer",
    "/v1/report-outcome",
    "/v1/screen-payer",
)

# Served ONLY when x402 billing is configured; the handler answers 404
# "billing not enabled" otherwise, so it is advertised only when priced.
BILLING_POST_ROUTES = ("/v1/session",)

_ROUTE_SUMMARY = {
    "/healthz": "Liveness probe.",
    "/.well-known/x402": "x402 service descriptor (discovery).",
    "/v1/discovery": "x402 service descriptor (alias of /.well-known/x402).",
    "/jwks.json": "Public Ed25519 receipt-signing keys, so a signed verdict "
                  "receipt can be verified offline with no secret of ours. "
                  "Retired keys stay published so receipts survive rotation.",
    "/.well-known/blackwall-receipt-key.json":
        "Alias of /jwks.json at the well-known name blackwall-mcp-remote fetches.",
    "/v1/price-index": "Per-category median price for agent services, computed "
                       "from SETTLED on-chain payments rather than advertised "
                       "prices. Read-only reference data.",
    "/openapi.json": "This document.",
    "/stats": "Request and verdict counters. No PII.",
    "/v1/verify-signer": "Stage-2 EIP-3009 signer recovery for a request that "
                         "got a fast deferred verdict. ok:false means the signer "
                         "is forged or mismatched -- do NOT submit the payment.",
    "/v1/report-outcome": "Report the realized outcome for a prior receipt "
                          "(closes the moat-flywheel loop). Requires a report_token.",
    # The BUYER side of the graph. Everything else here scores the payee; this
    # answers the mirror question a facilitator/wallet asks before it settles an
    # inbound payment. Free + unauthenticated: an unknown payer is NEUTRAL
    # cold-start, so there is nothing here worth gating behind a fee.
    "/v1/screen-payer": "Screen a PAYER wallet: tier (established / emerging / "
                        "unknown), anchors paid, and breadth. Informational -- "
                        "an unknown payer is neutral, never a block.",
    "/v1/session": "Open a reusable x402 session token (fund once, many checks).",
}

_ROUTE_RESPONSES = {
    "/v1/screen-payer": {"200": {"description": "Payer profile."},
                         "400": {"description": "Invalid payer address."},
                         "503": {"description": "No payer-reputation source "
                                                "configured."}},
    "/v1/report-outcome": {"202": {"description": "Outcome recorded."},
                           "403": {"description": "Invalid or missing report_token."}},
    "/healthz": {"200": {"description": "Service healthy."}},
    "/v1/verify-signer": {"200": {"description": "Signer verification result."},
                          "400": {"description": "Invalid request body."}},
    "/v1/session": {"200": {"description": "Session token."},
                    "402": {"description": "Payment required to open a session."},
                    "404": {"description": "Billing not enabled on this deployment."}},
}


def route_path(raw):
    """Raw request path -> the path used for GET/HEAD ROUTE MATCHING.

    `self.path` carries the query string and matching is exact, so a probe with
    a cache-buster (`/openapi.json?v=2`, `/healthz?cb=1`) matched nothing and
    answered 404 -- a monitor or discovery crawler scores our discovery
    documents DEAD. Measured live 2026-08-29: every GET route 404'd with any
    query string attached.

    SCOPE, deliberately narrow:
      - GET/HEAD only. POST routes stay STRICTLY matched: they carry payments,
        `self.path` feeds the x402 billing RESOURCE key, and a query-string
        request 404ing before it reaches a handler is fail-closed and correct.
      - The query string is dropped, not parsed. No route here takes a
        parameter, so it cannot be part of this server's route identity.
        NOTE this is NOT a general x402 rule: 27 of 3,827 URLs in our crawled
        directory carry a query string, and for that seller `?method=GET` and
        `?method=POST` are DIFFERENT priced resources. Stripping is safe for
        OUR routing, and would be wrong applied to a resource key.
      - A trailing slash is left alone. `/healthz/` is a separate question with
        its own blast radius; widening routing was not the job.

    Pure, total, never raises: a non-string yields "" (which matches no route).
    """
    if not isinstance(raw, str):
        return ""
    return raw.split("?", 1)[0]


def _operation_id(path):
    """/v1/price-index -> priceIndex. Stable, unique, and derived so a new route
    cannot ship without one."""
    parts = [p for p in path.replace(".", "-").replace("_", "-").split("/") if p]
    if parts and parts[0] == "v1":
        parts = parts[1:]
    words = [w for part in parts for w in part.split("-") if w]
    if not words:
        return "root"
    return words[0] + "".join(w.capitalize() for w in words[1:])


def _free_op(path):
    """OpenAPI operation for a free, unauthenticated route."""
    return {
        "operationId": _operation_id(path),
        "summary": _ROUTE_SUMMARY.get(path, path),
        "security": [],
        "responses": _ROUTE_RESPONSES.get(
            path, {"200": {"description": "OK."}}),
    }


def build_openapi(server_url=None, min_fee="0.001", max_fee="0.10",
                  currency="USD", ownership_proofs=None, priced=True):
    """Return the x402scan-shaped OpenAPI discovery document (pure: config->dict).

    `priced` reflects whether billing is on: when off, the forecast endpoint is
    advertised free (`security: []`, no x-payment-info) so we never MISREPRESENT
    a free oracle as paid. `server_url` is the public origin (e.g.
    https://agent-egress-proxy.onrender.com); omitted -> relative paths, which
    x402scan resolves against the probed origin.

    Only PUBLIC, non-sensitive metadata goes in here -- no keys, no internal
    signals, no reputation data. It describes the SHAPE of the API, nothing more.
    """
    forecast_op = {
        "operationId": "forecastPayment",
        "summary": "Forecast a payment: GO / HOLD / STOP verdict before signing.",
        "description": DESCRIPTION,
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {
                "type": "object",
                "required": ["counterparty", "amount", "asset", "chain"],
                "properties": {
                    "counterparty": {"type": "string",
                                     "description": "Recipient address to screen."},
                    "amount": {"type": "string",
                               "description": "Payment amount at risk (human decimal)."},
                    "asset": {"type": "string", "description": "Asset symbol/address."},
                    "chain": {"type": "string", "description": "Network (e.g. base)."},
                    "payer": {"type": "string"},
                    "resource": {"type": "string"},
                },
            }}},
        },
        "responses": {
            "200": {"description": "Verdict (GO / HOLD / STOP) + signed receipt.",
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "verdict": {"type": "string",
                                        "enum": ["GO", "HOLD", "STOP"]},
                            "receipt_id": {"type": "string"},
                        },
                    }}}},
            "402": {"description": "Payment required (x402). Retry with an "
                                   "X-PAYMENT / PAYMENT-SIGNATURE header."},
            "400": {"description": "Invalid request body."},
        },
    }
    if priced:
        # Value-aligned pricing => DYNAMIC price band. This is the FEE band the
        # caller can be charged: min = the fee floor (PricingPolicy.min_fee),
        # max = the fee cap (PricingPolicy.max_fee). It is NOT the amount-at-risk
        # `free_below` threshold -- that is a different quantity (the value being
        # protected, not a fee) and using it as `min` produces an inverted
        # min>max band that a strict x402scan/OpenAPI validator rejects.
        #
        # `protocols` MUST be an array of OBJECTS (`[{"x402": {}}]`), NOT strings.
        # The x402scan discovery parser (@agentcash/discovery PaymentInfoSchema)
        # types protocols as `array(record(string, unknown))`; a `["x402"]`
        # string array fails the STRUCTURED parse, and because our price is a
        # nested object (not the legacy top-level minPrice/maxPrice) the legacy
        # fallback then drops BOTH the price hint and the protocol -- the
        # endpoint registers with no pricing. Emitting objects makes the
        # structured parse succeed. (Verified against the published parser.)
        forecast_op["x-payment-info"] = {
            "protocols": [{"x402": {}}],
            "price": {"mode": "dynamic", "currency": currency,
                      "min": str(min_fee), "max": str(max_fee)},
        }
        forecast_op["security"] = [{"x402": []}]
    else:
        forecast_op["security"] = []  # free oracle -- do not advertise as paid

    paths = {"/v1/forecast-payment": {"post": forecast_op}}
    # Everything else is FREE: security [] so probers exclude it. Built from the
    # shared route table above rather than a hand-written literal -- see THE
    # ROUTE TABLE for the four-way drift that motivated it.
    for path in PUBLIC_GET_ROUTES:
        if path in UNADVERTISED_ROUTES:
            continue   # served, but deliberately not catalogued
        paths.setdefault(path, {})["get"] = _free_op(path)
    for path in PUBLIC_POST_ROUTES:
        if path == "/v1/forecast-payment":
            continue  # already added above, priced
        paths.setdefault(path, {})["post"] = _free_op(path)
    if priced:
        # Advertised ONLY when billing is on: with billing off the handler
        # answers 404 `billing not enabled`, and documenting a dead route is the
        # same defect as omitting a live one.
        for path in BILLING_POST_ROUTES:
            paths.setdefault(path, {})["post"] = _free_op(path)

    doc = {
        "openapi": "3.0.3",
        "info": {
            "title": "Blackwall x402 Payment-Risk Oracle",
            "description": DESCRIPTION,
            "version": "2.0.0",
            "contact": {"email": CONTACT_EMAIL},
        },
        "paths": paths,
        "components": {
            "securitySchemes": {
                # x402 payment as an API-key-style scheme carried in the
                # X-PAYMENT header (v2 canonical: PAYMENT-SIGNATURE). apiKey is
                # the closest OpenAPI 3.0 primitive for a per-request payment.
                "x402": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-PAYMENT",
                    "description": "Base64 x402 v2 PaymentPayload. On an unpaid "
                                   "request the endpoint returns 402 with the "
                                   "payment requirements.",
                },
            },
        },
    }
    if server_url:
        doc["servers"] = [{"url": server_url.rstrip("/")}]
    if ownership_proofs:
        doc["x-discovery"] = {"ownershipProofs": list(ownership_proofs)}
    return doc
