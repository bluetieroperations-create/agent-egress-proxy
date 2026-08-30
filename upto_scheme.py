#!/usr/bin/env python3
"""
upto_scheme.py -- the x402 `upto` (metered) scheme and the Permit2 allowance it
requires.

WHY THIS EXISTS. `exact` moves a fixed amount via EIP-3009
`transferWithAuthorization`, and the signed authorization IS the whole exposure:
one nonce, one value. `upto` is different. It quotes a CEILING
(`maxAmountRequired`), meters the real amount below it, and settles through the
**Permit2** contract using `transferFrom` -- which means the payer wallet must
first grant Permit2 an ERC-20 allowance.

That allowance is a SEPARATE, LONGER-LIVED exposure than the payment, and it is
invisible to every spending control in this market:

  * AWS Bedrock AgentCore Payments (GA) enforces `limits.maxSpendAmount` plus an
    expiry, and nothing else -- no payee allowlist, no counterparty check. An
    allowance is not a spend, so a $1.00 session budget coexists happily with an
    approval over the wallet's entire USDC balance.
  * Its own documentation offers granting an UNLIMITED allowance as a normal
    option, spelling out
    115792089237316195423570985008687907853269984665640564039457584007913129639935,
    and notes that `approve` SETS rather than adds.

An unlimited approval is the classic drainer pattern `calldata.py` already treats
as a hard STOP when it appears in a raw transaction. This module recognises the
same exposure when it arrives as an `upto` payment intent instead of as calldata,
so following the platform's own documentation cannot walk past the gate that
exists to catch it.

BOUNDARY, matching calldata.py:
  * UNLIMITED allowance  -> hard STOP (a mismatch), the same disposition an
    unlimited `approve` gets today.
  * EXCESSIVE (far above the quoted ceiling) -> HOLD only. Headroom is legitimate:
    a wallet approves once and meters many calls under it, so this must never STOP.
  * UNKNOWN (no allowance stated, or an unreadable ceiling) -> no gate at all.
    FAIL-OPEN: an absent field means the caller did not tell us, not that the
    exposure is zero, and guessing either way would be wrong.

Pure + stdlib. Nothing here performs I/O.
"""
from __future__ import annotations

# uint256-max is the canonical "infinite approval"; Permit2's own convention is
# uint160-max. Both are unlimited in practice, and calldata.py already treats
# anything at or above 2**128 as unlimited -- reused so the two modules cannot
# drift into disagreeing about what "unlimited" means.
import re

from calldata import UNLIMITED_MIN

# A human-unit price: digits, one decimal point, digits. Deliberately narrow --
# see parse_ceiling. `[0-9]` rather than `\d` so only ASCII digits qualify.
_HUMAN_QUOTE = re.compile(r"^[0-9]+\.[0-9]+$")

# How far above the quoted ceiling an allowance may sit before it is worth a
# HOLD. Deliberately generous: approving once and metering many calls under the
# approval is the normal way `upto` is used, so a tight ratio would flag correct
# behaviour. 100x means the wallet is exposed to two orders of magnitude more
# than the ceiling it was shown.
EXCESSIVE_RATIO = 100

SCHEME = "upto"



def _show(v):
    """Short, safe rendering for a message. Never raises, never echoes a huge blob."""
    text = "None" if v is None else str(v)
    return text if len(text) <= 40 else text[:37] + "..."


def is_upto(scheme):
    """True iff this names the metered scheme. Never raises."""
    return isinstance(scheme, str) and scheme.strip().lower() == SCHEME


