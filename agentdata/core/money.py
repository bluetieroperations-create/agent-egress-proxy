"""
money.py -- integer atomic-unit arithmetic for x402 pricing (NO FLOATS).

Seller-side counterpart to the buyer-side math in x402_budget.py. Same rule:
money is compared and stored as integers in atomic units (e.g. USDC = 6
decimals); decimal price strings are converted exactly once. A float never
touches a price.
"""
from __future__ import annotations


def to_atomic(s, decimals):
    """`"0.20"`, decimals=6 -> 200000. Rejects (ValueError) non-strings,
    garbage, and MORE precision than `decimals` (no silent truncation)."""
    if not isinstance(s, str):
        raise ValueError("amount must be a string, got %r" % type(s).__name__)
    t = s.strip()
    if not t:
        raise ValueError("empty amount")
    neg = False
    if t[0] in "+-":
        neg = t[0] == "-"
        t = t[1:]
    intp, _, frac = t.partition(".")
    if intp == "":
        intp = "0"
    if not intp.isdigit() or (frac != "" and not frac.isdigit()):
        raise ValueError("bad amount: %r" % s)
    if len(frac) > decimals:
        raise ValueError("more precision than %d decimals: %r" % (decimals, s))
    frac = frac.ljust(decimals, "0")
    val = int(intp) * (10 ** decimals) + (int(frac) if frac else 0)
    return -val if neg else val


def from_atomic(atomic, decimals):
    """200000, decimals=6 -> '0.2'. Inverse of to_atomic in canonical form."""
    if not isinstance(atomic, int):
        raise ValueError("atomic must be int")
    neg = atomic < 0
    a = -atomic if neg else atomic
    if decimals == 0:
        body = str(a)
    else:
        whole, frac = divmod(a, 10 ** decimals)
        frac_str = str(frac).rjust(decimals, "0").rstrip("0")
        body = str(whole) if not frac_str else "%d.%s" % (whole, frac_str)
    return ("-" + body) if neg else body


def parse_atomic_int(s):
    """An amount already in atomic units (offers/payments carry these) -> int
    or None. Rejects bools, negatives, decimals, garbage."""
    if isinstance(s, bool):
        return None
    if isinstance(s, int):
        return s if s >= 0 else None
    if not isinstance(s, str):
        return None
    t = s.strip()
    return int(t) if t.isdigit() else None
