#!/usr/bin/env python3
"""
reputation_onchain.py -- a REAL counterparty-history source for Blackwall.

This is the spike for build-order step 2: replace MockReputationSource with a
source backed by live on-chain data, and answer the load-bearing question --
*can Blackwall get counterparty history fast and cheap enough for a per-call,
pre-signature check?*

It is a drop-in for MockReputationSource: same `.lookup(counterparty) -> record`
seam, so `blackwall.forecast()` consumes it with ZERO changes. The record dict
carries the keys `decide_payment` reads (settlement_count, dispute_rate,
price_history, sanctioned, known_bad, ...) plus a `_meta` block marking which
signals are REAL vs UNAVAILABLE from chain alone.

Data source: Blockscout for Base (https://base.blockscout.com), no API key.
Stdlib only (urllib). The HTTP fetch is isolated in `_default_get` so the
derivation logic (`derive_record`) is pure and unit-tested offline.

HONEST FINDINGS (see docs/DATA_SOURCE_SPIKE.md for the full memo):
  * Chain data gives volume / typical-amount / counterparty cheaply.
  * dispute_rate / underdelivery is NOT on-chain -- it is the moat signal and
    must come from Blackwall's OWN observed-outcome ledger or an off-chain feed.
    This source reports it as UNAVAILABLE (None), never as a fabricated 0.
  * sanctions is a STOP signal derivable from a loaded OFAC address set (off
    chain, freely published) -- passed in, not invented here.
  * Latency is ~1-2s per HTTP call => a synchronous per-signature lookup needs
    caching + x402 session reuse, exactly as the spec anticipated.
"""
from __future__ import annotations

import json
import time
import urllib.request
from decimal import Decimal

# Native USDC on Base (not bridged USDbC).
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
DEFAULT_BASE_URL = "https://base.blockscout.com"
DEFAULT_UA = "blackwall-reputation-spike/0.1"
# Two timeouts on purpose: /counters is fast and reliable; the transfers page
# is slow and variable on a free indexer (token-filtered queries can exceed
# 20s -- see the spike memo), so it is fetched BEST-EFFORT and the lookup
# degrades to a volume-only record rather than blocking the hot path.
COUNTERS_TIMEOUT = 12.0
TRANSFERS_TIMEOUT = 6.0
THIN_PAGE = 50  # Blockscout returns 50 token-transfers per page


def _adjust(value, decimals):
    """Integer smallest-unit string + decimals -> Decimal token amount."""
    try:
        return Decimal(str(value)) / (Decimal(10) ** int(decimals))
    except Exception:
        return None


def derive_record(counters_json, transfers_json, counterparty,
                  usdc_address=BASE_USDC, sanctioned=None, known_bad=None):
    """
    PURE: turn raw Blockscout responses into a Blackwall reputation record.

    `counters_json` is /addresses/{a}/counters; `transfers_json` is
    /addresses/{a}/token-transfers?type=ERC-20&filter=to (inbound, first page).
    No network here -- unit-tested from canned JSON.
    """
    sanctioned = sanctioned or set()
    known_bad = known_bad or set()
    cp = counterparty.lower()

    tx_count = int((counters_json or {}).get("transactions_count") or 0)
    token_xfer_count = int((counters_json or {}).get("token_transfers_count") or 0)

    items = (transfers_json or {}).get("items") or []
    has_more = bool((transfers_json or {}).get("next_page_params"))

    usdc_amounts = []
    for it in items:
        tok = it.get("token") or {}
        addr = (tok.get("address") or "").lower()
        if addr != usdc_address.lower():
            continue
        tot = it.get("total") or {}
        amt = _adjust(tot.get("value"), tot.get("decimals") or 6)
        if amt is not None and amt > 0:
            usdc_amounts.append(amt)

    # settlement_count: count of USDC payments RECEIVED. The first page is a
    # sample (<= THIN_PAGE); the true count needs pagination, so this is a
    # LOWER BOUND. It is still decision-useful: if >= THIN_HISTORY on one page,
    # the thin-history gate is cleared for real.
    usdc_inbound_sampled = len(usdc_amounts)
    settlement_count_lower_bound = usdc_inbound_sampled
    sampled = has_more and usdc_inbound_sampled >= THIN_PAGE

    # price_history proxy: the amounts this counterparty has RECEIVED. "Is this
    # charge anomalous vs what it usually takes" -- a real, on-chain question.
    price_history = [str(a) for a in usdc_amounts]

    record = {
        "settlement_count": settlement_count_lower_bound,
        # The moat signal is NOT on chain. Report UNAVAILABLE, never a fake 0 --
        # decide_payment treats None as 0 (optimistic), which is itself a finding:
        # on-chain-only reputation cannot see disputes.
        "dispute_rate": None,
        "price_history": price_history,
        "sanctioned": cp in {s.lower() for s in sanctioned},
        "known_bad": cp in {k.lower() for k in known_bad},
        "_meta": {
            "source": "blockscout-base",
            "tx_count": tx_count,
            "token_transfer_count": token_xfer_count,
            "usdc_inbound_sampled": usdc_inbound_sampled,
            "settlement_count_is_lower_bound": True,
            "sampled_truncated": sampled,
            "data_completeness": {
                "volume": "real",
                "typical_amount": "real" if usdc_amounts else "none-seen",
                "dispute_rate": "UNAVAILABLE-on-chain",
                "age_days": "needs-extra-query",
                "sanctioned": "from-supplied-OFAC-set",
            },
        },
    }
    return record


class OnchainReputationSource:
    """
    Live Base-backed reputation source. Drop-in for MockReputationSource.

    A small TTL cache fronts it because per-call latency (~1-2s/HTTP call) is
    too high for a synchronous hot-path check otherwise -- the cache + x402
    session reuse are how the per-signature path stays usable.
    """

    def __init__(self, base_url=DEFAULT_BASE_URL, usdc_address=BASE_USDC,
                 http_get=None, sanctioned=None, known_bad=None,
                 cache_ttl=60.0, user_agent=DEFAULT_UA):
        self.base_url = base_url.rstrip("/")
        self.usdc_address = usdc_address
        self._get = http_get or self._default_get
        self.sanctioned = set(sanctioned or ())
        self.known_bad = set(known_bad or ())
        self.cache_ttl = cache_ttl
        self.user_agent = user_agent
        self._cache = {}  # counterparty -> (expires_at, record)

    def _default_get(self, path, timeout=COUNTERS_TIMEOUT):
        url = self.base_url + path
        req = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent,
                          "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def lookup(self, counterparty):
        now = time.time()
        hit = self._cache.get(counterparty)
        if hit and hit[0] > now:
            return dict(hit[1])

        # /counters is required (fast). The transfers page is best-effort: on a
        # timeout/error the record degrades to volume-only signal instead of
        # blocking -- a HOLD-leaning record is the safe default.
        counters = self._get("/api/v2/addresses/%s/counters" % counterparty,
                             timeout=COUNTERS_TIMEOUT)
        transfers_ok = True
        try:
            transfers = self._get(
                "/api/v2/addresses/%s/token-transfers?type=ERC-20&filter=to"
                % counterparty, timeout=TRANSFERS_TIMEOUT)
        except Exception:
            transfers_ok = False
            transfers = {}
        record = derive_record(
            counters, transfers, counterparty,
            usdc_address=self.usdc_address,
            sanctioned=self.sanctioned, known_bad=self.known_bad)
        record["_meta"]["transfers_fetched"] = transfers_ok

        self._cache[counterparty] = (now + self.cache_ttl, record)
        return dict(record)
