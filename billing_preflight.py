#!/usr/bin/env python3
"""
billing_preflight.py -- "if I flip billing ON, what actually happens?"

Turning billing on is a ONE-LINE config change (`BLACKWALL_PAY_TO=0x...`) that
changes what every caller sees, and every part of it fails QUIETLY:

  * a `pay_to` with a stray character still boots -- `BillingConfig` only checks
    the EVM shape, and a payment to a wrong-but-well-formed address is gone;
  * an asset the decimals table does not cover prices the challenge in units
    nobody agrees on;
  * the 402 we emit can be well-formed to us and UNREADABLE to a real x402
    client, in which case we are not "charging" -- we are just refusing;
  * a facilitator can answer and not support the (scheme, network) we quote;
  * and the pricing policy can be perfectly valid and collect NOTHING, which is
    the failure that looks exactly like success until the month ends.

That last one is the check nobody has. It was measured by hand once and the
answer was decisive: the live x402 corpus advertises a median price of ~$0.005,
and value pricing is free below $1.00, so nearly the whole ecosystem is on the
free path. A config that collects nothing should say so BEFORE it is deployed,
not after a quarter of waiting.

Everything here is stdlib-only and PURE except two seams: the facilitator probe
(injected `fetch`) and reading the committed corpus (injected `load`).

Exit codes (a scheduled run is actionable without reading the output):
  0  clean -- billing would work and would collect
  1  a person should look -- it would work but something is degraded
  2  it would not work -- a hard configuration error

CLI:
  python billing_preflight.py --pay-to 0x... [--facilitator URL] [--value-pricing]
"""

import json
import os
import sys
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------------------
# Status lattice. Ordered so `worst()` is a max -- adding a status means adding
# one row here, not touching every comparison.
# ---------------------------------------------------------------------------
OK = "ok"
NOTE = "note"
WARN = "warn"
FAIL = "fail"

_RANK = {OK: 0, NOTE: 1, WARN: 2, FAIL: 3}
EXIT_FOR = {OK: 0, NOTE: 0, WARN: 1, FAIL: 2}

# One forecast per payee in the committed corpus. Stated as a constant because
# the unit of the revenue projection is the whole claim -- see project_revenue.
#
# Resolved against THIS MODULE, not the working directory: it is a committed
# artifact of this repo, and an operator preflighting a deploy runs the script by
# path from wherever they happen to be. With a cwd-relative path that run
# silently produced "no corpus available" for the two checks the tool exists for
# -- a missing FILE presenting as a missing FINDING.
CORPUS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "directory.json")

# The resource a preflight challenge is built for. Any URL works (the challenge
# is not bound to it); a fixed one keeps the round-trip deterministic.
PREFLIGHT_RESOURCE = "https://example.invalid/v1/forecast-payment"


# How many facilitator kinds we will echo. A /supported document is written by a
# THIRD PARTY and can be arbitrarily long; the operator needs enough to see what
# is on offer, not the whole thing.
MAX_KINDS_SHOWN = 12


def _safe_text(value, limit=120):
    """Render untrusted text so it cannot forge a line in this report.

    FOURTH instance of this defect class in this repo -- `payee_syntax` echoed a
    merchant-controlled hint into `reasons[]` raw, `approvals` echoed a
    caller-supplied actor, and `secret_scan` exists because free-text fields
    reach places that render them. Here it is a FACILITATOR's `/supported`
    document: a compromised or hostile facilitator that returns a scheme
    containing a newline writes its own line into a report an operator reads
    before deciding to send it money.

    repr minus its quotes: control characters become visible escapes, ordinary
    text stays completely readable.
    """
    text = value if isinstance(value, str) else str(value)
    if len(text) > limit:
        text = text[:limit] + "..."
    return repr(text)[1:-1]


def worst(statuses):
    """The most severe status in `statuses` (OK when empty)."""
    return max(statuses, key=lambda s: _RANK.get(s, 0), default=OK)


def _check(name, status, detail, **extra):
    row = {"name": name, "status": status, "detail": detail}
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# Configuration checks (pure)
# ---------------------------------------------------------------------------
def check_payee(pay_to):
    """Is `pay_to` an address a payment can actually arrive at?

    TWO layers, because they catch different things and the second is the one
    that was found in the wild. `is_evm_address` is the shape check
    `BillingConfig` already runs. `payee_syntax.assess_payee` is the
    IMPOSSIBLE-CONTENT check -- a `.env` missing a newline glues the next
    variable onto the address, and that is a live seller bug we observed on the
    BUYING side. There is no reason to believe our own deploy is immune: it is
    the same file format and the same mistake.
    """
    from addresses import is_evm_address
    from payee_syntax import assess_payee

    if not pay_to:
        return _check("payee", FAIL, "no --pay-to: billing would stay OFF")

    signal = assess_payee(pay_to)
    grade = signal.get("grade")
    if grade == "malformed":
        return _check("payee", FAIL,
                      "pay_to contains content no address can hold (%s) -- a "
                      "payment sent here is lost" % signal.get("hint"),
                      grade=grade)
    if not is_evm_address(pay_to):
        return _check("payee", FAIL,
                      "pay_to is not a valid EVM address -- BillingConfig will "
                      "raise at boot", grade=grade)
    return _check("payee", OK, "pay_to is a well-formed EVM address",
                  grade=grade)


