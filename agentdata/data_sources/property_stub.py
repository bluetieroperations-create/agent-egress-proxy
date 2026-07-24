"""
property_stub.py -- STUB property-records source (deterministic fake).

Produces stable, plausible records derived from a hash of the address, so:
  * the same address always returns the same result (cacheable, testable),
  * different addresses return different results (exercises the pipeline).

This exists so the platform runs end-to-end with ZERO data licensing. Replace
with a real source (ATTOM-class wholesale feed, county open-data + permit
datasets) by implementing DataSource.fetch and swapping it in the wiring. The
product code does not change.

    >>> from .property_stub import StubPropertySource
    >>> src = StubPropertySource()
    >>> rec = src.fetch({"address": "123 Main St, Austin TX"})
    >>> sorted(rec)                          # doctest: +NORMALIZE_WHITESPACE
    ['code_violations', 'flood_zone', 'last_sale_price', 'open_permits',
     'owner', 'parcel_id', 'unpermitted_work_suspected', 'year_built',
     'zoning']
"""
from __future__ import annotations

import hashlib

from .base import DataSource

_ZONES = ["R-1 residential", "R-2 residential", "C-1 commercial",
          "MU mixed-use", "I-1 light industrial"]
_FLOOD = ["X (minimal)", "AE (1% annual)", "A (high risk)", "X (minimal)"]


def _h(address):
    return hashlib.sha256(address.strip().lower().encode("utf-8")).digest()


class StubPropertySource(DataSource):
    name = "property_stub"

    def fetch(self, query):
        address = (query or {}).get("address", "")
        if not address:
            return {}
        d = _h(address)
        # derive stable pseudo-facts from the digest bytes
        parcel = "%02X%02X-%02X-%02X" % (d[0], d[1], d[2], d[3])
        year_built = 1920 + (d[4] % 105)
        open_permits = d[5] % 4                     # 0..3
        code_violations = d[6] % 5                  # 0..4
        # "unpermitted work suspected" when there is recent work signal but a
        # gap in permits -- a genuinely useful derived signal for a buyer.
        unpermitted = (d[7] % 3 == 0) and open_permits == 0 and (d[8] % 2 == 0)
        last_sale = 150_000 + (int.from_bytes(d[9:12], "big") % 1_350_000)
        return {
            "parcel_id": parcel,
            "owner": "OWNER-%02X%02X" % (d[12], d[13]),
            "year_built": year_built,
            "zoning": _ZONES[d[14] % len(_ZONES)],
            "flood_zone": _FLOOD[d[15] % len(_FLOOD)],
            "open_permits": open_permits,
            "code_violations": code_violations,
            "unpermitted_work_suspected": bool(unpermitted),
            "last_sale_price": last_sale,
        }