def parse_allowance(value):
    """Allowance -> int, or None when we cannot read it.

    None means UNKNOWN, never 0: a zero would read as "no approval granted" and
    silently pass the exposure check. Booleans are rejected because `bool` is an
    `int` subclass and `True` would otherwise become 1.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        n = int(text, 16) if text.lower().startswith("0x") else int(text)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def allowance_from_request(body):
    """Find a stated Permit2 allowance in a request body.

    Accepts our own `permit2_allowance` and the AWS AgentCore spelling
    `permit2AllowanceLimit`, at the top level or nested under `cryptoX402` --
    which is the shape an agent integrating AgentCore actually holds
    (`paymentInput.cryptoX402.permit2AllowanceLimit`). Reading the field agents
    already have costs nothing and avoids asking them to restate it.
    """
    if not isinstance(body, dict):
        return None
    for key in ("permit2_allowance", "permit2AllowanceLimit"):
        if key in body:
            return parse_allowance(body[key])
    nested = body.get("cryptoX402")
    if isinstance(nested, dict):
        return allowance_from_request(nested)
    return None


def scheme_from_request(body):
    """The payment scheme named by a request, or None.

    Top-level `scheme` first, then the first `accepts[]` entry -- the entry a
    client pays, matching how the rest of the engine reads a 402 challenge.
    """
    if not isinstance(body, dict):
        return None
    scheme = body.get("scheme")
    if isinstance(scheme, str) and scheme.strip():
        return scheme
    accepts = body.get("accepts")
    if isinstance(accepts, list) and accepts:
        first = accepts[0]
        if isinstance(first, dict):
            s = first.get("scheme")
            if isinstance(s, str) and s.strip():
                return s
    return None


def parse_ceiling(max_amount, decimals=None):
    """The quoted ceiling as an ATOMIC int, or None when we cannot read it.

    An atomic quote is an integer, so `parse_allowance` handles the ordinary
    case. The remainder is a seller quoting in HUMAN units -- `"0.00335"` rather
    than `"3350"` -- which previously read as unreadable and switched the ratio
    check off. Measured, not hypothetical: probing the live corpus found one
    quote in 363 written that way.

    Scaling needs the asset's decimals, and the caller must pass only decimals it
    can VERIFY (`payload_sim.known_decimals`), never a value the request
    asserted. The direction of that risk is what matters: an inflated ceiling
    SUPPRESSES the excessive-allowance warning, so a caller who could choose the
    scale could switch off the check being applied to it.

    Only a value `parse_allowance` could not read reaches the scaling branch, so
    an integer is never re-interpreted as human units -- which would multiply a
    real ceiling by 10^decimals and silently suppress the same warning.
    """
    atomic = parse_allowance(max_amount)
    if atomic is not None:
        return atomic
    if decimals is None:
        return None
    # AUDIT FINDING (2026-08-29, MEDIUM -- in this function's first version).
    # "Only a value parse_allowance could not read reaches the scaling branch"
    # was true, but nothing said what such a value had to LOOK like, and
    # `Decimal` accepts far more than a human price. A seller-authored `"1e999"`
    # scaled to a ceiling with 1000 digits and the excessive-allowance warning
    # went quiet -- `status: ok`. `"1e6"` was worse for being plausible: read as
    # 1e6 TOKENS, it inflated a 1,000,000-atomic quote by a further 10^6. The
    # ceiling comes from the 402 challenge, so it is the screened party's own
    # input, and inflating it is exactly how you switch this check off.
    #
    # So the human-unit form is now stated explicitly: digits, one point, digits.
    # `[0-9]` not `\d`, because `\d` also matches other scripts' digits and
    # `int("\u0663")` is 3 -- a quote should not depend on that. Anything else
    # (exponents, hex, inf/nan, underscores, a leading `.`, a sign) is NOT a
    # price we recognise, so it stays unknown and the gate simply does not run.
    text = max_amount if isinstance(max_amount, str) else (
        repr(max_amount) if isinstance(max_amount, float) else None)
    if text is None or not _HUMAN_QUOTE.match(text.strip()):
        return None
    from x402 import to_atomic          # deferred: x402 is the base module
    return to_atomic(text.strip(), decimals)


def assess_upto(scheme, max_amount=None, allowance=None, decimals=None):
    """Assess the Permit2 exposure of an `upto` payment.

    Returns {status, mismatches[], warnings[], allowance, ceiling} where status is
    one of not_applicable / unknown / ok / excessive / unlimited.

    `mismatches` is non-empty ONLY for `unlimited` -- the caller folds mismatches
    as a hard STOP, and that is the one disposition an approval over the whole
    balance deserves. `excessive` is a warning, because headroom is normal.
    Never raises.
    """
    out = {"status": "not_applicable", "mismatches": [], "warnings": [],
           "allowance": None, "ceiling": None}
    if not is_upto(scheme):
        return out

    allow = parse_allowance(allowance)
    ceiling = parse_ceiling(max_amount, decimals)
    out["allowance"] = allow
    out["ceiling"] = ceiling

    if allow is None:
        out["status"] = "unknown"
        return out

    if allow >= UNLIMITED_MIN:
        out["status"] = "unlimited"
        out["mismatches"].append(
            "`upto` payment grants an UNLIMITED Permit2 allowance -- Permit2 may "
            "then move this asset from the wallet without a further signature, up "
            "to the entire balance. A spending cap cannot restrain it, because an "
            "allowance is not a spend. Approve the quoted ceiling instead.")
        return out

    if ceiling is None or ceiling <= 0:
        # The ratio check cannot run, and SAYS SO. A ceiling quoted in human units
        # ("0.001" rather than the atomic "1000") is not an integer, so comparing
        # it to an atomic allowance would not be a comparison at all -- skipping is
        # right. Skipping SILENTLY is not: that is a check that did not run,
        # reported as though nothing was wrong, which is the defect shape this
        # codebase keeps finding. Note only when an allowance was actually stated;
        # saying nothing is an absent input, not a skipped check.
        out["status"] = "unknown"
        out["warnings"].append(
            "`upto` Permit2 allowance %s could not be compared to the quoted "
            "ceiling %s -- the ceiling is not an atomic integer, so the "
            "proportionality check did not run (an unlimited allowance is still "
            "refused)" % (allow, _show(max_amount)))
        return out

    if allow > ceiling * EXCESSIVE_RATIO:
        out["status"] = "excessive"
        out["warnings"].append(
            "`upto` payment grants a Permit2 allowance of %s against a quoted "
            "ceiling of %s (%.0fx) -- the wallet is exposed well beyond the amount "
            "it was shown" % (allow, ceiling, allow / float(ceiling)))
        return out

    out["status"] = "ok"
    return out
