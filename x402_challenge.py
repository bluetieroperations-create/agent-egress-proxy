"""x402_challenge.py -- the ONE place that reads an x402 402 challenge.

An x402 server can advertise its payment requirements through EITHER of two
carriers:

  1. the JSON body      -- {"x402Version":2,"accepts":[{...}]}   (v1 style)
  2. a response HEADER  -- WWW-Authenticate: X402 requirements="<base64 json>"

Every consumer in this repo used to read only the body. The directory-liveness
survey measured the header carrier directly: of 195 distinct hosts probed,
`hdr_accepts` was 2 -- small TODAY, and worth being precise about, because the
survey's own parser already read them while nothing else did. (The 86
`opaque_402` hosts in that same run are a DIFFERENT problem: they carry no
readable requirements in either carrier, and this module does not help them.)

The reason to close it anyway is structural rather than numeric. Those two
endpoints are not broken and the verdict engine scores them fine once the
requirements are in hand -- they were simply invisible, which made them both
uncrawlable (no price, no payee harvested) and unpayable (the funded client
could not find an `accepts` entry to sign against). The header form is the x402
v2 style, so the count is a floor that grows as servers adopt it, and being
blind to a carrier is the kind of gap that is cheap now and expensive later.

This module is that missing half, factored out of `directory_liveness.py` so the
survey, the crawler and the paying client all read a challenge the same way. It
is PURE + stdlib: no network, no state. The transport-facing helper
`accepts_from_http_error` takes an already-raised `urllib` error rather than
making a request, so callers keep control of their own fetch policy.

TOLERANT BY DESIGN. These parse arbitrary third-party responses, so nothing here
raises: a malformed header must never mask a good body, and junk of any shape
yields "no challenge" rather than an exception.

BODY WINS when both carriers are present. They should agree; when they don't, the
body is the one every x402 client already reads, so preferring it keeps us paying
what the majority of the ecosystem would pay.
"""

from __future__ import annotations

import base64
import json
import re

#: Which carrier a challenge came from. Callers record this so a survey can tell
#: "readable today" from "readable only because we now parse the header".
BODY_ACCEPTS = "body_accepts"
HDR_ACCEPTS = "hdr_accepts"
PAYMENT_REQUIRED = "payment_required_hdr"

#: The scheme token that opens an x402 WWW-Authenticate challenge. Matched as a
#: whole TOKEN, not a prefix: "X402Bearer" and "x402-evil" are different schemes
#: and must not be read as x402 payment requirements.
SCHEME = "x402"

_SCHEME_RE = re.compile(r'^\s*x402(?:\s|$)', re.IGNORECASE)
_REQUIREMENTS_RE = re.compile(r'requirements="([^"]+)"')

#: The THIRD carrier: a bare base64 x402 document in its own header, with no
#: `requirements=""` wrapper and no auth scheme.
#:
#: Measured 2026-08-28. The liveness survey filed 86 hosts as `opaque_402` -- a
#: 402 whose requirements were unreadable in either known carrier. **80 of those
#: 86 carry a complete x402 v2 challenge here**, with `{}` as the body, which is
#: precisely why they looked opaque. They are not broken or abandoned:
#: api.ipintel.ai is one of them, with 78 distinct payers and 145 settlements.
#: The remaining 6 have simply moved or gone (400/404/405/410) since the survey.
#:
#: Matched by EXACT header name. The survey probe found this by decoding every
#: header and seeing what looked like x402, which is the right move for discovery
#: and much too permissive to ship -- an unrelated header must never be mistaken
#: for payment requirements.
PAYMENT_REQUIRED_HEADER = "payment-required"

#: Hard ceiling on a 402 body we will parse. A challenge is a small JSON
#: document -- real ones are a few KB -- so this is ~100x generous while still
#: refusing to buffer a hostile seller's multi-GB "402". `http_util.read_capped`
#: guards the normal fetch path for the same reason; an error body reached
#: through a raised HTTPError bypasses it, so the cap has to live here too.
MAX_CHALLENGE_BYTES = 1024 * 1024


def accepts_of(doc):
    """Return a non-empty accepts[] from an x402 challenge document, else None.

    An `accepts` present but EMPTY is not a challenge: the server named the
    field and offered no way to pay, so there is nothing to sign or price.
    """
    if isinstance(doc, dict):
        accepts = doc.get("accepts")
        if isinstance(accepts, list) and accepts:
            return accepts
    return None


def _too_big(value):
    """A header value larger than a plausible challenge. Defence in depth.

    urllib caps a single header LINE at 65536 bytes, so an oversize value cannot
    arrive over HTTP through our own consumers -- but `parse_challenge` is public
    and takes headers from anywhere, and the BODY path is already capped. Leaving
    headers uncapped was an inconsistency, not a live hole: measured, a 19.2 MB
    header value decoded to 200,000 accepts entries at 85 MB peak.
    """
    try:
        return len(value) > MAX_CHALLENGE_BYTES
    except TypeError:
        return False