def check_asset(asset, network, decimals_of=None):
    """Can we SCALE the asset we are about to quote a price in, and is it USD?

    A price is a NUMBER plus a UNIT. `BillingConfig` takes `decimals=6` as a
    default argument and never consults the decimals table, so quoting a
    non-6-decimal asset advertises a price off by a power of ten. And an asset
    that is not a dollar makes the number itself mean something else -- the same
    error `payload_sim.is_non_usd` exists to catch on the buying side.
    """
    if decimals_of is None:
        from payload_sim import known_decimals as decimals_of
    from x402 import to_caip2
    from payload_sim import NON_USD_ASSETS

    caip2 = to_caip2(network)
    claim = {"asset": asset, "chain": caip2}
    dec = decimals_of(claim)
    key = (caip2, (asset or "").lower())

    if key in NON_USD_ASSETS:
        return _check("asset", FAIL,
                      "billing asset %s on %s is NOT a US dollar -- the price "
                      "knobs are all dollar-denominated"
                      % (_safe_text(asset, 80), caip2),
                      decimals=dec)
    if dec is None:
        return _check("asset", FAIL,
                      "no known decimals for %s on %s -- the quoted price would "
                      "be in units nobody agrees on"
                      % (_safe_text(asset, 80), caip2))
    if dec != 6:
        return _check("asset", WARN,
                      "asset has %d decimals but BillingConfig scales at 6 -- "
                      "pass decimals=%d explicitly" % (dec, dec), decimals=dec)
    return _check("asset", OK, "asset scales at 6 decimals and is USD-denominated",
                  decimals=dec)


def check_network(network, asset):
    """Do the network and the asset describe the SAME chain?

    Found by the preflight's own test suite, and it is a real hole in the deploy
    path rather than a hypothetical. `default_billing_asset` returns Base MAINNET
    USDC for any network it does not recognize -- `blackwall.py` prints a boot
    WARNING about exactly this -- and `to_caip2` passes an unknown bare name
    through unchanged by design ("fail-visible: the facilitator will reject
    invalid_network rather than us silently mislabeling the chain"). Those two
    behaviours compose into a challenge advertising an eip155 USDC address on a
    network called `solana`, and the decimals check cannot catch it because
    `known_decimals` falls back to an address-only table that answers 6 whatever
    the chain says.

    Deferring to the facilitator to reject it is right for the RUNTIME and wrong
    for a preflight: "the facilitator will reject every payment" is precisely the
    answer this tool exists to give BEFORE the deploy.
    """
    from x402 import to_caip2

    caip2 = to_caip2(network)
    if not caip2 or ":" not in str(caip2):
        return _check("network", FAIL,
                      "network %s does not resolve to a CAIP-2 id -- it is "
                      "advertised verbatim and a facilitator rejects it as "
                      "invalid_network" % _safe_text(network, 40))
    namespace = str(caip2).split(":", 1)[0]
    is_evm_asset = isinstance(asset, str) and asset.lower().startswith("0x")
    if namespace == "eip155" and not is_evm_asset:
        return _check("network", FAIL,
                      "network %s is EVM but the billing asset is not an EVM "
                      "address" % caip2, caip2=caip2)
    if namespace != "eip155" and is_evm_asset:
        return _check("network", FAIL,
                      "network %s is not EVM but the billing asset is an EVM "
                      "address -- default_billing_asset falls back to Base "
                      "mainnet USDC for an unrecognized network" % caip2,
                      caip2=caip2)
    return _check("network", OK, "network resolves to %s and the asset matches "
                  "its namespace" % caip2, caip2=caip2)


def check_pricing(price, value_pricing=False, knobs=None):
    """Would the pricing config construct, and what does it MEAN?

    `BillingConfig` and `PricingPolicy` both raise on bad input -- at BOOT, in a
    container, where the traceback goes to a log nobody is reading. Constructing
    them here turns that into an answer.
    """
    from x402 import BillingConfig, PricingPolicy  # noqa: F401  (BillingConfig used by caller)

    if not value_pricing:
        from x402 import to_atomic
        atomic = to_atomic(price, 6)
        if atomic is None or atomic <= 0:
            return _check("pricing", FAIL,
                          "flat price %r is not a positive amount at 6 decimals "
                          "-- BillingConfig raises at boot" % (price,))
        return _check("pricing", OK,
                      "FLAT pricing: every billable call costs %s USDC" % price,
                      mode="flat", price=str(price))

    try:
        policy = PricingPolicy(**(knobs or {}))
    except ValueError as e:
        return _check("pricing", FAIL, "value pricing rejected its config: %s" % e)
    return _check("pricing", OK,
                  "VALUE pricing: free at or below %s, else %s bps capped at %s "
                  "(and capped at %s bps of the amount)"
                  % (policy.free_below, policy.bps, policy.max_fee,
                     policy.max_fee_ratio_bps),
                  mode="value", free_below=str(policy.free_below),
                  bps=str(policy.bps), max_fee=str(policy.max_fee))


