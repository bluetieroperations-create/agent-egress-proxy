"""Liveness + challenge-format survey of the x402 payee directory.

WHY THIS EXISTS
---------------
`data/directory.json` is the ecosystem map `ecosystem_scan.py` derives from the
Bazaar crawl: 198 payees ranked by an explainable trust score. But that map is a
snapshot of what was ADVERTISED, and an advertised endpoint is not a reachable
one. Before treating the directory as a lead list -- or as evidence that the
engine has anything to score -- someone has to ask each host whether it is still
serving a 402 today.

This module does that, and reports the answer in the terms that actually matter
to Blackwall: not "is it up" but "can we PARSE its payment requirements", because
`forecast` scores a payment from the challenge's `accepts[]`. An endpoint that is
perfectly alive but advertises its requirements somewhere we do not read is an
endpoint we cannot score.

TWO MEASUREMENT ARTIFACTS THIS DELIBERATELY AVOIDS
--------------------------------------------------
Both were real errors in the first two passes of this survey, each of which
UNDERCOUNTED the live ecosystem:

1. GET-only probing. A 405 is not a dead endpoint, it is a POST endpoint. Retrying
   the non-scoreable hosts with POST recovered 14 of them -- including two with
   50+ distinct payers. `probe_host` therefore falls back to POST.

2. Body-only challenge parsing. The x402 v2 style carries the requirements in a
   `WWW-Authenticate: X402 requirements="<base64 json>"` header. Reading only the
   JSON body classified those hosts as "402 with no accepts[]" -- indistinguishable,
   in the output, from a genuinely broken endpoint. `parse_challenge` reads both.

PARSER GAP THIS SURVEY FOUND
----------------------------
Nothing in this repo reads `WWW-Authenticate` -- `grep -rin www-authenticate`
returns zero hits. Every consumer takes `accepts` from the JSON body:
`clients/x402_pay.py`, `discovery_crawl.py`, `readiness.py`, `traceipt_attest.py`,
`blackwall.py`, and the OpenClaw hook's 402-challenge recognizer. So a v2
header-style endpoint is invisible to the crawler and unpayable by the client,
even though the engine would score it perfectly if handed the requirements.

Measured impact is small but not zero: 2 of 195 hosts, one of which
(blockrun.ai) is 2nd on the lead list by distinct payers. Recorded here rather
than fixed silently, because the fix belongs in those consumers, not in a survey.

CLASSES
-------
  body_accepts -> {"x402Version":..,"accepts":[..]} in the body. Scoreable today.
  hdr_accepts  -> WWW-Authenticate: X402 requirements="<b64>". Scoreable ONLY if
                  the caller reads the header (see PARSER GAP in the module docs).
  wellknown    -> no inline challenge, but /.well-known/x402.json serves a catalog.
  opaque_402   -> 402 with no machine-readable requirements anywhere. Not scoreable.
  other        -> answers, but not a 402 (route moved, keyed, or retired).
  dead         -> DNS failure / refused / TLS failure / timeout.
  blocked      -> egress policy refused us. NOT a fact about the endpoint.

Pure helpers (`parse_challenge`, `classify`, `is_managed_host`, `rank_leads`) are
unit-tested; the network half is injected.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import socket
import ssl
import urllib.error
import urllib.request

import x402_challenge

TIMEOUT = 12
USER_AGENT = "blackwall-liveness/1.0 (+x402 directory check)"

#: Re-exported from x402_challenge so this module keeps its public names while
#: there is exactly ONE parser in the repo -- the survey, the crawler and the
#: paying client must agree on what "readable requirements" means.
BODY_ACCEPTS = x402_challenge.BODY_ACCEPTS
HDR_ACCEPTS = x402_challenge.HDR_ACCEPTS
PAYMENT_REQUIRED = x402_challenge.PAYMENT_REQUIRED
WELLKNOWN = "wellknown"
OPAQUE_402 = "opaque_402"
OTHER = "other"
DEAD = "dead"
BLOCKED = "blocked"

#: Classes whose payment requirements Blackwall can actually read and score.
SCOREABLE = (BODY_ACCEPTS, HDR_ACCEPTS, PAYMENT_REQUIRED)

#: Free hosting subdomains. A payee on one of these is far more likely to be a
#: weekend demo than a business, so `rank_leads` filters them out of a BD list.
#: This is a HEURISTIC for prioritisation only -- it never affects a verdict.
MANAGED_HOST_MARKERS = (
    "workers.dev", "vercel.app", "onrender.com", "up.railway.app", "zeabur.app",
    "netlify.app", "hf.space", "a.run.app", "ngrok-free.dev", "duckdns.org",
    "lambda-url", "on.aws", "hstgr.cloud", "fly.dev", "herokuapp.com",
)

def _accepts_of(doc):
    """Deprecated alias -- see x402_challenge.accepts_of."""
    return x402_challenge.accepts_of(doc)


def parse_challenge(body, headers):
    """Extract accepts[] from a 402 response, from EITHER carrier.

    Delegates to x402_challenge, which is the single implementation shared with
    discovery_crawl and clients/x402_pay. Kept here as a name because the survey
    and its tests were written against it.
    """
    return x402_challenge.parse_challenge(body, headers)


def is_managed_host(host):
    """True if `host` sits on a free/managed hosting subdomain. Prioritisation only."""
    host = (host or "").lower()
    return any(marker in host for marker in MANAGED_HOST_MARKERS)


def summarize_accepts(accepts):
    """One-line human summary of the first payment option."""
    first = (accepts or [{}])[0]
    if not isinstance(first, dict):
        return "?"
    amount = first.get("maxAmountRequired") or first.get("amount") or "?"
    return f"{first.get('network', '?')} {amount}"


def classify(status, body, headers, wellknown_ok=False):
    """Map one probe response to a class. Pure -- the network half is the caller's."""
    if status == 402:
        accepts, carrier = parse_challenge(body, headers)
        if accepts:
            return carrier, summarize_accepts(accepts)
        if wellknown_ok:
            return WELLKNOWN, "/.well-known/x402.json"
        return OPAQUE_402, ""
    return OTHER, str(status)


