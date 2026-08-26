"""Is a timely institution-level feed worth anything, given BRIMR publishes
free ANNUAL rankings? Test: how early in a fiscal year can you call the
direction of an institution's annual funding change?"""
import json, collections, datetime as dt
from lab_signal import to_date

rows = json.load(open("reporter_MI.json"))

def inst(r):
    return (r.get("organization") or {}).get("org_name") or "?"

# NIH fiscal year starts Oct 1
def fy_month(d, fy):
    start = dt.date(fy - 1, 10, 1)
    return (d.year - start.year) * 12 + (d.month - start.month) + 1

# annual totals per institution
ann = collections.defaultdict(lambda: collections.defaultdict(int))
for r in rows:
    ann[inst(r)][r["fiscal_year"]] += r.get("award_amount") or 0

big = sorted(ann.items(), key=lambda kv: -sum(kv[1].values()))[:8]
print("ANNUAL NIH FUNDING BY INSTITUTION ($M)")
print(f"{'institution':<38} " + " ".join(f"{y:>7}" for y in range(2021, 2027)))
print("-" * 86)
for name, ys in big:
    cells = " ".join(f"{ys.get(y,0)/1e6:>7.1f}" for y in range(2021, 2027))
    print(f"{name[:37]:<38} {cells}")

# YoY change 2025 -> 2026 (2026 partial!)
print("\nFY2025 -> FY2026 change (FY2026 incomplete -- that is the point):")
for name, ys in big[:6]:
    a, b = ys.get(2025, 0), ys.get(2026, 0)
    if a:
        print(f"  {name[:36]:<37} {a/1e6:>7.1f} -> {b/1e6:>6.1f}  ({(b-a)/a:>+6.1%})")

# THE TIMELINESS TEST -------------------------------------------------
# Using date_added, reconstruct what was knowable at month M of each FY,
# and ask: does the partial-year signal predict the full-year direction?
print("\n" + "="*86)
print("TIMELINESS: can a partial year call the annual direction?")
print("="*86)

def known_by(rows, cutoff):
    out = collections.defaultdict(lambda: collections.defaultdict(int))
    for r in rows:
        da = to_date(r.get("date_added"))
        if da and da <= cutoff:
            out[inst(r)][r["fiscal_year"]] += r.get("award_amount") or 0
    return out

INSTS = [n for n, _ in big[:12]]
print(f"\n{'month of FY':>12} {'direction calls':>16} {'correct':>9} {'accuracy':>9}")
for m in (3, 4, 5, 6, 7, 8, 9, 12):
    ok = tot = 0
    for fy in (2022, 2023, 2024, 2025):
        cutoff = dt.date(fy - 1, 10, 1) + dt.timedelta(days=30 * m)
        part = known_by(rows, cutoff)
        for name in INSTS:
            prev_full = ann[name].get(fy - 1, 0)
            curr_full = ann[name].get(fy, 0)
            if not prev_full or not curr_full:
                continue
            # partial-year run rate vs same point last year
            p_now = part[name].get(fy, 0)
            cutoff_prev = dt.date(fy - 2, 10, 1) + dt.timedelta(days=30 * m)
            p_prev = known_by(rows, cutoff_prev)[name].get(fy - 1, 0)
            if not p_prev:
                continue
            pred_up = p_now > p_prev
            true_up = curr_full > prev_full
            tot += 1
            ok += (pred_up == true_up)
    if tot:
        print(f"{m:>12} {tot:>16} {ok:>9} {ok/tot:>8.1%}")