# ---------------------------------------------------------------------------
# The challenge round-trip (pure)
# ---------------------------------------------------------------------------
# The fields a payer SIGNS. A mismatch on any of these is not cosmetic: the
# payment is built from what the client parsed, so if the client reads a
# different payTo or amount than we configured, it pays the wrong party or the
# wrong amount and `payment_satisfies` rejects it -- an unsatisfiable loop.
SIGNED_FIELDS = ("scheme", "network", "amount", "asset", "payTo")


def compare_accept(accept, expected):
    """Field-by-field diff of a parsed accept against what we configured.

    Returns a list of human-readable mismatch strings (empty == agreement).
    Comparison is on STRINGS because that is what crosses the wire -- an int
    amount and a str amount are the same value and a DIFFERENT payload.
    """
    problems = []
    for field in SIGNED_FIELDS:
        want = expected.get(field)
        got = accept.get(field) if isinstance(accept, dict) else None
        if got is None:
            problems.append("%s: absent from the parsed challenge" % field)
        elif str(got).lower() != str(want).lower():
            problems.append("%s: configured %r, a client reads %r"
                            % (field, want, got))
    return problems


def roundtrip_carriers(body, header_b64):
    """Parse our OWN 402 back through our OWN parser, per carrier.

    Returns {carrier_name: (accept_or_None, carrier_label)}. This is the only
    check here that proves a real x402 client can PAY us rather than merely be
    refused by us: a challenge is a document we write and a stranger reads, and
    the two halves have been out of step in this ecosystem before (86 of 195
    live hosts served requirements in a carrier nothing in this repo read).
    """
    from x402_challenge import parse_challenge

    out = {}
    accepts, carrier = parse_challenge(json.dumps(body), {})
    out["body"] = (accepts[0] if accepts else None, carrier)
    # A client that reads ONLY the header must see the same thing. Our server
    # emits both, so an empty body here is the honest isolation of that path.
    accepts, carrier = parse_challenge("{}", {"PAYMENT-REQUIRED": header_b64})
    out["header"] = (accepts[0] if accepts else None, carrier)
    return out


def check_challenge(gate, expected, resource=PREFLIGHT_RESOURCE, amount_at_risk=None):
    """Emit the 402 we would serve and confirm a client can read it."""
    import base64

    result = gate.check(resource, amount_at_risk=amount_at_risk)
    if result.paid:
        return _check("challenge", WARN,
                      "no 402 is emitted at amount_at_risk=%r -- this call is "
                      "served FREE (value pricing below the threshold)"
                      % (amount_at_risk,), free=True)
    body = result.body
    if not isinstance(body, dict) or not body.get("accepts"):
        return _check("challenge", FAIL,
                      "the 402 body carries no accepts[] -- there is nothing to pay")

    header_b64 = base64.b64encode(
        json.dumps(body).encode("utf-8")).decode("ascii")
    carriers = roundtrip_carriers(body, header_b64)

    problems = []
    for name, (accept, carrier) in sorted(carriers.items()):
        if accept is None:
            problems.append("%s carrier: unreadable by our own parser" % name)
            continue
        for p in compare_accept(accept, expected):
            problems.append("%s carrier: %s" % (name, p))
    if problems:
        return _check("challenge", FAIL,
                      "the 402 we emit does not round-trip: " + "; ".join(problems),
                      problems=problems)
    return _check("challenge", OK,
                  "the 402 round-trips through both carriers with the configured "
                  "payTo/amount/asset/network (quoting %s atomic units)"
                  % expected.get("amount"),
                  carriers=sorted(c for _, c in carriers.values()),
                  quoted_amount=str(expected.get("amount")))


# ---------------------------------------------------------------------------
# Facilitator probe (network injected)
# ---------------------------------------------------------------------------
def supported_kinds(doc):
    """Normalize a /supported response to a set of (scheme, network) pairs.

    Tolerant by contract: this reads a THIRD party's document, and the shape has
    already drifted once in this ecosystem (v1 `maxAmountRequired` -> v2
    `amount`). Junk yields an empty set, never an exception.
    """
    kinds = set()
    if not isinstance(doc, dict):
        return kinds
    for entry in (doc.get("kinds") or []):
        if not isinstance(entry, dict):
            continue
        scheme = entry.get("scheme")
        network = entry.get("network")
        if scheme and network:
            # Sanitized HERE, at the trust boundary, so every consumer is
            # covered rather than each format site remembering to. A legitimate
            # value survives unchanged, so the (scheme, network) comparison is
            # unaffected; a hostile one simply fails to match, which is correct.
            kinds.add((_safe_text(scheme, 40), _safe_text(network, 40)))
        if len(kinds) >= 200:  # a capability list, not a data feed
            break
    return kinds


