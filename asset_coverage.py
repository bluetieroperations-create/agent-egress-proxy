"""
asset_coverage.py -- does the decimals table still cover what the ecosystem
actually quotes?

WHY THIS EXISTS. `payload_sim.KNOWN_DECIMALS_BY_CHAIN` is a SNAPSHOT: it covers
the assets the live x402 corpus advertised on the day it was built. An asset that
is not in it resolves to unknown, which is safe -- the amount is reported
`unverified_decimals` rather than compared at a guessed scale -- but the check is
switched off for that payment. Nothing tells us when that starts happening.

This module answers three questions from one pass over the live corpus:

  1. COVERAGE  -- what fraction of live quotes we can scale, and exactly which
                  (network, asset) pairs we cannot. That list is the work item.
  2. DRIFT     -- which hosts serve each unresolved pair, so a new asset can be
                  traced to the sellers that introduced it. A BROKEN identifier
                  is separated from a merely unknown one: the first run found a
                  seller advertising a 39-character address, which is a bug to
                  report, not decimals to look up.
  3. SANITY    -- with the table applied, does every quote land at a plausible
                  price? A wrong decimals value does not hide here: it shows up
                  as an absurd implied price. This is how the corpus itself
                  corroborated Stellar's 7 (its quotes priced to $0.002/$0.02,
                  matching the same sellers' 6-decimal legs).

WHAT IT DELIBERATELY DOES NOT DO: resolve anything on-chain and write it into the
table. `KNOWN_DECIMALS_BY_CHAIN` gates payments, and a scale read from a single
public RPC is a value an operator of that RPC chose. Auto-committing it would let
one lying endpoint mis-scale a gate. Resolution stays a reviewed step -- see
docs/DECIMALS_AUDIT.md for the procedure actually used (read every public RPC the
chain lists and require agreement). This module produces the WORK LIST; a human
decides what enters the table.

The decision-critical logic is pure and network-free; the network is injected.

CLI:
    python asset_coverage.py data/liveness.json [--json report.json]
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation

USER_AGENT = "blackwall-asset-coverage/1.0"
TIMEOUT = 12
MAX_BODY = 200_000

# A micro-payment band, deliberately wide. Anything outside it is not "wrong",
# it is WORTH LOOKING AT -- these are per-call API prices, so a quote implying
# a hundredth of a cent or several thousand dollars means either an unusual
# seller or a decimals value that is off by orders of magnitude.
MIN_PLAUSIBLE = Decimal("0.000001")
MAX_PLAUSIBLE = Decimal("1000")

# Assets in the table that are NOT dollar-denominated. Their quotes are perfectly
# valid and simply cannot be judged against a USD band, so they are reported
# separately rather than run through it. NO exchange rates are kept here on
# purpose: a hardcoded rate is stale the day it is written, and this module must
# not turn a currency guess into a "suspicious price" finding.
NON_USD = {
    ("eip155:137", "0x431d5dff03120afa4bdf332c61a6e1766ef37bdb"),   # JPYC, yen
    ("eip155:8453", "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42"),  # EURC, euro
}


# ===========================================================================
# PURE core
# ===========================================================================
def asset_key(network, asset):
    """(network, asset) normalized the way the decimals table is keyed, or None.

    Must agree with `payload_sim._chain_decimals`, which lowercases both halves.
    Keeping the normalization here identical is the point: a coverage report that
    normalized differently would count assets as missing that the engine resolves
    perfectly well, and send someone to fix a table that is already correct.
    """
    if not isinstance(network, str) or not isinstance(asset, str):
        return None
    net, addr = network.strip().lower(), asset.strip().lower()
    if not net or not addr:
        return None
    return (net, addr)


# An identifier is a bare address or token id. It is never a URL and never a
# key=value pair, on any chain -- so these are safe to reject without knowing
# the chain's address format.
_NEVER_IN_AN_IDENTIFIER = ("=", "://", " ", "\t", "\n")


def is_malformed_identifier(value):
    """True when an on-chain identifier is BROKEN rather than merely unknown.

    BOTH CASES HERE WERE FOUND ON THIS MODULE'S FIRST LIVE RUN, on one seller:

      * an asset of `0x8AC76a51cc950d9822D68b83fE43AD4843bA77E` -- **39** hex
        characters, not 40. A truncated address, not a token. Reporting it as
        "unresolved" would send someone to look up decimals for a contract that
        cannot exist.
      * a Solana `payTo` of `2DgEL95L8Dta...WYcpFACILITATOR_URL=https://x402.org/
        facilitator` -- a real address with an environment variable concatenated
        onto it, almost certainly a missing newline in a `.env`. That one matters
        more than a decimals gap: it is the address an agent would PAY.

    Two independent checks, because neither catches the other. Content that
    cannot appear in any chain's identifier (`=`, `://`, whitespace) is rejected
    without needing to know the address format -- which is what catches the
    Solana case, since there is no cheap base58 validity test. Then `0x`-prefixed
    values must be valid EVM addresses. Non-EVM identifiers that look clean are
    NOT judged: we cannot validate them, and guessing would condemn real assets.
    """
    if not isinstance(value, str):
        return True
    text = value.strip()
    if not text:
        return True
    if any(bad in text for bad in _NEVER_IN_AN_IDENTIFIER):
        return True
    if not text.lower().startswith("0x"):
        return False
    from addresses import is_evm_address
    return not is_evm_address(text)


def is_malformed_asset(asset):
    """Back-compat alias -- assets and payees get the same treatment."""
    return is_malformed_identifier(asset)


def pairs_from_accepts(rows):
    """Accept rows -> {key: {network, asset, hosts, amounts}}.

    `rows` are dicts with host/network/asset/amount -- the shape `harvest`
    produces. Rows we cannot key are dropped rather than counted as unresolved:
    an entry with no asset is a malformed challenge, not a missing table entry.
    """
    out = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        key = asset_key(row.get("network"), row.get("asset"))
        if key is None:
            continue
        entry = out.setdefault(key, {"network": row.get("network"),
                                     "asset": row.get("asset"),
                                     "hosts": set(), "amounts": [],
                                     "payees": {}})
        # payee -> the hosts that ADVERTISE it, not every host serving this
        # pair. AUDIT: attaching the pair's whole host set misattributed one
        # seller's broken payee to two innocent hosts that quote the same asset
        # -- a join-key error that sends someone to report a bug to the wrong
        # party.
        pay_to = row.get("pay_to")
        if isinstance(pay_to, str) and pay_to.strip():
            entry["payees"].setdefault(pay_to, set())
            if row.get("host"):
                entry["payees"][pay_to].add(row["host"])
        host = row.get("host")
        if host:
            entry["hosts"].add(host)
        if row.get("amount") not in (None, ""):
            entry["amounts"].append(row["amount"])
    return out


def implied_price(amount, decimals):
    """A quote -> its price in whole token units, or None.

    Reuses `upto_scheme.parse_ceiling` for the atomic-vs-human decision rather
    than re-deriving it. That rule is load-bearing (an integer is atomic; a
    decimal point means the seller quoted human units) and it is security
    relevant in that module, so the two must never drift apart.
    """
    if decimals is None:
        return None
    from upto_scheme import parse_ceiling
    atomic = parse_ceiling(amount, decimals)
    if atomic is None:
        return None
    try:
        return Decimal(atomic) / (Decimal(10) ** int(decimals))
    except (InvalidOperation, ArithmeticError, ValueError):
        return None


def assess(pairs, resolver):
    """Coverage + price sanity over the harvested pairs.

    `resolver(network, asset) -> decimals | None` is injected; production passes
    `payload_sim.known_decimals`, so the report reflects exactly what the engine
    would resolve, not a reimplementation of it.

    Returns {total_pairs, known_pairs, unresolved[], total_quotes,
             priced_quotes, implausible[], non_usd[]}.
    """
    unresolved, implausible, non_usd, malformed = [], [], [], []
    bad_payees = []
    known_pairs = priced = total_quotes = 0

    for key, entry in sorted(pairs.items()):
        row_base = {"network": entry["network"], "asset": entry["asset"],
                    "quotes": len(entry["amounts"]), "hosts": sorted(entry["hosts"])}
        for payee, payee_hosts in sorted((entry.get("payees") or {}).items()):
            if is_malformed_identifier(payee):
                bad_payees.append({"network": entry["network"], "pay_to": payee,
                                   "hosts": sorted(payee_hosts)})
        if is_malformed_identifier(entry["asset"]):
            total_quotes += len(entry["amounts"])
            malformed.append(row_base)
            continue
        decimals = resolver(entry["network"], entry["asset"])
        total_quotes += len(entry["amounts"])
        row = dict(row_base, decimals=decimals)
        if decimals is None:
            unresolved.append(row)
            continue
        known_pairs += 1

        prices = [p for p in (implied_price(a, decimals) for a in entry["amounts"])
                  if p is not None]
        priced += len(prices)
        if not prices:
            continue
        low, high = min(prices), max(prices)
        priced_row = dict(row, min_price=str(low), max_price=str(high))
        if key in NON_USD:
            non_usd.append(priced_row)
        elif high > MAX_PLAUSIBLE or low < MIN_PLAUSIBLE:
            implausible.append(priced_row)

    return {"total_pairs": len(pairs), "known_pairs": known_pairs,
            "unresolved": unresolved, "total_quotes": total_quotes,
            "priced_quotes": priced, "implausible": implausible,
            "non_usd": non_usd, "malformed": malformed,
            "malformed_payees": bad_payees}


def coverage_pct(report):
    """Share of (network, asset) pairs the table can scale, 0..100. 0 pairs -> 0.0
    rather than a ZeroDivisionError: an empty probe is a failed probe, and it must
    not read as perfect coverage."""
    total = report.get("total_pairs") or 0
    if total <= 0:
        return 0.0
    return 100.0 * report.get("known_pairs", 0) / total


def needs_attention(report):
    """True when a run found something a person should look at.

    The signal a schedule acts on: either an asset we cannot scale, or a price
    that does not make sense at the scale we used.
    """
    return bool(report.get("unresolved") or report.get("implausible")
                or report.get("malformed") or report.get("malformed_payees"))


def format_report(report):
    """The report as text, ordered by what a reader needs first."""
    lines = []
    lines.append("pairs: %d  covered: %d (%.1f%%)  quotes: %d  priced: %d"
                 % (report["total_pairs"], report["known_pairs"],
                    coverage_pct(report), report["total_quotes"],
                    report["priced_quotes"]))
    if report["unresolved"]:
        lines.append("")
        lines.append("UNRESOLVED -- no decimals for these; resolve and review "
                     "before adding to KNOWN_DECIMALS_BY_CHAIN:")
        for row in report["unresolved"]:
            lines.append("  %-46s %-46s quotes=%-4d %s"
                         % (row["network"][:46], row["asset"][:46], row["quotes"],
                            ",".join(row["hosts"][:3])))
    if report.get("malformed"):
        lines.append("")
        lines.append("MALFORMED ASSET -- not a valid identifier; a seller bug, "
                     "NOT a decimals-table gap:")
        for row in report["malformed"]:
            lines.append("  %-46s %-46s quotes=%-4d %s"
                         % (row["network"][:46], row["asset"][:46], row["quotes"],
                            ",".join(row["hosts"][:3])))
    if report.get("malformed_payees"):
        lines.append("")
        lines.append("MALFORMED PAYEE -- the address an agent would PAY is not a "
                     "valid identifier:")
        for row in report["malformed_payees"]:
            lines.append("  %-24s %-62s %s"
                         % (row["network"][:24], row["pay_to"][:62],
                            ",".join(row["hosts"][:2])))
    if report["implausible"]:
        lines.append("")
        lines.append("IMPLAUSIBLE PRICE -- the scale we used may be wrong:")
        for row in report["implausible"]:
            lines.append("  %-46s %-46s %s .. %s"
                         % (row["network"][:46], row["asset"][:46],
                            row["min_price"], row["max_price"]))
    if report["non_usd"]:
        lines.append("")
        lines.append("NON-USD (reported, not price-checked):")
        for row in report["non_usd"]:
            lines.append("  %-46s %-46s %s .. %s"
                         % (row["network"][:46], row["asset"][:46],
                            row["min_price"], row["max_price"]))
    if not needs_attention(report):
        lines.append("")
        lines.append("nothing to do: every live quote resolves at a plausible price.")
    return "\n".join(lines)


def targets_from_directory(directory):
    """(host, url) pairs from a `data/liveness.json`-shaped list."""
    out = []
    for row in directory or ():
        if isinstance(row, dict) and row.get("url"):
            out.append((row.get("host") or "", row["url"]))
    return out


# ===========================================================================
# Network (injected everywhere above)
# ===========================================================================
def _open(url, data=None, timeout=TIMEOUT):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data is not None else "GET")
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_accepts(url, timeout=TIMEOUT):
    """The accepts[] a live endpoint advertises, or [].

    GET then POST, because a 405 is a POST-only endpoint rather than a dead one --
    a distinction that cost 14 hosts when `directory_liveness` was first run by
    hand. Reads all three challenge carriers through `x402_challenge`, so a
    seller using the header form is not silently invisible here.
    """
    from x402_challenge import accepts_from_http_error, parse_challenge
    for data in (None, b"{}"):
        try:
            with _open(url, data, timeout) as response:
                accepts, _ = parse_challenge(response.read(MAX_BODY), response.headers)
            if accepts:
                return accepts
        except urllib.error.HTTPError as error:
            accepts, _ = accepts_from_http_error(error)
            if accepts:
                return accepts
        except Exception:
            pass
    return []


def harvest(targets, fetch=None, workers=10):
    """(host, url) pairs -> accept rows, via `fetch(url) -> accepts[]`."""
    fetch = fetch or fetch_accepts
    from x402 import _req_amount
    rows = []
    urls = [url for _, url in targets]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (host, _), accepts in zip(targets, pool.map(fetch, urls)):
            for accept in accepts or ():
                if not isinstance(accept, dict):
                    continue
                rows.append({"host": host,
                             "network": accept.get("network"),
                             "asset": accept.get("asset"),
                             "pay_to": accept.get("payTo"),
                             "amount": _req_amount(accept)})
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("directory", help="a data/liveness.json-shaped file")
    parser.add_argument("--json", help="also write the report as JSON here")
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args(argv)

    with open(args.directory) as handle:
        targets = targets_from_directory(json.load(handle))
    rows = harvest(targets, workers=args.workers)

    from payload_sim import known_decimals
    report = assess(pairs_from_accepts(rows),
                    lambda net, asset: known_decimals({"asset": asset, "chain": net}))
    print(format_report(report))
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(report, handle, indent=1, sort_keys=True)
    # Exit 1 when a person should look, so a scheduled run is actionable without
    # anyone reading the output.
    return 1 if needs_attention(report) else 0


if __name__ == "__main__":
    sys.exit(main())
