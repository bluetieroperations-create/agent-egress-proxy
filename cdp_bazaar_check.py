#!/usr/bin/env python3
"""
cdp_bazaar_check.py -- is our endpoint listed in the CDP x402 Bazaar yet?

Bazaar has no registration step and no published indexing SLA: CDP catalogs a
resource after its first successful CDP settle. This answers "are we in it?"

NO CREDENTIALS NEEDED. This script used to mint a CDP Bearer JWT and refuse to
run without `CDP_API_KEY_ID`/`CDP_API_KEY_SECRET` -- so checking the listing
required setting up credentials locally, and the check went unrun for that
reason alone. MEASURED 2026-09-15: `GET /discovery/resources` and
`/discovery/search` both answer 200 UNAUTHENTICATED. The catalog is public,
which makes sense -- it is a marketplace. Creds are still honoured if present,
because an authenticated read is no worse, but they are not required.

    python cdp_bazaar_check.py            # 0 = listed, 1 = not yet, 2 = inconclusive

THE SEARCH ENDPOINT CANNOT PROVE ABSENCE, and this is the trap worth knowing:
`?q=` IS honoured when there are matches (q=onesource -> 19 of 20 results
contain it) but on a MISS it silently returns 20 ARBITRARY entries with
`partialResults: true`. So a miss looks like a page of unrelated sellers, and a
loose substring needle could match one of them and report us LISTED when we are
not. Search is therefore used only to CONFIRM a hit; absence is always settled
by the full paginated scan.

PAGINATION IS OFFSET-BASED and the response states `pagination.total`
(15,572 entries on 2026-09-15), so the scan knows when it is genuinely done
instead of inferring the end from a short page.
"""
import json
import os
import urllib.error
import urllib.request

from cdp_auth import build_cdp_jwt
from creds_local import load_creds
from x402 import CDP_FACILITATOR_URL
import user_agent as ua_policy

try:
    load_creds()  # optional: ~/.blackwall-creds, if the operator keeps one
except Exception:
    pass
KEY_ID = os.environ.get("CDP_API_KEY_ID")
SECRET = os.environ.get("CDP_API_KEY_SECRET")
# What identifies OUR endpoint in the catalog (host, and the resource slugs the
# first-call scripts use). Case-insensitive substring match on each entry's JSON.
#: Identifiers that can ONLY be ours. The bare product name was here and is
#: deliberately gone: matching is a substring test over each entry's whole JSON,
#: and the search endpoint returns 20 UNRELATED entries on a miss -- so a generic
#: needle could match somebody else's description and report us LISTED when we
#: are absent, which is a false positive on the one question being asked. A host
#: and the payout address cannot belong to anyone else.
NEEDLES = [n.lower() for n in (
    "blackwall-free.onrender.com",
    "agent-egress-proxy.onrender.com",  # legacy host: a stale entry still counts
    os.environ.get("BLACKWALL_PAY_TO")
    or "0x9B901C313b846B415c76963F8FFDf62B7A83521d",
)]


def _get(path):
    url = CDP_FACILITATOR_URL + path
    headers = {"Accept": "application/json",
               "User-Agent": ua_policy.browser("bazaar-check")}
    if KEY_ID and SECRET:
        # Honoured when present, never required -- the catalog is public.
        headers["Authorization"] = "Bearer " + build_cdp_jwt(
            KEY_ID, SECRET, "GET", url)
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "REQUEST FAILED: %s" % e


def main():
    """0 = listed, 1 = not yet listed, 2 = inconclusive / unreachable.

    Real exit codes because every outcome used to exit 0, so a scheduled run
    could not tell "we are listed" from "we are not" from "the scan broke" --
    the same actionability rule `asset_coverage` and `billing_preflight` follow.
    """
    # Fast path: the discovery SEARCH endpoint (query, not a full scan). If it
    # answers, it's definitive and cheap; otherwise fall back to paginating the
    # whole catalog below.
    from urllib.parse import quote
    s_status, s_body = _get("/discovery/search?q=%s" % quote("blackwall-free.onrender.com"))
    if s_status == 200 and isinstance(s_body, dict):
        hits = s_body.get("items") or s_body.get("resources") or s_body.get("data") or []
        matched = [h for h in hits if any(n in json.dumps(h).lower() for n in NEEDLES)]
        if matched:
            print("discovery/search -> LISTED:")
            for h in matched:
                print("   -", json.dumps(h.get("resource") or h.get("url") or h)[:200])
            return 0
        print("discovery/search reachable, no match yet; confirming via full scan...\n")

    found, scanned = [], 0
    offset, limit, pages = 0, 100, 0
    PAGE_CAP = 1000  # up to 100k entries -- effectively "the whole catalog"
    reached_end = False
    total = None
    while pages < PAGE_CAP:
        status, body = _get("/discovery/resources?limit=%d&offset=%d" % (limit, offset))
        if status != 200 or not isinstance(body, dict):
            print("discovery/resources -> HTTP %s" % status)
            print(str(body)[:400])
            break
        # The API states the catalog size, so completeness is a FACT rather
        # than an inference from a short final page.
        if total is None:
            total = (body.get("pagination") or {}).get("total")
        items = body.get("items") or body.get("resources") or body.get("data") or []
        if not items:
            reached_end = True
            break
        for it in items:
            scanned += 1
            blob = json.dumps(it).lower()
            if any(n in blob for n in NEEDLES):
                found.append(it)
        offset += len(items)
        pages += 1
        if total and offset >= int(total):
            reached_end = True
            break
        if len(items) < limit:
            reached_end = True
            break

    print("Scanned %d%s catalog entries%s." % (
        scanned, " of %s" % total if total else "",
        "" if reached_end else " (STOPPED AT CAP -- catalog is larger)"))
    if found:
        print("\n==> LISTED. Our endpoint is in the Bazaar catalog:\n")
        for it in found:
            res = it.get("resource") or it.get("url") or it.get("name") or it
            print("   -", json.dumps(res)[:200])
        return 0
    elif not reached_end:
        print("\n==> INCONCLUSIVE: scanned to the cap without reaching the end of")
        print("    the catalog, so a 'not found' here is not trustworthy.")
        return 2
    else:
        print("\n==> NOT YET in the catalog (scanned the FULL catalog).")
        print("    Async indexing can lag the settle by minutes-to-a-day.")
        print("    STATE AS OF 2026-09-15: the relative-`resource` hypothesis was")
        print("    FIXED AND DEPLOYED -- the live 402 now advertises an absolute")
        print("    url (verified in production), so a first CDP settlement plus an")
        print("    absolute resource is already in place and this is now a waiting")
        print("    game, not a known defect.")
        print("    IF STILL ABSENT AFTER ~24-48h, the next candidate is")
        print("    extensions.bazaar.info: present in 2000/2000 catalogued entries")
        print("    while we emit only `schema`. It was left out DELIBERATELY so")
        print("    that a listing appearing would tell us which change mattered.")
        print("    See docs/BAZAAR_LISTING.md.")
        return 1


if __name__ == "__main__":
    # raise, not call: a `return 2` that nobody propagates exits 0, which is how
    # cdp_verify_probe reported "I declined to probe" as success.
    raise SystemExit(main())
