"""Sweep NSF awards for a state that are still active. stdlib only."""
import json, sys, time, urllib.request, urllib.parse

BASE = "https://api.nsf.gov/services/v1/awards.json"
KEEP = ("id","piFirstName","piLastName","pdPIName","awardeeName","perfLocation",
        "startDate","expDate","estimatedTotalAmt","fundsObligatedAmt","activeAwd","title")

def get(params, tries=4):
    url = BASE + "?" + urllib.parse.urlencode(params)
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept":"application/json",
                "User-Agent":"labclosure-research/0.1 (public-data spike)"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except Exception:
            if i == tries-1: raise
            time.sleep(2**i)

def sweep(state, exp_after, rpp=25, cap=8000):
    out, offset = [], 1
    while offset <= cap:
        d = get({"awardeeStateCode": state, "expDateStart": exp_after,
                 "rpp": rpp, "offset": offset})
        aw = d.get("response", {}).get("award", [])
        if not aw: break
        for a in aw:
            out.append({k: a.get(k) for k in KEEP})
        offset += len(aw)
        if len(aw) < rpp: break
        if offset % 500 == 1:
            print(f"  ...{offset}", file=sys.stderr)
        time.sleep(0.2)
    return out

if __name__ == "__main__":
    state = sys.argv[1] if len(sys.argv) > 1 else "MI"
    after = sys.argv[2] if len(sys.argv) > 2 else "08/25/2026"
    print(f"NSF sweep {state}, expiring after {after}", file=sys.stderr)
    rows = sweep(state, after)
    json.dump(rows, open(f"nsf_{state}.json","w"))
    print(f"wrote {len(rows)} active NSF awards -> nsf_{state}.json", file=sys.stderr)
