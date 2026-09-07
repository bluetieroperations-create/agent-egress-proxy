#!/usr/bin/env python3
"""
user_agent.py -- the single source of truth for outbound User-Agent strings.

WHY THIS EXISTS
---------------
UA policy had scattered across 19 modules in five incompatible shapes -- four
variants of `Mozilla/5.0 (...)`, seven of `Blackwall-x/1`, five lowercase
`blackwall-x/1.0`. Two of those modules had ALREADY been forced browser-shaped
by the same Cloudflare problem, independently, without either learning from the
other (`x402.py` for facilitators, `settlement_watch.py` for explorers). A
policy re-derived per module is a policy nobody owns, so the next module gets it
wrong again. This module owns it; `test_user_agent.py` fails the build if a new
hardcoded UA appears anywhere else.

TWO SHAPES, AND THE CHOICE BETWEEN THEM IS DELIBERATE
-----------------------------------------------------
`browser(component)` -- "Mozilla/5.0 (compatible; Blackwall-<component>/0.1)"

    The default for third-party HTTP. Cloudflare-fronted hosts challenge
    non-browser agents with a 403, and `http_util` classifies 403 as a PERMANENT
    4xx that is deliberately never retried -- so a bare token UA turns such a
    host into a hard failure rather than a degraded one. Measured on one day
    against two instances of the SAME explorer software: base.blockscout.com
    served the bare `Blackwall/0.1` a 200 (only raw urllib drew Cloudflare error
    1010), while robinhoodchain.blockscout.com answered the bare UA with a 403
    challenge and this shape with a 200. Strictness is per-deployment and cannot
    be inferred from the software, so browser-shaped is the safe default.

    It stays self-identifying: "Blackwall" and the component are inside the
    parentheses. We declare ourselves; we do not impersonate a specific browser.

`bot(component, contact)` -- "blackwall-<component>/0.1 (+<contact>)"

    For surveys that MUST be honestly identifiable as automated. The directory
    liveness sweep is the evidence that x402 endpoints do not bot-block -- 0 of
    195 hosts refused it -- and that evidence is only worth something because the
    sweep announced itself as a bot while collecting it. Making it browser-shaped
    would destroy the measurement that justifies the relaxed posture everywhere
    else. Do NOT use this shape for a Cloudflare-fronted host: it will be
    challenged, and that is precisely what it is for.

Choosing `bot` is therefore a statement that being blocked is an acceptable, even
useful, outcome. Everything else takes `browser`.
"""
from __future__ import annotations

PRODUCT = "Blackwall"
VERSION = "0.1"

#: Rejected in a component or contact: anything that could split or forge a
#: header, or break the UA grammar. A UA is interpolated straight into an HTTP
#: request header, so this is an injection guard, not a style check.
_FORBIDDEN = set("\r\n\x00()<>@,;:\\\"/[]?={}")


def _clean(part, what):
    """A UA token that cannot corrupt the header it lands in."""
    if not isinstance(part, str) or not part.strip():
        raise ValueError("%s must be a non-empty string" % what)
    bad = _FORBIDDEN & set(part)
    if bad or any(ord(c) < 0x20 or ord(c) == 0x7F for c in part):
        raise ValueError("%s contains characters not allowed in a User-Agent: %r"
                         % (what, part))
    return part.strip()


def browser(component=None, version=VERSION):
    """Browser-prefixed, self-identifying UA -- the default for third-party HTTP.

    `browser()` -> "Mozilla/5.0 (compatible; Blackwall/0.1)"
    `browser("dex")` -> "Mozilla/5.0 (compatible; Blackwall-dex/0.1)"
    """
    name = PRODUCT if component is None else "%s-%s" % (PRODUCT, _clean(component, "component"))
    return "Mozilla/5.0 (compatible; %s/%s)" % (name, _clean(version, "version"))


def bot(component, contact=None, version=VERSION):
    """Plainly-automated UA, for surveys that must be identifiable as bots.

    `bot("liveness", "x402 directory check")`
        -> "blackwall-liveness/0.1 (+x402 directory check)"

    Read the module docstring before choosing this over `browser`.
    """
    ua = "%s-%s/%s" % (PRODUCT.lower(), _clean(component, "component"),
                       _clean(version, "version"))
    if contact is not None:
        ua += " (+%s)" % _clean(contact, "contact")
    return ua
