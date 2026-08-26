"""Tighten the NSF match: surname + first initial + same institution."""
import json, re, collections, datetime as dt
from lab_signal import build_labs, find_dark_labs, drop_still_funded, dedupe_shared_grants
from run import national_awards

TODAY = dt.date(2026, 8, 25)
def norm(s): return re.sub(r"[^a-z]", "", (s or "").lower())
def inst_key(s):
    s = (s or "").lower()
    for k, v in (("michigan state","msu"), ("wayne state","wsu"),
                 ("michigan technological","mtu"), ("western michigan","wmu"),
                 ("michigan at ann arbor","umich"), ("university of michigan","umich"),
                 ("henry ford","hfhs"), ("van andel","vai"), ("oakland univ","ou")):
        if k in s: return v
    return norm(s)[:14]

rows = json.load(open("reporter_MI.json"))
labs = build_labs(rows)
cands = find_dark_labs(labs, TODAY)
final = dedupe_shared_grants(drop_still_funded(cands, national_awards([p for p,_ in cands]), TODAY))

nsf = json.load(open("nsf_MI.json"))
by_last = collections.defaultdict(list)
for a in nsf:
    if norm(a.get("piLastName")): by_last[norm(a.get("piLastName"))].append(a)

tiers = collections.Counter()
detail = []
for pid, L in final:
    parts = [p for p in (L["name"] or "").split() if p]
    if len(parts) < 2: continue
    fn, ln = norm(parts[0]), norm(parts[-1])
    pool = by_last.get(ln, [])
    if not pool:
        tiers["no surname match"] += 1; continue
    same_first = [a for a in pool if norm(a.get("piFirstName")) == fn]
    same_init  = [a for a in pool if norm(a.get("piFirstName"))[:1] == fn[:1]]
    same_inst  = [a for a in same_init if inst_key(a.get("awardeeName")) == inst_key(L["org"])]
    if same_first and same_inst:
        tiers["full name + institution"] += 1
        detail.append(("STRONG", L["name"], L["org"], L["total"], same_inst[0]))
    elif same_first:
        tiers["full name, diff institution"] += 1
        detail.append(("WEAK", L["name"], L["org"], L["total"], same_first[0]))
    elif same_inst:
        tiers["initial + institution"] += 1
        detail.append(("WEAK", L["name"], L["org"], L["total"], same_inst[0]))
    else:
        tiers["surname only (collision)"] += 1

n = len(final)
print(f"cohort: {n} NIH-dark MI labs vs {len(nsf)} active NSF awards\n")
for k in ("full name + institution","full name, diff institution",
          "initial + institution","surname only (collision)","no surname match"):
    print(f"  {k:<32} {tiers[k]:>4}  ({tiers[k]/n:>5.1%})")

strong = tiers["full name + institution"]
print(f"\n  DEFENSIBLE NSF overlap: {strong}/{n} = {strong/n:.1%}")
print("\n  strong matches:")
for tag, nm, org, tot, a in [d for d in detail if d[0]=="STRONG"]:
    print(f"    {nm[:26]:<27} {inst_key(org):<7} NSF thru {a.get('expDate')}  {(a.get('title') or '')[:40]}")
print("\n  full-name matches at a DIFFERENT institution (likely different people):")
for tag, nm, org, tot, a in [d for d in detail if d[0]=="WEAK"][:6]:
    print(f"    {nm[:26]:<27} NIH:{inst_key(org):<7} NSF:{inst_key(a.get('awardeeName')):<7} {(a.get('title') or '')[:34]}")
