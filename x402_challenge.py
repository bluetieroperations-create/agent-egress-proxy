#!/usr/bin/env python3
"""
x402_challenge.py -- parse the WWW-Authenticate payment challenge served in the wild.

WHY THIS EXISTS
---------------
`directory_liveness.py` recorded that nothing in this repo reads `WWW-Authenticate`
-- every consumer takes `accepts[]` from the JSON body -- so a header-style endpoint
is uncrawlable and unpayable even though the verdict engine would score it fine.
That module also guessed the format:

    WWW-Authenticate: X402 requirements="<base64 json>"

**That guess is wrong.** Measured live on 2026-08-27 against all 195 surveyed
hosts, 11 serve a challenge and every one uses:

    WWW-Authenticate: Payment id="...", realm="...", method="evm", intent="charge",
                      request="<base64url JSON>", description="...", expires="..."

The scheme token is `Payment`, not `X402`; the payload parameter is `request`, not
`requirements`; and the base64 is **base64url** (`-`/`_`, unpadded), so a standard
b64decode raises. `_REQUIREMENTS_RE` in directory_liveness would match none of them.

MEASURED SHAPE (11 live challenges)
-----------------------------------
methods:      tempo (5), evm (4), asterpay (1), usdc (1)
payload keys: amount 11/11, currency 11/11, recipient 11/11, methodDetails 10/11,
              description 4, asset 1, settlement 1, extra 1, externalId 1

`currency` is USUALLY a token CONTRACT ADDRESS (Base USDC
0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913) but one host sends the SYMBOL "USDC"
there and puts the address in `asset` -- so both spellings must be handled or that
host's asset resolves to the string "USDC" and any address comparison silently
fails. Chain ids seen: 8453 (Base) and 4217 (Tempo).

Amounts are ATOMIC strings. `decimals` appears only inside `methodDetails` and only
sometimes; when absent this returns None rather than assuming 6, because guessing
decimals mis-scales the amount by 10^n -- the same class of error as the PFAS
units bug (a 1000x miss from assuming a unit).
"""

import base64
import json
import re

SCHEME_RE = re.compile(r'^\s*Payment\b', re.I)
PARAM_RE = re.compile(r'(\w+)="([^"]*)"')


def parse_params(header):
    """{name: value} for the quoted parameters of a Payment challenge.

    Returns {} for a missing header or a different auth scheme (Bearer, Basic),
    so a caller cannot mistake an ordinary 401 for a payment challenge.
    """
    if not header or not SCHEME_RE.match(header):
        return {}
    return dict(PARAM_RE.findall(header))


def decode_request(blob):
    """Decode the base64url `request` payload. None if it is not decodable JSON.

    Handles base64URL (- and _) and missing padding; the live headers use both.
    """
    if not blob:
        return None
    s = blob.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        return json.loads(base64.b64decode(s))
    except Exception:
        return None


def to_accepts(header):
    """Normalize a Payment challenge into ONE `accepts[]`-shaped dict.

    The engine scores from `accepts[]`, so emitting that shape means a header-style
    endpoint flows through the existing path with no other change.

    Returns None when the header is absent, not a Payment scheme, or carries no
    decodable request payload.
    """
    params = parse_params(header)
    if not params:
        return None
    req = decode_request(params.get("request"))
    if not isinstance(req, dict):
        return None

    details = req.get("methodDetails") or {}
    chain_id = details.get("chainId")
    currency = req.get("currency")
    asset = req.get("asset")
    # One host puts the SYMBOL in `currency` and the ADDRESS in `asset`. Prefer
    # whichever looks like an address; never let a symbol land in the address slot.
    if isinstance(currency, str) and currency.startswith("0x"):
        address, symbol = currency, (asset if isinstance(asset, str) and not asset.startswith("0x") else None)
    elif isinstance(asset, str) and asset.startswith("0x"):
        address, symbol = asset, (currency if isinstance(currency, str) else None)
    else:
        address, symbol = None, currency or asset

    return {
        "payTo": req.get("recipient"),
        "maxAmountRequired": req.get("amount"),
        "asset": address,
        "assetSymbol": symbol,
        "decimals": details.get("decimals"),
        "network": ("eip155:%s" % chain_id) if chain_id is not None else None,
        "chainId": chain_id,
        "method": params.get("method"),
        "realm": params.get("realm"),
        "challengeId": params.get("id"),
        "expires": params.get("expires"),
        "description": req.get("description") or params.get("description"),
        "source": "www-authenticate",
    }


def accepts_from_response(body_doc, header):
    """Every `accepts[]` entry for a 402, from the BODY first then the HEADER.

    Body entries come first because that is the format every existing consumer
    already understands; the header entry is appended so a v2-only endpoint stops
    being invisible. Deliberately additive -- it can only ever ADD a payment
    option, never remove or reorder the ones already parsed.
    """
    out = []
    if isinstance(body_doc, dict):
        for a in body_doc.get("accepts") or []:
            if isinstance(a, dict):
                out.append(a)
    hdr = to_accepts(header)
    if hdr:
        out.append(hdr)
    return out
