"""Detect NIH-funded labs whose support has ended.

Pure functions first (repo convention); all network I/O lives in
`fetch_reporter.py` and is injected. stdlib only.

Measured on Michigan FY2019-2026 (17,900 awards, 3,857 PIs) -- see
docs/WEEK_ONE_FINDINGS.md for the false-positive numbers behind every
constant below. None of them are guesses.
"""
import datetime as dt
import collections

# Awards that fund a trainee, not an equipment-bearing lab.
TRAINEE_CODES = frozenset({
    "F30", "F31", "F32", "F33", "F99", "T32", "T35", "T15",
    "K12", "K99", "R25", "R13", "D43",
})

# Department types that do desk science. A $35M panel survey has laptops,
# not freezers -- the largest flagged "lab" in the MI run was exactly this.
DESK_DEPTS = frozenset({
    "BIOSTATISTICS & OTHER MATH SCI",
    "PUBLIC HEALTH & PREV MEDICINE",
    "SOCIAL SCIENCES",
    "ECONOMICS",
    "EDUCATION",
})

# 95% of awards are posted to RePORTER within 90 days of their budget start
# (median 5 days). Flagging sooner reads posting lag as lab closure.
QUIET_DAYS = 90

# Beyond a year dark, it is history rather than a lead.
MAX_DARK_DAYS = 365

# Below this, the lab has no equipment worth a dealer's trip.
MIN_TOTAL_AWARD = 500_000


def to_date(value):
    """Parse a RePORTER ISO timestamp. None-safe."""
    return dt.date.fromisoformat(value[:10]) if value else None


def pi_ids(award):
    """Every PI profile_id on an award.

    Multi-PI grants fund each listed PI, so all of them count as supported.
    profile_id is RePORTER's own stable identifier -- measured coverage was
    22,033 of 22,034 PI entries, so no name matching is needed.
    """
    return [p["profile_id"]
            for p in (award.get("principal_investigators") or [])
            if p.get("profile_id")]


def support_end(award):
    """Date through which this award funds its PIs."""
    return to_date(award.get("budget_end")) or to_date(award.get("project_end_date"))


def is_desk_science(dept_types):
    """True when every known department is non-bench.

    Conservative: an unknown department never makes a lab look desk-bound,
    because dept_type is missing on roughly 45% of awards.
    """
    known = {d for d in dept_types if d}
    return bool(known) and known <= DESK_DEPTS


def build_labs(awards, vintage=None):
    """Roll awards up per PI.

    `vintage` reconstructs what RePORTER knew on a past date, using
    date_added (present on 100% of measured records). It captures a record's
    first appearance, not later edits to it.
    """
    labs = collections.defaultdict(lambda: {
        "name": None, "org": None, "total": 0, "last_end": None,
        "awards": 0, "codes": set(), "depts": set(),
    })
    for award in awards:
        if vintage is not None:
            added = to_date(award.get("date_added"))
            if added is None or added > vintage:
                continue
        end = support_end(award)
        if end is None:
            continue
        org = award.get("organization") or {}
        for pid in pi_ids(award):
            lab = labs[pid]
            lab["awards"] += 1
            lab["total"] += award.get("award_amount") or 0
            lab["codes"].add(award.get("activity_code") or "")
            lab["depts"].add(org.get("dept_type"))
            if lab["last_end"] is None or end > lab["last_end"]:
                lab["last_end"] = end
            if lab["name"] is None:
                for p in (award.get("principal_investigators") or []):
                    if p.get("profile_id") == pid:
                        lab["name"] = p.get("full_name")
                lab["org"] = org.get("org_name")
    return dict(labs)


def is_equipment_bearing(lab, min_total=MIN_TOTAL_AWARD):
    """A real bench lab: research-coded, funded at scale, not desk science."""
    if not (lab["codes"] - TRAINEE_CODES):
        return False
    if lab["total"] < min_total:
        return False
    return not is_desk_science(lab["depts"])


def find_dark_labs(labs, as_of, quiet_days=QUIET_DAYS,
                   max_dark_days=MAX_DARK_DAYS, min_total=MIN_TOTAL_AWARD):
    """PIs whose NIH support ended between quiet_days and max_dark_days ago.

    Returns candidates only. They are not leads until verified nationally --
    24.3% of state-flagged PIs simply moved institution (measured).
    """
    out = []
    for pid, lab in labs.items():
        if not is_equipment_bearing(lab, min_total):
            continue
        end = lab["last_end"]
        if end is None or end >= as_of:
            continue
        dark = (as_of - end).days
        if dark < quiet_days or dark > max_dark_days:
            continue
        out.append((pid, lab))
    out.sort(key=lambda item: -item[1]["total"])
    return out


def drop_still_funded(candidates, national_awards, as_of):
    """Remove PIs that any award anywhere still funds.

    The single most important correction: state-scoped flagging alone ran a
    36.0% false-positive rate, of which 24.3 points were PIs who had moved.
    """
    funded = set()
    for award in national_awards:
        end = support_end(award)
        if end is not None and end >= as_of:
            funded.update(pi_ids(award))
    return [(pid, lab) for pid, lab in candidates if pid not in funded]


def dedupe_shared_grants(candidates):
    """Collapse co-PIs on one grant to one site.

    Two PIs on the same multi-PI award are one physical lab, and a dealer
    should not receive the same address twice.
    """
    seen, out = set(), []
    for pid, lab in candidates:
        key = (lab["org"], lab["total"], lab["last_end"])
        if key in seen:
            continue
        seen.add(key)
        out.append((pid, lab))
    return out
