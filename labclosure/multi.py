"""Like-for-like YoY across states. Same-point comparison via date_added."""
import json, glob, collections, datetime as dt, os
from lab_signal import to_date

def series(path):
    rows = json.load(open(path))
    def as_of(fy, cutoff):
        t = 0
        for r in rows:
            if r["fiscal_year"] != fy: continue
            da = to_date(r.get("date_added"))
            if da and da <= cutoff: t += r.get("award_amount") or 0
        return t
    out = {}
    for fy in range(2021, 2027):
        c = as_of(fy, dt.date(fy, 8, 25))
        p = as_of(fy-1, dt.date(fy-1, 8, 25))
        out[fy] = ((c - p) / p) if p else None
    naive_prev = sum(r.get("award_amount") or 0 for r in rows if r["fiscal_year"] == 2025)
    naive_cur  = sum(r.get("award_amount") or 0 for r in rows if r["fiscal_year"] == 2026)
    return out, (naive_cur - naive_prev)/naive_prev if naive_prev else None, len(rows)

print(f"{'state':<7} {'rows':>7} " + " ".join(f"FY{y%100:>5}" for y in range(2022,2027)) + f" {'naive FY26':>11}")
print("-" * 66)
for path in sorted(glob.glob("reporter_*.json")):
    st = os.path.basename(path).split("_")[1].split(".")[0]
    s, naive, n = series(path)
    cells = " ".join(f"{(s[y]*100):>6.1f}" if s.get(y) is not None else "     -" for y in range(2022,2027))
    print(f"{st:<7} {n:>7,} {cells} {naive*100:>10.1f}%")
print("\n(columns are LIKE-FOR-LIKE same-point YoY %; last column is the naive"
      "\n partial-vs-complete comparison an unaware builder would publish)")
