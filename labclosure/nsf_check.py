"""How much of the NIH blind spot does NSF close?
Name-matching only -- NSF has no crosswalk to NIH's profile_id, so this is
softer than the NIH-side national check and both bounds are reported."""
import json, datetime as dt, re, collections
from lab_signal import build_labs, find_dark_labs, drop_still_funded, dedupe_shared_grants
from run import national_awards

TODAY = dt.date(2026, 8, 25)

def norm(s):
    return re.sub(r"[^a-z]", "", (s or "").lower())

def split_nih(full):
    parts = [p for p in (full or "").split() if p]
    if len(parts) < 2: return None, None
    return norm(parts[0]), norm(parts[-1])   # first, last

# --- rebuild the verified list ---
rows = json.load(open("reporter_MI.json"))
labs = build_labs(rows)
cands = find_dark_labs(labs, TODAY)
verified = drop_still_funded(cands, national_awards([p for p, _ in cands]), TODAY)
final = dedupe_shared_grants(verified)
print(f"NIH-flagged dark labs (MI): {len(final)}")

# --- index active NSF PIs ---
nsf = json.load(open("nsf_MI.json"))
by_last = collections.defaultdict(list)
for a in nsf:
    ln = norm(a.get("piLastName"))
    if ln:
        by_last[ln].append(a)
print(f"active NSF awards (MI): {len(nsf)} across {len(by_last)} distinct PI surnames")

strict, loose, hits = 0, 0, []
for pid, L in final:
    fn, ln = split_nih(L["name"])
    if not ln: continue
    pool = by_last.get(ln, [])
    if not pool: continue
    loose += 1
    exact = [a for a in pool if norm(a.get("piFirstName")) == fn]
    if exact:
        strict += 1
        best = max(exact, key=lambda a: a.get("expDate") or "")
        hits.append((L["name"], L["org"], L["total"], best.get("expDate"),
                     best.get("estimatedTotalAmt"), (best.get("title") or "")[:44]))

n = len(final)
print(f"\n  surname-only match (upper bound) : {loose:>3}  ({loose/n:.1%})")
print(f"  first+last match  (lower bound)  : {strict:>3}  ({strict/n:.1%})")
print(f"\n  => NIH-only precision was ~83.5%; NSF removes {strict/n:.1%}-{loose/n:.1%} more")
lo = 0.835 * (1 - loose/n); hi = 0.835 * (1 - strict/n)
print(f"  => corrected precision band: {lo:.1%} - {hi:.1%}")

if hits:
    print(f"\n  labs with LIVE NSF money (would have been bad leads):")
    for nm, org, tot, exp, amt, ttl in sorted(hits, key=lambda x: -x[2])[:10]:
        print(f"    {nm[:24]:<25} NIH ${tot:>10,} | NSF thru {exp} ${int(amt or 0):>9,}")
        print(f"       {ttl}")
json.dump({"n": n, "loose": loose, "strict": strict}, open("nsf_result.json","w"))
