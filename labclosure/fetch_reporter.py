"""Pull NIH RePORTER projects for a state across fiscal years. stdlib only."""
import json, sys, time, urllib.request, urllib.error

URL = "https://api.reporter.nih.gov/v2/projects/search"
FIELDS = ["ProjectNum","CoreProjectNum","ApplId","FiscalYear","AwardAmount",
          "ProjectStartDate","ProjectEndDate","BudgetStart","BudgetEnd",
          "IsActive","ActivityCode","AwardType","ContactPiName",
          "PrincipalInvestigators","Organization","SubprojectId","AgencyIcAdmin","DateAdded","AwardNoticeDate"]

def post(body, tries=4):
    data = json.dumps(body).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(URL, data=data, headers={
                "Content-Type": "application/json", "Accept": "application/json",
                "User-Agent": "labclosure-research/0.1 (public-data spike)"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(2 ** i); continue
            raise
        except Exception:
            if i < tries - 1:
                time.sleep(2 ** i); continue
            raise

def fetch_state(state, years, limit=500):
    out, seen = [], set()
    for fy in years:
        offset, total = 0, None
        while True:
            body = {"criteria": {"fiscal_years": [fy], "org_states": [state]},
                    "include_fields": FIELDS, "offset": offset, "limit": limit,
                    "sort_field": "project_start_date", "sort_order": "asc"}
            d = post(body)
            if total is None:
                total = d["meta"]["total"]
                print(f"  FY{fy}: {total} projects", file=sys.stderr)
            res = d.get("results", [])
            if not res: break
            for r in res:
                k = r.get("appl_id")
                if k not in seen:
                    seen.add(k); out.append(r)
            offset += len(res)
            if offset >= total or offset >= 14500: break
            time.sleep(0.25)
    return out

if __name__ == "__main__":
    state = sys.argv[1] if len(sys.argv) > 1 else "MI"
    years = list(range(2019, 2027))
    print(f"fetching {state} FY{years[0]}-{years[-1]}", file=sys.stderr)
    rows = fetch_state(state, years)
    path = f"reporter_{state}.json"
    with open(path, "w") as f:
        json.dump(rows, f)
    print(f"wrote {len(rows)} rows -> {path}", file=sys.stderr)