def check_facilitator(url, scheme, network, fetch=None,
                      cdp_id=None, cdp_secret=None, authed_fetch=None):
    """Does the facilitator answer, and does it support what we QUOTE?

    The two failure modes are deliberately graded differently:
      * unreachable -> WARN. A facilitator can be down for a minute and fine at
        deploy time; refusing to deploy over a transient blip is its own bug.
      * reachable but does NOT list our (scheme, network) -> FAIL. That is a
        configuration error which will never work no matter how long you wait,
        and it presents as "every payment is rejected" in production.
    """
    from x402 import to_caip2

    if cdp_id and cdp_secret:
        # Mirror `choose_facilitator` by CALLING it, so this cannot drift from
        # the selection the server actually makes. Two things worth an operator's
        # attention come out of that call: CDP is the only Bazaar-cataloging
        # path, and a non-CDP `facilitator_url` set alongside CDP creds is
        # silently IGNORED (deliberately -- sending a CDP Bearer JWT to a
        # community facilitator would leak an auth token).
        from x402 import choose_facilitator
        facilitator, note = choose_facilitator(url, cdp_id, cdp_secret)
        base = getattr(facilitator, "base_url", "") or ""
        # PROBE IT. This used to return NOTE with "/supported is authenticated,
        # so it was NOT probed here" -- which meant the single most likely way a
        # mainnet deploy fails, a mistyped or wrong-scoped CDP credential, PASSED
        # the preflight whose entire job is to answer "what happens if I flip
        # billing on?". A rejected credential is not transient and no amount of
        # waiting fixes it: it presents in production as every settlement
        # failing while the service reports healthy. Authenticated is a reason to
        # MINT A TOKEN, not a reason to skip the check.
        authed = _cdp_get_json if authed_fetch is None else authed_fetch
        try:
            doc = authed(base.rstrip("/") + "/supported", cdp_id, cdp_secret)
        except _CredentialsRejected as e:
            return _check("facilitator", FAIL,
                          "CDP rejected the credentials (%s) -- billing would be "
                          "ON and every settlement would fail. Check "
                          "CDP_API_KEY_ID / CDP_API_KEY_SECRET and that the key "
                          "is enabled for x402." % _safe_text(e, 120))
        except Exception as e:
            return _check("facilitator", WARN,
                          "%s -- but /supported did not answer (%s); could be "
                          "transient, support for %s was NOT confirmed"
                          % (_safe_text(note, 200), _safe_text(e, 120), scheme))
        result = _grade_kinds(base, doc, scheme, network)
        if "IGNORING" in note and result["status"] == OK:
            # The credentials work AND a non-CDP facilitator_url is being
            # silently dropped -- the operator should know their setting is inert.
            return _check("facilitator", WARN,
                          _safe_text(note, 200) + " -- credentials accepted and "
                          + result["detail"], kinds=result.get("kinds"))
        return result
    if not url:
        return _check("facilitator", WARN,
                      "no facilitator configured -- BillingGate falls back to "
                      "the built-in MOCK, which reports every payment settled "
                      "and moves NO money")
    if fetch is None:
        fetch = _http_get_json

    try:
        doc = fetch(url.rstrip("/") + "/supported")
    except Exception as e:
        # The exception string can carry server-supplied text (urllib puts the
        # response reason in HTTPError), so it is untrusted too.
        return _check("facilitator", WARN,
                      "facilitator %s did not answer /supported (%s) -- could be "
                      "transient; it was NOT confirmed to speak the protocol"
                      % (_safe_text(url), _safe_text(e)))
    return _grade_kinds(url, doc, scheme, network)


def _grade_kinds(url, doc, scheme, network):
    """Grade a /supported document. Shared by the keyless and the CDP path so
    the two cannot drift on what "supports what we quote" means."""
    from x402 import to_caip2

    caip2 = to_caip2(network)
    kinds = supported_kinds(doc)
    if not kinds:
        return _check("facilitator", WARN,
                      "facilitator %s answered /supported with no readable kinds "
                      "-- support for %s/%s is unconfirmed"
                      % (_safe_text(url), scheme, caip2))
    # A facilitator may list either spelling of the network.
    for candidate in (caip2, network):
        if (scheme, str(candidate)) in kinds:
            return _check("facilitator", OK,
                          "facilitator %s supports %s on %s"
                          % (_safe_text(url), scheme, candidate),
                          kinds=sorted("%s/%s" % k for k in kinds))
    listed = sorted("%s/%s" % k for k in kinds)
    shown = ", ".join(listed[:MAX_KINDS_SHOWN])
    if len(listed) > MAX_KINDS_SHOWN:
        shown += " (+%d more)" % (len(listed) - MAX_KINDS_SHOWN)
    return _check("facilitator", FAIL,
                  "facilitator %s does NOT support %s on %s (it lists: %s) -- "
                  "every payment would be rejected"
                  % (_safe_text(url), scheme, caip2, shown), kinds=listed)


class _CredentialsRejected(Exception):
    """The facilitator answered and refused the credentials -- never transient."""


