#!/usr/bin/env python3
"""
volume_integrity.py -- how much of a payment network's volume is real?

WHY THIS EXISTS
---------------
Measured on MPP (Machine Payments Protocol, Tempo mainnet): ONE address presents
as the largest seller on the network and is not a seller at all. Every payment
to it is for exactly $0.01, every one of its buyers pays it and nothing else,
and all of them are funded by a single wallet that receives nothing itself. The
sink never forwards. One operator, fanned out across ~1,300 wallets, paying its
own endpoint. It has run continuously for at least 30 days.

Its share is 18.3% of in-band machine payments (7,167 of 39,164), pooled across
18 independent windows spanning 30 days. 95% CI 12.2%-27.4% (bootstrap,
resampling whole windows). Two independent runs -- different offsets, different
window sizes, separate RPC pulls -- returned 18.7% and 17.6%.

MIND THE DENOMINATOR TOO. "In-band machine payments" means USDC.e
(0x20c0...b9537d11c60e8b50) between $0.0001 and $20 -- not all of Tempo. USDC.e
is only 67.6% of the chain's ERC-20 transfers; the rest is pathUSD, USDT0 and a
dozen smaller stablecoins. That is the right scope rather than an oversight:
all 140 tempo-method services in the MPP directory declare USDC.e as their
settlement asset and no other, and the other tokens carry treasury-scale value
(pathUSD p99 $5,913, USDT0 p99 $380,000), not per-request payments. Screening
every Tempo token pooled moves the share from 20.3% to 17.7% on the same sample
-- inside the interval, so the scoping does not carry the result. State the
token anyway; a reader who assumes "all of Tempo" is reading a 68% slice.

QUOTE THE INTERVAL, NOT THE POINT. "Roughly one in five, 95% CI 12-27%" is what
the sampling supports; "18.3%" implies a precision it does not have.

MIND THE SAMPLING -- this module's own headline was wrong once. A single 14-hour
window put the figure at 49.9%, and it was quoted that way before the follow-up
ran. Across 18 windows the per-window share runs 4.3% to 61.7%: A SINGLE WINDOW
SPANS A 14x RANGE and is worthless on its own. Pool a dozen or more across
weeks; `synthetic_share` cannot tell how its input was sampled and will
faithfully report a number that means nothing.

The same screen over the x402 corpus (46,031 settlements, 281 payees, 29 months)
returns ZERO. That contrast is the point: a detector that fires everywhere is
worthless.

WHAT THE SIGNATURE IS -- AND WHAT IT IS NOT
--------------------------------------------
Three independent signals, and a gate that matters more than any of them:

  FAN-OUT (the gate)   many distinct buyers. Without it the other signals are
      meaningless: a payee with ONE buyer trivially has 100% "exclusive" buyers
      funded by one source, and that is just a company funding its own agent
      wallet. The MPP signature is FABRICATED DIVERSITY -- wallets engineered to
      look like a crowd. One wallet is not a crowd. `MIN_BUYERS` enforces this,
      and dropping it is how this module would start slandering real businesses.

  UNIFORM PRICING      one distinct settled amount across the payee's whole
      history. Real usage varies -- different requests, different prices. This
      is the ONLY signal immune to crawl bias, because it is computed entirely
      within one payee's own records. A synthetic verdict REQUIRES it.

  BUYER EXCLUSIVITY    the payee's buyers transact with nobody else. Strong
      evidence -- but INFLATED by a targeted backfill, which crawls known payees
      and so never sees a buyer's other counterparties. Never sufficient alone.

  FUNDER CONCENTRATION the buyers are all funded from one source. The most
      damning signal and the least often available: it needs the funding graph,
      not just the settlement graph. When absent, this module says so rather
      than quietly scoring 2-of-2 as if it were 3-of-3.

VERDICTS ARE DELIBERATELY CONSERVATIVE
---------------------------------------
`synthetic` requires fan-out AND uniform pricing AND every other available
signal. `suspect` is the honest middle. Absent evidence never counts as
evidence: a payee with no funding data can still be called synthetic on the
other two, but the result records which signals were actually checked, so a
reader can see what the call rests on.

Calling real traffic fake is the expensive error here -- it is an accusation
about someone's business. The thresholds are set to under-report.

Pure functions given their inputs; no network, no chain coupling. The caller
supplies the graph and is responsible for excluding protocol addresses (fee
collectors, burn addresses) -- see `SYSTEM_ADDRESS_PREFIXES` for the ones
observed on Tempo -- and for deciding which token(s) the graph covers, which
this module cannot see and will not mention in its output.
"""
from __future__ import annotations

#: Below this many distinct buyers, fabricated diversity is not what is being
#: measured and the screen returns `unscreenable`. The MPP cluster ran ~1,300.
MIN_BUYERS = 20

#: Below this many payments there is not enough history to characterise.
MIN_PAYMENTS = 20

#: Fraction of a payee's buyers that transact with nobody else.
EXCLUSIVITY_THRESHOLD = 0.90

#: Fraction of buyer funding arriving from a single source.
FUNDER_THRESHOLD = 0.90

#: Distinct settled amounts at or below this count reads as machine-fixed.
#: One is the observed case; the constant exists so a caller can loosen it.
MAX_DISTINCT_AMOUNTS = 1

#: Amounts are rounded here before counting distinct values, so float noise in
#: a decimal-converted integer amount does not read as price variety.
AMOUNT_PRECISION = 6

#: Patterned addresses observed on Tempo that are protocol machinery, not
#: sellers: a fee collector taking 15,723 transfers worth $1.00 in total would
#: otherwise rank as a top "seller". Callers should filter these out first.
SYSTEM_ADDRESS_PREFIXES = ("0xfeec0000", "0xdec00000")
NULL_ADDRESS = "0x" + "0" * 40