def rank_leads(results, min_payers=8):
    """Rank the results that are worth a business conversation.

    A lead needs all three: a challenge we can parse, enough distinct on-chain
    payers to be real traffic rather than the operator testing their own endpoint,
    and its own domain. Sorted by distinct payers -- the hardest signal to fake.
    """
    leads = [
        r for r in results
        if r.get("class") in SCOREABLE
        and r.get("distinct_payers", 0) >= min_payers
        and not is_managed_host(r.get("host", ""))
    ]
    leads.sort(key=lambda r: (-r.get("distinct_payers", 0), r.get("host", "")))
    return leads


def targets_from_directory(directory):
    """One probe target per HOST -- payees advertise dozens of routes on one host."""
    seen, targets = set(), []
    for rec in directory:
        resources = rec.get("resources") or []
        if not resources:
            continue
        url = resources[0]
        try:
            host = url.split("/")[2]
        except IndexError:
            continue
        if host in seen:
            continue
        seen.add(host)
        targets.append((rec, url, host))
    return targets


# --------------------------------------------------------------------------
# Network half. Isolated below this line; everything above is pure.
# --------------------------------------------------------------------------

def _open(url, data=None, timeout=TIMEOUT):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data is not None else "GET")
    return urllib.request.urlopen(request, timeout=timeout)


def _has_wellknown(host, timeout=TIMEOUT):
    try:
        with _open(f"https://{host}/.well-known/x402.json", timeout=timeout) as response:
            doc = json.loads(response.read(200_000).decode("utf-8", "replace"))
    except Exception:
        return False
    if isinstance(doc, dict):
        return bool(doc.get("accepts") or doc.get("resources")
                    or doc.get("items") or doc.get("x402Version"))
    return bool(isinstance(doc, list) and doc)


def probe_host(url, host, timeout=TIMEOUT):
    """Probe one host: GET, then POST if GET did not yield a parseable challenge.

    Returns (class, note). Never raises -- a survey that dies on one bad TLS
    handshake is useless.
    """
    best = None
    for data in (None, b"{}"):
        try:
            with _open(url, data=data, timeout=timeout) as response:
                result = (OTHER, str(response.status))
        except urllib.error.HTTPError as error:
            try:
                body = error.read(60_000)
            except Exception:
                body = b""
            if error.code in (403, 407) and b"agentproxy" in body.lower():
                return BLOCKED, "egress policy"
            wellknown_ok = error.code == 402 and _has_wellknown(host, timeout)
            result = classify(error.code, body, error.headers, wellknown_ok)
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError) as error:
            result = (DEAD, str(getattr(error, "reason", error))[:60])
        except Exception as error:
            result = (DEAD, type(error).__name__)

        if result[0] in SCOREABLE:
            return result
        # Keep the most informative non-scoreable answer across both methods.
        if best is None or _rank(result[0]) < _rank(best[0]):
            best = result
    return best


_PRIORITY = {BODY_ACCEPTS: 0, HDR_ACCEPTS: 1, WELLKNOWN: 2,
             OPAQUE_402: 3, OTHER: 4, BLOCKED: 5, DEAD: 6}


def _rank(cls):
    return _PRIORITY.get(cls, 99)


def survey(directory, workers=8, timeout=TIMEOUT):
    """Probe every distinct host in `directory`. Returns a list of result dicts."""
    targets = targets_from_directory(directory)

    def run(target):
        rec, url, host = target
        cls, note = probe_host(url, host, timeout)
        return {
            "host": host, "url": url, "class": cls, "note": note,
            "payee": rec.get("payee"),
            "distinct_payers": rec.get("distinct_payers", 0),
            "settlements": rec.get("settlement_count", 0),
            "resources": rec.get("resource_count", 0),
            "trust_score": rec.get("trust_score", 0),
        }

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(run, targets))


def main(argv=None):
    import argparse
    import collections

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", nargs="?", default="data/directory.json")
    parser.add_argument("--out", default="data/liveness.json")
    parser.add_argument("--min-payers", type=int, default=8)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)

    with open(args.directory) as handle:
        directory = json.load(handle)

    results = survey(directory, workers=args.workers)
    counts = collections.Counter(r["class"] for r in results)

    print(f"{len(directory)} payees -> {len(results)} distinct hosts probed\n")
    for cls in sorted(counts, key=_rank):
        print(f"  {cls:14s} {counts[cls]:4d}")

    scoreable = [r for r in results if r["class"] in SCOREABLE]
    print(f"\nscoreable today: {len(scoreable)} hosts, "
          f"{sum(r['resources'] for r in scoreable)} advertised resources")

    leads = rank_leads(results, args.min_payers)
    print(f"\n--- leads (scoreable, >={args.min_payers} distinct payers, own domain): {len(leads)} ---")
    for lead in leads:
        print(f"  {lead['distinct_payers']:4d}p {lead['settlements']:4d}s "
              f"{lead['resources']:4d}res  {lead['host']:38s} {lead['note']}")

    with open(args.out, "w") as handle:
        json.dump(results, handle, indent=1)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
