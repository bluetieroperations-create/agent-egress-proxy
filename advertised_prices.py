"""Supply each payee's OWN advertised price range to the verdict.

WHY THIS EXISTS
---------------
`blackwall.price_stop_is_corroborated` withholds a price STOP when the quote is
corroborated, by either of two independent checks: an established multi-payer
TIER at that price, or the payee's own ADVERTISED range. The second arm was inert
in production -- `reputation_store` holds settlements only, so nothing ever
supplied `advertised_min_price` / `advertised_max_price` and the check always
answered "unknown".

That left three legitimate sellers STOP'd on live Base endpoints: each quotes a
premium route whose price appears in their published catalog, but whose on-chain
history at that price comes from a single repeat customer, so the tier arm cannot
vouch for it. Their catalogs can.

`ecosystem_scan` already writes exactly this data to `data/directory.json`
(min_price / max_price per payee, harvested from the x402 discovery crawl). This
module turns that artifact into a reputation source.

SECURITY -- READ BEFORE EXTENDING
---------------------------------
This signal is MONOTONICALLY PERMISSIVE: a wider advertised range corroborates
more prices, and corroboration can only ever downgrade STOP -> HOLD. A payee that
controls its own advertised range could therefore widen it to escape a price STOP.

Two properties keep that safe, and both must hold for any future change:

  1. The range comes from OUR OWN committed crawl artifact, loaded at startup --
     never from the request, never from a live fetch on the hot path. A payee
     cannot influence a verdict by editing what it serves at payment time.
  2. It never reaches GO. A ratio >= STOP_ANOMALY_RATIO scores anomaly 1.0, so
     `hold_conditions` still denies auto-approve. The worst case is a payment
     routed to human review instead of refused -- not a signed overpayment.

It is also strictly ADDITIVE: it contributes no settlements, no payer counts, and
no dispute signal, so it cannot move the reputation, Sybil, or thin-history gates.

Fail-open by construction: a missing or malformed artifact yields an empty index,
every lookup answers None, and `within_advertised_range` reads that as "unknown"
-- which is not evidence of wrongdoing, so the STOP simply stands as it does today.
"""

from __future__ import annotations

import json
import os

#: The artifact ecosystem_scan writes. Overridable for tests/alternate corpora.
DEFAULT_INDEX_PATH = "data/directory.json"

#: Env var that switches the source on in the server.
ENV_INDEX_PATH = "BLACKWALL_ADVERTISED_INDEX"


def _normalize(address):
    """Lowercase an EVM address for use as a join key.

    NOT cosmetic. A live 402 challenge returns an EIP-55 CHECKSUMMED `payTo`
    while the crawl artifact stores lowercase, so an exact-match join misses
    almost everything -- measured at 64 of 69 live endpoints silently answering
    "no catalog data". The store already lowercases for the same reason.
    """
    if not isinstance(address, str):
        return None
    address = address.strip().lower()
    return address or None


def _price(value):
    """Accept a price only if it is a finite, non-negative number.

    Kept as the ORIGINAL string/number for the caller's Decimal conversion --
    parsing to float here would introduce binary rounding into a monetary bound.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")) or parsed < 0:
        return None
    return value


def build_advertised_index(records):
    """Pure: [directory record] -> {payee_lower: (min_price, max_price)}.

    Only entries with BOTH bounds usable are kept -- a half-known range cannot
    answer "is this quote inside the catalog", and guessing the missing side
    would invent a bound the payee never advertised.

    On a duplicate payee the WIDEST range would be the permissive choice, so the
    NARROWEST is taken instead: this signal only ever relaxes a STOP, and where
    the corpus disagrees with itself the conservative reading should win.
    """
    index = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        payee = _normalize(record.get("payee"))
        if not payee:
            continue
        low, high = _price(record.get("min_price")), _price(record.get("max_price"))
        if low is None or high is None:
            continue
        if payee in index:
            previous_low, previous_high = index[payee]
            low = max(float(low), float(previous_low))
            high = min(float(high), float(previous_high))
            if low > high:                      # disjoint ranges -> no usable claim
                del index[payee]
                continue
        index[payee] = (low, high)
    return index


def load_advertised_index(path=None):
    """Load the crawl artifact into an index. Fail-open: never raises."""
    path = path or os.environ.get(ENV_INDEX_PATH) or DEFAULT_INDEX_PATH
    try:
        with open(path) as handle:
            records = json.load(handle)
    except (OSError, ValueError):
        return {}
    if isinstance(records, dict):
        records = records.get("payees") or records.get("directory") or []
    if not isinstance(records, list):
        return {}
    return build_advertised_index(records)


class AdvertisedPriceSource:
    """Reputation source contributing ONLY a payee's advertised price bounds.

    Composes through `reputation_store.CombinedReputationSource`; carries no
    settlements, so `merge_records` leaves every reputation gate untouched.
    """

    def __init__(self, index=None, path=None):
        self.index = index if index is not None else load_advertised_index(path)

    @classmethod
    def from_path(cls, path=None):
        return cls(path=path)

    def __len__(self):
        return len(self.index)

    def lookup(self, counterparty):
        key = _normalize(counterparty)
        if not key:
            return None
        bounds = self.index.get(key)
        if not bounds:
            return None
        low, high = bounds
        return {
            "advertised_min_price": low,
            "advertised_max_price": high,
            # No settlement_count: this source must never make a cold-start payee
            # look established. _meta.known stays False so merge_records does not
            # count it as evidence the counterparty is known.
            "_meta": {"source": "advertised", "known": False},
        }