def decode_payment_required(value):
    """Decode a bare-base64 `payment-required` header value -> accepts[], or None."""
    if _too_big(value):
        return None
    try:
        raw = base64.b64decode(str(value or "").strip().strip('"') + "==")
        return accepts_of(json.loads(raw.decode("utf-8", "replace")))
    except Exception:
        return None


def decode_requirements(value):
    """Decode a `WWW-Authenticate: X402 requirements="<b64>"` value -> accepts[].

    Returns None unless the value opens with the x402 scheme AND carries a
    base64 blob that decodes to a document with a non-empty accepts[].
    """
    value = str(value or "")
    if _too_big(value) or not _SCHEME_RE.match(value):
        return None
    match = _REQUIREMENTS_RE.search(value)
    if not match:
        return None
    blob = match.group(1)
    try:
        # Tolerate missing base64 padding -- servers strip it. Two extra '='
        # is always enough and never harmful: b64decode ignores the surplus.
        raw = base64.b64decode(blob + "==").decode("utf-8", "replace")
        return accepts_of(json.loads(raw))
    except Exception:
        return None


def parse_challenge(body, headers):
    """Extract accepts[] from a 402 response, from EITHER carrier.

    Returns (accepts, carrier) where carrier is BODY_ACCEPTS / HDR_ACCEPTS,
    or (None, None) when the response carries no readable requirements.
    """
    # No bytes->str decode: json.loads takes str, bytes AND bytearray, and a
    # body that is not valid UTF-8 raises inside the same try. An explicit
    # decode branch here was dead code -- a mutation test proved removing it
    # changed nothing.
    try:
        accepts = accepts_of(json.loads(body or ""))
    except Exception:
        accepts = None
    if accepts:
        return accepts, BODY_ACCEPTS

    # Materialize ONCE. Two passes over _header_items(headers) would call .items()
    # twice, and a lazy/generator-backed mapping yields its headers only on the
    # first call -- the second pass would see nothing and the challenge would be
    # silently lost. urllib's HTTPMessage returns a list so our own consumers were
    # unaffected, but this function is public and tolerant by contract.
    items = _header_items(headers)

    # Header carriers, in a DETERMINISTIC order. `www-authenticate` is checked
    # first because it shipped first: adding a carrier must not change what an
    # already-working endpoint resolves to.
    for key, value in items:
        if str(key).lower() != "www-authenticate":
            continue
        accepts = decode_requirements(value)
        if accepts:
            return accepts, HDR_ACCEPTS
    for key, value in items:
        if str(key).lower() != PAYMENT_REQUIRED_HEADER:
            continue
        accepts = decode_payment_required(value)
        if accepts:
            return accepts, PAYMENT_REQUIRED
    return None, None


def _header_items(headers):
    """(name, value) pairs from a dict, an email.Message, or anything else.

    `urllib` hands back an `http.client.HTTPMessage`, which supports .items()
    and repeats a name once per occurrence -- so a response carrying several
    WWW-Authenticate headers gets every one considered, not just the last.
    """
    if headers is None:
        return []
    items = getattr(headers, "items", None)
    if callable(items):
        try:
            return list(items())
        except Exception:
            return []
    return []


def accepts_from_http_error(err):
    """(accepts, carrier) from a urllib HTTPError that is a 402 challenge.

    A 402 arrives as a raised HTTPError, where the body and the headers both
    still hang off the exception. Anything that is not a 402 -- or a 402 with no
    readable requirements -- yields (None, None); the caller decides whether that
    is fatal.

    Reads the body ONCE. HTTPError wraps a stream, so a second .read() returns
    b"" and would silently downgrade a body-carried challenge to "unreadable".

    The read is CAPPED at MAX_CHALLENGE_BYTES. The crawler calls this on every
    source it fetches, including hostile ones, and an oversize body is dropped
    rather than truncated -- a partial document must never be parsed as if it
    were what the server sent. The header is still consulted, so a seller cannot
    hide its requirements behind a bloated body.
    """
    if getattr(err, "code", None) != 402:
        return None, None
    try:
        body = err.read(MAX_CHALLENGE_BYTES + 1)
    except TypeError:
        # A file-like that does not accept a size argument. Fall back to an
        # uncapped read only for such objects, then enforce the cap below.
        try:
            body = err.read()
        except Exception:
            body = b""
    except Exception:
        body = b""
    if body is not None and len(body) > MAX_CHALLENGE_BYTES:
        body = b""
    return parse_challenge(body, getattr(err, "headers", None))