def _cdp_get_json(url, key_id, key_secret, timeout=8.0):
    """GET an authenticated CDP endpoint with a freshly minted Bearer JWT.

    401/403 is raised as `_CredentialsRejected` rather than a generic error
    because the two grade differently: unreachable is transient and warns,
    rejected is a configuration error that fails.
    """
    from cdp_auth import build_cdp_jwt

    token = build_cdp_jwt(key_id, key_secret, "GET", url)
    req = urllib.request.Request(url, headers={
        "accept": "application/json", "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read(1 << 20).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise _CredentialsRejected("HTTP %s" % e.code)
        raise


def _http_get_json(url, timeout=8.0):
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(1 << 20).decode("utf-8"))


# ---------------------------------------------------------------------------
# Revenue projection (pure)
# ---------------------------------------------------------------------------
def _dec(value):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() and d >= 0 else None


def price_points(rows):
    """(payee, min_price, max_price) for every corpus row that advertises both.

    `data/directory.json` stores the min/max HULL of each payee's price list,
    NOT the list -- the exact caveat `advertised_prices.py` documents and the
    reason a hull may not vouch for a quote on its own. So this returns the
    BOUNDS and `project_revenue` reports an interval; collapsing a hull into a
    point estimate would be inventing a distribution we never measured.
    """
    points = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        lo = _dec(row.get("min_price"))
        hi = _dec(row.get("max_price"))
        if lo is None or hi is None:
            continue
        if hi < lo:
            lo, hi = hi, lo
        points.append((row.get("payee"), lo, hi))
    return points


def project_revenue(fee_atomic, points, decimals=6):
    """What the configured policy collects over the corpus, as an INTERVAL.

    THE UNIT, stated because a revenue number without one is noise: ONE forecast
    request per payee in the corpus, each priced at that payee's own advertised
    amount. `low` prices every payee at its cheapest advertised option, `high` at
    its dearest. The truth is in between and we do not know where, because the
    corpus stores bounds rather than the list.

    Returns {low, high, billable_low, billable_high, total} where the money
    figures are HUMAN units (a fee is atomic; a projection is read by a person).
    """
    scale = Decimal(10) ** int(decimals)
    out = {"total": len(points)}
    for label, index in (("low", 1), ("high", 2)):
        billable = 0
        gross = Decimal(0)
        for point in points:
            fee = fee_atomic(point[index])
            if fee and fee > 0:
                billable += 1
                gross += Decimal(fee) / scale
        out[label] = str(gross)
        out["billable_" + label] = billable
    return out


def fee_ratios(fee_atomic, points, index, decimals=6):
    """Sorted fee/amount ratios over the corpus at one end of the price hull."""
    scale = Decimal(10) ** int(decimals)
    ratios = []
    for point in points:
        amount = point[index]
        if amount <= 0:
            continue
        fee = Decimal(fee_atomic(amount) or 0) / scale
        ratios.append(fee / amount)
    ratios.sort()
    return ratios


def check_proportionality(fee_atomic, points, bound_bps=100, decimals=6,
                          source=None):
    """What share of the payment does the fee TAKE?

    THIS IS THE CHECK THAT ONLY EXISTS HERE. `x402.PricingPolicy` enforces a
    proportionality invariant -- the fee may never exceed `max_fee_ratio_bps` of
    the amount at risk, and its own comment names the reason: "min_fee is an
    absolute floor that becomes an ever-larger share of a shrinking payment --
    and the median live x402 quote is $0.005". But that invariant lives in
    PricingPolicy, and `BillingGate._price_for` consults the policy ONLY when one
    is configured. The SHIPPED DEFAULT is flat pricing with no policy, so the
    default path never applies the bound its own module documents as necessary.

    Measured on the committed corpus at the shipped flat price of $0.001: the fee
    is a MEDIAN 20% of the payment being screened at each payee's cheapest
    advertised option, and exceeds the 1% bound for 251 of 265 payees. Under
    value pricing the identical amounts return free instead.

    Graded on evidence, not taste: charging 20% is a pricing DECISION an operator
    may make deliberately, so it warns. A fee at or above the payment itself is
    not a decision -- nobody pays $1 to screen $1 -- so a majority in that state
    fails.
    """
    if not points:
        return _check("proportionality", WARN,
                      "no corpus available%s -- fee proportionality was NOT "
                      "measured"
                      % (" at %s" % _safe_text(source, 200) if source else ""))
    bound = Decimal(bound_bps) / Decimal(10000)
    low = fee_ratios(fee_atomic, points, 1, decimals)
    high = fee_ratios(fee_atomic, points, 2, decimals)
    if not low or not high:
        return _check("proportionality", WARN,
                      "corpus carries no positive amounts -- not measured")
    median_low = low[len(low) // 2]
    over = sum(1 for r in low if r > bound)
    unpayable = sum(1 for r in high if r >= 1)
    stats = {"median_ratio_cheapest": "%.4f" % median_low,
             "over_bound_cheapest": over,
             "unpayable_dearest": unpayable,
             "measured": len(low)}
    if unpayable * 2 > len(high):
        return _check("proportionality", FAIL,
                      "the fee is at or above the whole payment for %d of %d "
                      "payees even at their DEAREST advertised price -- there is "
                      "no rational buyer" % (unpayable, len(high)), **stats)
    if median_low > bound:
        return _check("proportionality", WARN,
                      "the fee takes a median %.1f%% of the payment at each "
                      "payee's cheapest option (%d of %d exceed the %s bps "
                      "bound) -- PricingPolicy would cap this, flat pricing does "
                      "not consult it"
                      % (median_low * 100, over, len(low), bound_bps), **stats)
    return _check("proportionality", OK,
                  "the fee stays within %s bps of the payment for the corpus "
                  "(median %.3f%% at the cheapest end)"
                  % (bound_bps, median_low * 100), **stats)


# Coinbase CDP facilitator pricing, read from their docs on 2026-09-08
# (docs.cdp.coinbase.com/x402/seller/facilitator): "The first 1,000 onchain
# Facilitator transactions each month are free, then each additional onchain
# transaction costs $0.001." Verification is free; only SETTLEMENT costs.
#
# A THIRD PARTY'S PRICE, so it is dated and overridable rather than treated as a
# constant of nature -- when it moves, the number here is wrong and the check
# would quietly mis-measure. `--settlement-cost` overrides it.
SETTLEMENT_COST = Decimal("0.001")
SETTLEMENT_FREE_TIER = 1000


def check_settlement_cost(fee_atomic, points, settle=SETTLEMENT_COST,
                          decimals=6, free_tier=SETTLEMENT_FREE_TIER,
                          source=None):
    """Does the fee cover what it COSTS to collect the fee?

    THE GAP THIS CLOSES: every other money check here asks what we charge.
    None asked what charging COSTS. Collecting an x402 payment means the
    facilitator broadcasts an onchain settlement, and past the free tier that
    settlement has a price. A fee below it is not thin margin -- it is a
    payment we lose money by accepting, and it looks identical to revenue in
    every report until the invoice arrives.

    MEASURED on the committed corpus at the shipped value pricing (10 bps,
    min fee $0.0001) against CDP's $0.001: 41 of 46 billable payees (89.1%)
    at the cheapest end of the hull, 133 of 164 (81.1%) at the dearest, at a
    mean shortfall of $0.000882. Break-even lands at $0.9995 rather than the
    $1.00 the arithmetic suggests (10 bps of $1 is exactly $0.001) because the
    real fee function ROUNDS -- which is why `_breakeven_amount` bisects that
    function instead of inverting the bps. An earlier note here said "140 of
    164"; that figure took the first billable of each payee's min/max and so
    belonged to neither end of the hull. Both real ends are reported above.

    GRADED AS ECONOMICS, NEVER AS BREAKAGE -- so WARN, never FAIL. Billing
    still works: the 402 is valid, the payer pays, the money arrives. Selling
    below cost is a decision an operator may make deliberately (a loss-leader
    that buys the verdict->outcome history this engine is built to accumulate),
    and the free tier makes it cost literally nothing at low volume. FAIL means
    "this would not work", and that is not what is wrong here.

    The fee is compared at each payee's CHEAPEST advertised option, which is the
    end where the absolute min-fee floor binds and margin is thinnest -- the
    same end `check_proportionality` measures, and the pessimistic one.
    """
    if not points:
        return _check("settlement_cost", WARN,
                      "no corpus available%s -- settlement economics were NOT "
                      "measured"
                      % (" at %s" % _safe_text(source, 200) if source else ""))
    settle = Decimal(str(settle))
    scale = Decimal(10) ** int(decimals)
    billable = below = 0
    shortfall = Decimal(0)
    for point in points:
        fee = Decimal(fee_atomic(point[1]) or 0) / scale
        if fee <= 0:
            continue                      # free path: no settlement, no cost
        billable += 1
        if fee < settle:
            below += 1
            shortfall += settle - fee
    if not billable:
        return _check("settlement_cost", OK,
                      "nothing is billed, so no settlement is ever paid for",
                      billable=0)
    # Break-even is a property of the POLICY, not of any one payee, so it is
    # found by asking the real fee function rather than re-deriving the bps --
    # that would be a second implementation of pricing, free to drift from the
    # one that actually quotes.
    breakeven = _breakeven_amount(fee_atomic, settle, decimals)
    stats = {"billable": billable, "below_settlement": below,
             "settlement_cost": str(settle),
             "avg_shortfall": ("%.6f" % (shortfall / below)) if below else "0",
             "breakeven": str(breakeven) if breakeven is not None else None,
             "free_tier": free_tier}
    if not below:
        return _check("settlement_cost", OK,
                      "every payment billable at its cheapest advertised "
                      "option covers the %s settlement cost (%d of %d)"
                      % (settle, billable, billable), **stats)
    where = ("; break-even is a %s payment" % breakeven
             if breakeven is not None else "")
    return _check("settlement_cost", WARN,
                  "%d of %d payees billable at their CHEAPEST advertised "
                  "option are billed BELOW the %s it costs to settle them, "
                  "losing a mean %s each%s -- free for the first %d "
                  "settlements a month, then real money"
                  % (below, billable, settle, stats["avg_shortfall"], where,
                     free_tier), **stats)


def _breakeven_amount(fee_atomic, settle, decimals=6, ceiling=Decimal("1000")):
    """Smallest advertised amount whose fee covers `settle`, or None.

    Bisection over the REAL fee function. Pricing is monotonic in the amount
    (bps of it, clamped), so bisection is valid; it is deliberately not an
    inversion of the bps formula, which would duplicate pricing here.
    """
    scale = Decimal(10) ** int(decimals)

    def covers(amount):
        return Decimal(fee_atomic(amount) or 0) / scale >= settle

    if not covers(ceiling):
        return None                       # even a huge payment cannot cover it
    lo, hi = Decimal(0), ceiling
    for _ in range(40):
        mid = (lo + hi) / 2
        if covers(mid):
            hi = mid
        else:
            lo = mid
    return hi.quantize(Decimal("0.0001"))


def check_revenue(projection, source=None):
    """Would this configuration collect ANYTHING?

    FAIL when even the most generous end of the interval bills nobody: a policy
    that cannot charge its own addressable market is not a pricing decision, it
    is an outage that reports success. WARN when the generous end bills a small
    minority -- which is the SHIPPED default, and is a real business fact rather
    than a bug: the live x402 market is too cheap for a per-transaction fee.
    """
    total = projection.get("total") or 0
    high = projection.get("billable_high") or 0
    low = projection.get("billable_low") or 0
    if not total:
        return _check("revenue", WARN,
                      "no corpus available%s -- revenue was NOT projected (this "
                      "is a missing file, not a finding about your config)"
                      % (" at %s" % _safe_text(source, 200) if source else ""))
    if high == 0:
        return _check("revenue", FAIL,
                      "this configuration bills 0 of %d corpus payees even at "
                      "their DEAREST advertised price -- it would collect "
                      "nothing" % total, **projection)
    share = 100.0 * high / total
    detail = ("bills %d-%d of %d corpus payees (%.1f%% at the dearest end); "
              "projected %s-%s USDC for one forecast each"
              % (low, high, total, share,
                 projection.get("low"), projection.get("high")))
    if share < 25.0:
        return _check("revenue", WARN, detail + " -- the free path serves the "
                      "large majority of this market", **projection)
    return _check("revenue", OK, detail, **projection)


def check_self_reported_amount(mode):
    """Value pricing derives the fee from a field the CALLER writes.

    `_Handler` passes `payload["amount"]` straight to `BillingGate.check` as
    `amount_at_risk`, and nothing verifies it: a caller who declares $0.50 is on
    the free path. What they get back is a verdict scored at $0.50 -- so the
    amount-DEPENDENT parts (the budget threshold, blast radius) degrade -- but
    the counterparty screen does not depend on the amount at all. Sanctions,
    payee syntax, reputation, Sybil and price-anomaly all still run and are
    arguably the valuable half of the answer.

    This is not a defect to fix here; understating is self-limiting for the
    caller who actually wants the budget check. It is a fact an operator should
    know BEFORE choosing value pricing, so it is reported rather than gated.
    """
    if mode != "value":
        return _check("self_reported_amount", OK,
                      "flat pricing does not read a caller-supplied amount")
    return _check("self_reported_amount", NOTE,
                  "value pricing prices on the caller-declared `amount`, which "
                  "is unverified -- a caller declaring a sub-threshold amount "
                  "gets the counterparty screen free (the budget/blast-radius "
                  "half of the verdict degrades, which is self-limiting)")


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def load_corpus(path=CORPUS_PATH, load=None):
    """Read the committed corpus; missing/corrupt -> [] (the projection warns)."""
    if load is None:
        def load(p):
            with open(p, "r", encoding="utf-8") as fh:
                return json.load(fh)
    try:
        rows = load(path)
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def preflight(pay_to, facilitator=None, network="base", asset=None,
              price="0.001", value_pricing=False, knobs=None,
              corpus=None, fetch=None, offline=False,
              cdp_id=None, cdp_secret=None, corpus_path=None,
              settlement_cost=SETTLEMENT_COST):
    """Run every check and return the report. No side effects beyond `fetch`."""
    from x402 import (BASE_SEPOLIA_USDC, BASE_USDC, BillingConfig, BillingGate,
                      DEFAULT_SCHEME, PricingPolicy, to_caip2)
    from blackwall import default_billing_asset

    checks = [check_payee(pay_to)]
    if checks[0]["status"] == FAIL:
        # Everything downstream constructs a BillingConfig, which raises on a bad
        # payee. Report the one real finding rather than a cascade of noise.
        return {"status": FAIL, "checks": checks, "pay_to": pay_to}

    asset = default_billing_asset(network, asset, BASE_USDC, BASE_SEPOLIA_USDC)
    checks.append(check_network(network, asset))
    checks.append(check_asset(asset, network))

    pricing_check = check_pricing(price, value_pricing, knobs)
    checks.append(pricing_check)
    checks.append(check_self_reported_amount(pricing_check.get("mode")))

    policy = None
    if pricing_check["status"] != FAIL and value_pricing:
        policy = PricingPolicy(**(knobs or {}))

    if pricing_check["status"] != FAIL:
        cfg = BillingConfig(price=price, pay_to=pay_to, network=network,
                            asset=asset)
        gate = BillingGate(cfg, pricing=policy)
        # Probe the challenge at a REALISTIC billable amount under value
        # pricing, not at the default. `fee_atomic(None)` falls back to
        # `min_fee`, so a None probe would still emit a 402 -- but it would
        # advertise the FLOOR price rather than the value-derived one, and the
        # value-derived price is what an agent actually pays. Round-tripping the
        # floor would leave the mode's real quote unexercised.
        probe_amount = None
        if policy is not None:
            probe_amount = str(policy.free_below * 1000)
        expected = {
            "scheme": DEFAULT_SCHEME,
            "network": to_caip2(network),
            "asset": asset,
            "payTo": pay_to,
            "amount": str(gate._price_for(probe_amount)),
        }
        checks.append(check_challenge(gate, expected, amount_at_risk=probe_amount))

        source = None
        if corpus is None:
            source = corpus_path or CORPUS_PATH
            corpus = load_corpus(source)
        points = price_points(corpus)
        projection = project_revenue(gate._price_for, points)
        checks.append(check_revenue(projection, source=source))
        bound_bps = int(policy.max_fee_ratio_bps) if policy is not None else 100
        checks.append(check_proportionality(gate._price_for, points,
                                            bound_bps=bound_bps, source=source))
        checks.append(check_settlement_cost(gate._price_for, points,
                                            settle=settlement_cost,
                                            source=source))

    if offline:
        checks.append(_check("facilitator", NOTE,
                             "--offline: the facilitator was not probed"))
    else:
        checks.append(check_facilitator(facilitator, DEFAULT_SCHEME, network,
                                        fetch=fetch, cdp_id=cdp_id,
                                        cdp_secret=cdp_secret))

    return {"status": worst(c["status"] for c in checks),
            "checks": checks, "pay_to": pay_to, "asset": asset,
            "network": network}


_MARK = {OK: "ok  ", NOTE: "note", WARN: "WARN", FAIL: "FAIL"}


def format_report(report):
    lines = ["billing preflight: pay_to=%s network=%s"
             % (report.get("pay_to"), report.get("network"))]
    for c in report["checks"]:
        lines.append("  [%s] %-22s %s" % (_MARK.get(c["status"], "?"),
                                          c["name"], c["detail"]))
    lines.append("overall: %s" % report["status"].upper())
    return "\n".join(lines)


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    # Every flag defaults to the env var the SERVER reads, so running this with a
    # deploy's environment loaded checks THAT config rather than a set of
    # defaults nothing is actually running. Two blueprints once carried a
    # free_below a third documented as unreachable; a preflight that could only
    # check its own defaults would not have caught it.
    env = os.environ.get
    p.add_argument("--pay-to", default=env("BLACKWALL_PAY_TO"),
                   help="the funded EVM wallet billing would pay to")
    p.add_argument("--facilitator", default=env("BLACKWALL_FACILITATOR"),
                   help="x402 facilitator base URL")
    p.add_argument("--network", default=env("BLACKWALL_NETWORK", "base"))
    p.add_argument("--asset", default=env("BLACKWALL_ASSET"),
                   help="billing asset (default: USDC for the network)")
    p.add_argument("--price", default=env("BLACKWALL_PRICE", "0.001"),
                   help="flat per-forecast price")
    p.add_argument("--value-pricing", action="store_true",
                   default=bool(env("BLACKWALL_VALUE_PRICING")))
    p.add_argument("--free-below", default=env("BLACKWALL_FREE_BELOW", "1.00"))
    p.add_argument("--bps", default=env("BLACKWALL_PRICE_BPS", "10"))
    p.add_argument("--min-fee", default=env("BLACKWALL_MIN_FEE", "0.001"))
    p.add_argument("--max-fee", default=env("BLACKWALL_MAX_FEE", "0.10"))
    p.add_argument("--max-fee-ratio-bps",
                   default=env("BLACKWALL_MAX_FEE_RATIO_BPS", "100"))
    p.add_argument("--corpus", default=None,
                   help="price corpus to project against (default: the "
                        "committed data/directory.json beside this script)")
    p.add_argument("--settlement-cost", default=str(SETTLEMENT_COST),
                   help="USD your facilitator charges per onchain settlement "
                        "(default %s, CDP's price as of 2026-09-08 past its "
                        "free tier; a THIRD PARTY'S number, so override it "
                        "when it moves)" % SETTLEMENT_COST)
    p.add_argument("--offline", action="store_true",
                   help="skip the facilitator probe (no network)")
    p.add_argument("--json", metavar="PATH", help="also write the report as JSON")
    args = p.parse_args(argv)

    report = preflight(
        args.pay_to, facilitator=args.facilitator, network=args.network,
        cdp_id=env("CDP_API_KEY_ID"), cdp_secret=env("CDP_API_KEY_SECRET"),
        asset=args.asset, price=args.price, value_pricing=args.value_pricing,
        knobs={"free_below": args.free_below, "bps": args.bps,
               "min_fee": args.min_fee, "max_fee": args.max_fee,
               "max_fee_ratio_bps": args.max_fee_ratio_bps},
        offline=args.offline, corpus_path=args.corpus,
        settlement_cost=args.settlement_cost)
    sys.stdout.write(format_report(report) + "\n")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, sort_keys=True)
    return EXIT_FOR.get(report["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