def is_system_address(addr):
    """Protocol machinery rather than a participant."""
    a = (addr or "").lower()
    return a == NULL_ADDRESS or a.startswith(SYSTEM_ADDRESS_PREFIXES)


def price_uniformity(amounts, precision=AMOUNT_PRECISION):
    """Distinct settled amounts. 1 means every payment was for the same price."""
    return len({round(float(a), precision) for a in amounts if a is not None})


def buyer_exclusivity(payee, buyers, pays_to):
    """Fraction of `buyers` that transact with `payee` and nobody else.

    `pays_to`: {buyer: set(payees)}. Returns None for an empty buyer set rather
    than 0.0 -- no buyers is unknown, not 'perfectly diverse'."""
    if not buyers:
        return None
    exclusive = sum(1 for b in buyers if (pays_to.get(b) or set()) == {payee})
    return exclusive / float(len(buyers))


def funder_concentration(buyers, funders_of, exclude=()):
    """Fraction of inbound funding to `buyers` arriving from one source.

    `funders_of`: {buyer: {funder: count}}. Returns None when no funding data
    is available -- the caller must not read that as 'diverse'."""
    tally = {}
    skip = {str(x).lower() for x in exclude}
    for b in buyers or ():
        for src, n in (funders_of.get(b) or {}).items():
            if str(src).lower() in skip:
                continue
            tally[src] = tally.get(src, 0) + n
    total = sum(tally.values())
    if not total:
        return None
    return max(tally.values()) / float(total)


def screen_payee(payee, amounts, buyers, pays_to, funders_of=None, *,
                 min_buyers=MIN_BUYERS, min_payments=MIN_PAYMENTS,
                 exclusivity_threshold=EXCLUSIVITY_THRESHOLD,
                 funder_threshold=FUNDER_THRESHOLD,
                 max_distinct_amounts=MAX_DISTINCT_AMOUNTS):
    """Screen one payee. Returns a verdict dict, never raises on thin input.

    verdict is one of:
      `unscreenable` -- too few payments or too few buyers to say anything. NOT
          a clean bill of health, and the reason is recorded.
      `clean`        -- screened, no signal fired.
      `suspect`      -- some signals fired but not the full set.
      `synthetic`    -- fan-out AND uniform pricing AND every other signal that
          could be evaluated. `signals_checked` says which those were.
    """
    amounts = [a for a in (amounts or []) if a is not None]
    buyers = set(buyers or ())
    if len(amounts) < min_payments:
        return {"payee": payee, "verdict": "unscreenable", "payments": len(amounts),
                "buyers": len(buyers), "reason": "fewer than %d payments" % min_payments}
    if len(buyers) < min_buyers:
        # The gate. One buyer paying one seller from one funded wallet is a
        # customer, not a fabrication, and must never be scored as one.
        return {"payee": payee, "verdict": "unscreenable", "payments": len(amounts),
                "buyers": len(buyers),
                "reason": "fewer than %d buyers -- no fan-out to assess" % min_buyers}

    distinct = price_uniformity(amounts)
    uniform = distinct <= max_distinct_amounts
    excl = buyer_exclusivity(payee, buyers, pays_to or {})
    conc = funder_concentration(buyers, funders_of or {}, exclude=(payee,))

    checked, fired = ["uniform_price"], (["uniform_price"] if uniform else [])
    if excl is not None:
        checked.append("buyer_exclusivity")
        if excl >= exclusivity_threshold:
            fired.append("buyer_exclusivity")
    if conc is not None:
        checked.append("funder_concentration")
        if conc >= funder_threshold:
            fired.append("funder_concentration")

    if uniform and len(fired) == len(checked) and len(checked) >= 2:
        verdict = "synthetic"
    elif fired:
        verdict = "suspect"
    else:
        verdict = "clean"

    return {"payee": payee, "verdict": verdict,
            "payments": len(amounts), "buyers": len(buyers),
            "distinct_amounts": distinct, "uniform_price": uniform,
            "buyer_exclusivity": excl, "funder_concentration": conc,
            "signals_checked": checked, "signals_fired": fired}


def screen(amounts_by_payee, buyers_by_payee, pays_to, funders_of=None, **kw):
    """Screen every payee. Returns results sorted worst-first (synthetic, then
    suspect, then by payment volume)."""
    order = {"synthetic": 0, "suspect": 1, "clean": 2, "unscreenable": 3}
    out = [screen_payee(p, amounts_by_payee.get(p), (buyers_by_payee or {}).get(p),
                        pays_to, funders_of, **kw)
           for p in (amounts_by_payee or {})]
    out.sort(key=lambda r: (order[r["verdict"]], -r["payments"]))
    return out


def synthetic_share(results, amounts_by_payee=None):
    """What fraction of total payments the `synthetic` payees account for.

    This is the headline number -- 'X% of this network's volume is one operator
    paying itself'. Counts payments, not value: the MPP cluster moved $38.72 in
    total while generating ~19% of traffic, so a value-weighted figure would have
    reported ~0% and hidden it completely.

    The result is only as good as the sampling of `results`. Pool a dozen or more
    windows across weeks: single windows of this cluster span 4.3%-61.7% against
    a pooled 18.3%. This function cannot detect that and will not warn you."""
    total = sum(r["payments"] for r in results) if amounts_by_payee is None else \
        sum(len(v or []) for v in amounts_by_payee.values())
    if not total:
        return {"synthetic_payments": 0, "total_payments": 0, "share": 0.0}
    syn = sum(r["payments"] for r in results if r["verdict"] == "synthetic")
    return {"synthetic_payments": syn, "total_payments": total,
            "share": syn / float(total)}
