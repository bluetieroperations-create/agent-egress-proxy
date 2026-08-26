"""Which US public water systems exceed the EPA PFAS limits?

Source: EPA UCMR 5 occurrence data (free bulk ZIP, no key).
  https://www.epa.gov/system/files/other-files/2023-08/ucmr5-occurrence-data.zip

Measured Jan-2026 release: 1,928,117 results / 10,299 systems / 1,717 over limit.
"""
import csv
import collections

# EPA National Primary Drinking Water Regulation (April 2024), in ng/L.
LIMIT_NGL = {"PFOA": 4.0, "PFOS": 4.0, "PFHxS": 10.0, "PFNA": 10.0,
             "HFPO-DA": 10.0}

# UCMR 5 reports in µg/L. Comparing the raw value to the ng/L limit is wrong
# by 1000x and silently reports zero exceedances -- it did, on the first run.
NG_PER_UG = 1000.0

# 97% of rows are non-detects, carrying "<" and an empty value.
DETECTED = "="


def limit_ugl(contaminant):
    """Regulatory limit in the file's own units, or None if unregulated."""
    ngl = LIMIT_NGL.get(contaminant)
    return None if ngl is None else ngl / NG_PER_UG


def detected_value(row):
    """The measured concentration in µg/L, or None if not a detection."""
    if (row.get("AnalyticalResultsSign") or "").strip() != DETECTED:
        return None
    try:
        return float(row.get("AnalyticalResultValue") or "")
    except ValueError:
        return None


def exceedances(row, peak):
    """Contaminants where this system's peak reading is over the limit."""
    return {c: v for c, v in peak.items() if v > limit_ugl(c)}


def peak_by_system(rows):
    """Highest detected reading per system per regulated contaminant."""
    peak = collections.defaultdict(dict)
    meta = {}
    for row in rows:
        pwsid = row.get("PWSID")
        if not pwsid:
            continue
        meta[pwsid] = {"name": row.get("PWSName", ""),
                       "size": row.get("Size", ""),
                       "state": row.get("State", "")}
        contaminant = (row.get("Contaminant") or "").strip()
        if limit_ugl(contaminant) is None:
            continue
        value = detected_value(row)
        if value is None:
            continue
        if value > peak[pwsid].get(contaminant, -1.0):
            peak[pwsid][contaminant] = value
    return dict(peak), meta


def over_limit(peak):
    """Systems in violation, with the contaminants that put them there."""
    out = {}
    for pwsid, readings in peak.items():
        bad = exceedances(None, readings)
        if bad:
            out[pwsid] = bad
    return out


def read_ucmr(path):
    with open(path, encoding="latin-1", newline="") as fh:
        yield from csv.DictReader(fh, delimiter="\t")


if __name__ == "__main__":
    import sys
    peak, meta = peak_by_system(read_ucmr(sys.argv[1]))
    bad = over_limit(peak)
    print(f"systems monitored : {len(meta):,}")
    print(f"systems over limit: {len(bad):,} ({len(bad)/len(meta):.1%})")
    ranked = sorted(bad.items(), key=lambda kv: -max(kv[1].values()))
    for pwsid, readings in ranked:
        c, v = max(readings.items(), key=lambda x: x[1])
        m = meta[pwsid]
        print(f"{m['state']:<3} {m['size']:<2} {m['name'][:44]:<45} "
              f"{c:<8} {v*NG_PER_UG:>8.1f} ng/L")
