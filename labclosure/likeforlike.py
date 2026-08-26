"""Apples-to-apples: compare each FY as known at the SAME point in its cycle,
using date_added. Naively comparing a partial FY2026 to a complete FY2025
manufactures a fake collapse -- this is the calculation that separates real
contraction from posting lag."""
import json, collections, datetime as dt
from lab_signal import to_date

rows = json.load(open("reporter_MI.json"))
def inst(r): return (r.get("organization") or {}).get("org_name") or "?"

def as_of(fy, cutoff):
    """Funding for fiscal year `fy` as it was KNOWN on `cutoff`."""
    out = collections.defaultdict(int)
    for r in rows:
        if r["fiscal_year"] != fy: continue
        da = to_date(r.get("date_added"))
        if da and da <= cutoff:
            out[inst(r)] += r.get("award_amount") or 0
    return out

# FY2026 as of 2026-08-25 (11 months in) vs FY2025 as of 2025-08-25 (same point)
cur = as_of(2026, dt.date(2026, 8, 25))
prv = as_of(2025, dt.date(2025, 8, 25))
# and the naive (wrong) comparison for contrast
full_prev = collections.defaultdict(int)
for r in rows:
    if r["fiscal_year"] == 2025:
        full_prev[inst(r)] += r.get("award_amount") or 0

names = sorted(set(cur) | set(prv), key=lambda n: -prv.get(n, 0))[:8]
print("LIKE-FOR-LIKE vs NAIVE, FY2026 to date")
print(f"{'institution':<36} {'FY25@Aug':>10} {'FY26@Aug':>10} {'REAL':>8}   {'naive':>8}")
print("-" * 80)
for n in names:
    a, b, f = prv.get(n, 0), cur.get(n, 0), full_prev.get(n, 0)
    if a < 1e6: continue
    real = (b - a) / a
    naive = (b - f) / f if f else 0
    print(f"{n[:35]:<36} {a/1e6:>9.1f} {b/1e6:>9.1f} {real:>+8.1%}   {naive:>+8.1%}")

tot_a, tot_b = sum(prv.values()), sum(cur.values())
tot_f = sum(full_prev.values())
print("-" * 80)
print(f"{'MICHIGAN TOTAL':<36} {tot_a/1e6:>9.1f} {tot_b/1e6:>9.1f} "
      f"{(tot_b-tot_a)/tot_a:>+8.1%}   {(tot_b-tot_f)/tot_f:>+8.1%}")

print(f"\nThe naive read overstates the decline by "
      f"{abs((tot_b-tot_f)/tot_f) - abs((tot_b-tot_a)/tot_a):.1%} points of Michigan-wide funding.")

# how stable is the like-for-like measure across prior years?
print("\nsanity: same-point YoY for earlier years (should look ordinary)")
for fy in (2022, 2023, 2024, 2025, 2026):
    c = as_of(fy, dt.date(fy, 8, 25)); p = as_of(fy-1, dt.date(fy-1, 8, 25))
    if sum(p.values()):
        print(f"  FY{fy} vs FY{fy-1} at same point: {(sum(c.values())-sum(p.values()))/sum(p.values()):>+7.1%}")
