#!/usr/bin/env python3
"""
cdp_bazaar_check.py -- is our endpoint listed in the CDP x402 Bazaar yet?

Bazaar has no separate registration and no published indexing SLA: CDP catalogs a
resource after its first successful CDP settle (done). This queries the live
discovery catalog (GET /v2/x402/discovery/resources) and tells you whether our
endpoint is in it yet -- turning "when does it go live?" into "run this to check."

    $env:CDP_API_KEY_ID     = '<uuid>'
    $env:CDP_API_KEY_SECRET = '<base64 ed25519 secret>'
    python cdp_bazaar_check.py
    Remove-Item Env:CDP_API_KEY_SECRET

Read-only, money-free. Prints no secret.
"""
import json
import os
import urllib.error
import urllib.request

from cdp_auth import build_cdp_jwt
from creds_local import load_creds
from x402 import CDP_FACILITATOR_URL

load_creds()  # auto-load ~/.blackwall-creds so setting env vars by hand is optional
KEY_ID = os.environ.get("CDP_API_KEY_ID")
SECRET = os.environ.get("CDP_API_KEY_SECRET")
# What identifies OUR endpoint in the catalog (host, and the resource slugs the
# first-call scripts use). Case-insensitive substring match on each entry's JSON.
NEEDLES = [n.lower() for n in (
    "agent-egress-proxy.onrender.com",
    "bazaar-first-settled-call",
    "blackwall",
)]


def _get(path):
    url = CDP_FACILITATOR_URL + path
    token = build_cdp_jwt(KEY_ID, SECRET, "GET", url)
    req = urllib.request.Request(
        url, method="GET",
        headers={"Accept": "application/json",
                 "Authorization": "Bearer " + token,
                 "User-Agent": "Mozilla/5.0 (Blackwall bazaar check)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "REQUEST FAILED: %s" % e


def main():
    if not (KEY_ID and SECRET):
        print("Set CDP_API_KEY_ID and CDP_API_KEY_SECRET first.")
        raise SystemExit(2)

    # Fast path: the discovery SEARCH endpoint (query, not a full scan). If it
    # answers, it's definitive and cheap; otherwise fall back to paginating the
    # whole catalog below.
    from urllib.parse import quote
    s_status, s_body = _get("/discovery/search?q=%s" % quote("agent-egress-proxy.onrender.com"))
    if s_status == 200 and isinstance(s_body, dict):
        hits = s_body.get("items") or s_body.get("resources") or s_body.get("data") or []
        matched = [h for h in hits if any(n in json.dumps(h).lower() for n in NEEDLES)]
        if matched:
            print("discovery/search -> LISTED:")
            for h in matched:
                print("   -", json.dumps(h.get("resource") or h.get("url") or h)[:200])
            return
        print("discovery/search reachable, no match yet; confirming via full scan...\n")

    found, scanned = [], 0
    offset, limit, pages = 0, 100, 0
    PAGE_CAP = 1000  # up to 100k entries -- effectively "the whole catalog"
    reached_end = False
    while pages < PAGE_CAP:
        status, body = _get("/discovery/resources?limit=%d&offset=%d" % (limit, offset))
        if status != 200 or not isinstance(body, dict):
            print("discovery/resources -> HTTP %s" % status)
            print(str(body)[:400])
            break
        items = body.get("items") or body.get("resources") or body.get("data") or []
        if not items:
            reached_end = True
            break
        for it in items:
            scanned += 1
            blob = json.dumps(it).lower()
            if any(n in blob for n in NEEDLES):
                found.append(it)
        if len(items) < limit:
            reached_end = True
            break
        offset += limit
        pages += 1

    print("Scanned %d catalog entries%s." % (
        scanned, "" if reached_end else " (STOPPED AT CAP -- catalog is larger)"))
    if found:
        print("\n==> LISTED. Our endpoint is in the Bazaar catalog:\n")
        for it in found:
            res = it.get("resource") or it.get("url") or it.get("name") or it
            print("   -", json.dumps(res)[:200])
    elif not reached_end:
        print("\n==> INCONCLUSIVE: scanned to the cap without reaching the end of")
        print("    the catalog, so a 'not found' here is not trustworthy. Tell")
        print("    Claude -- we need the discovery/search endpoint, not a full scan.")
    else:
        print("\n==> NOT YET in the catalog (scanned the FULL catalog).")
        print("    Async indexing can lag the settle by minutes-to-a-day; re-run")
        print("    later. If still absent after ~24-48h, the likely cause is the")
        print("    settle envelope not carrying a `resource` id CDP can index (v2")
        print("    moved resource out of paymentRequirements) -- tell Claude.")


if __name__ == "__main__":
    main()
