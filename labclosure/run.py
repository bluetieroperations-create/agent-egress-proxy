"""End-to-end: state corpus -> candidates -> national verification -> list."""
import json, sys, datetime as dt, urllib.request, time
from lab_signal import (build_labs, find_dark_labs, drop_still_funded,
                        dedupe_shared_grants, is_desk_science)

URL = "https://api.reporter.nih.gov/v2/projects/search"

def post(body):
    for i in range(4):
        try:
            r = urllib.request.Request(URL, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json",
                         "User-Agent": "labclosure-research/0.1 (public-data spike)"})
            with urllib.request.urlopen(r, timeout=120) as f:
                return json.loads(f.read())
        except Exception:
            if i == 3: raise
            time.sleep(2 ** i)

def national_awards(pids, batch=25):
    out = []
    for i in range(0, len(pids), batch):
        chunk = pids[i:i + batch]
        d = post({"criteria": {"pi_profile_ids": chunk,
                               "fiscal_years": [2025, 2026, 2027]},
                  "include_fields": ["PrincipalInvestigators", "Organization",
                                     "BudgetEnd", "ProjectEndDate"],
                  "offset": 0, "limit": 500})
        out.extend(d.get("results", []))
        time.sleep(0.25)
    return out

if __name__ == "__main__":
    state = sys.argv[1] if len(sys.argv) > 1 else "MI"
    today = dt.date(2026, 8, 25)
    rows = json.load(open(f"reporter_{state}.json"))

    labs = build_labs(rows)
    cands = find_dark_labs(labs, today)
    print(f"1. state corpus            : {len(rows):,} awards / {len(labs):,} PIs")
    print(f"2. dark, right window, bench: {len(cands)}")

    verified = drop_still_funded(cands, national_awards([p for p, _ in cands]), today)
    print(f"3. after national check     : {len(verified)}  "
          f"(-{len(cands)-len(verified)})")

    final = dedupe_shared_grants(verified)
    print(f"4. after co-PI dedupe       : {len(final)}  (-{len(verified)-len(final)})")

    print(f"\n{'PI':<26} {'institution':<32} {'NIH total':>11}  dark since")
    print("-" * 84)
    for pid, L in final[:15]:
        print(f"{(L['name'] or '')[:25]:<26} {(L['org'] or '')[:31]:<32} "
              f"${L['total']:>10,}  {L['last_end']}")
    tot = sum(L["total"] for _, L in final)
    print(f"\n{len(final)} labs · ${tot:,} in ended support")
