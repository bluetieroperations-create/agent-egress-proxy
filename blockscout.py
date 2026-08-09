#!/usr/bin/env python3
"""
blockscout.py -- FREE keyless on-chain ENRICHMENT for Blackwall verdicts (Base).

Blockscout's public Base API (https://base.blockscout.com/api/v2, no key, no paid tier)
gives behavioral/heuristic context on a counterparty: `is_scam` (a crowd/heuristic
tag), contract-vs-EOA, ENS/labels, and ERC-20 transfer activity (spam-airdrop exposure).

╔══════════════════════════════════════════════════════════════════════════════════╗
║ HARD BOUNDARY (do not cross): Blockscout is RAW CHAIN DATA + sparse crowd tags --  ║
║ NOT a sanctions/attribution source. This layer can only ever push a verdict toward ║
║ REVIEW (HOLD). It must NEVER produce a sanctions clear (GO) or hit (STOP), and     ║
║ never sets hard_stop. Dressing chain data up as "compliance screening" is a        ║
║ liability, not a feature -- OFAC/Chainalysis/TRM stay the sanctions authority.     ║
╚══════════════════════════════════════════════════════════════════════════════════╝

So `address_enrichment()` returns a `review` flag that the verdict folds in HOLD-only
(structurally it can only turn a would-be GO into HOLD; it can't cause or clear a STOP).
The other fields (is_contract, distinct_incoming_tokens, tags, has_ens) are DESCRIPTIVE
context for a human reviewer, not gates.

The source does a LIVE fetch at verdict time, so it is OPT-IN (network on the hot path)
and FAIL-OPEN (Blockscout down / rate-limited / any error -> no signal, never blocks).
Bounded: one page of token-transfers, short timeout, per-session cache. Uses the repo's
hardened http_util (retry/backoff/size-cap + a real User-Agent -- Blockscout 403s the
default urllib UA). Caveat: the /counters endpoint was flaky; we use token-transfers + tx.
"""
from __future__ import annotations

BLOCKSCOUT_BASE = "https://base.blockscout.com"


def address_enrichment(address_json, transfers_items=None):
    """Derive the enrichment signal from a Blockscout `/addresses/{addr}` object and its
    ERC-20 `token-transfers` items. PURE. `review` is HOLD-only (see the hard boundary):
    it fires ONLY on Blockscout's `is_scam` crowd tag -- never on anything that would
    imply a sanctions decision."""
    a = address_json or {}
    items = [it for it in (transfers_items or []) if isinstance(it, dict)]

    is_scam = bool(a.get("is_scam"))
    is_contract = bool(a.get("is_contract"))
    has_ens = bool(a.get("ens_domain_name"))
    tags = [t.get("label") for t in (a.get("public_tags") or []) if isinstance(t, dict)]
    tags += [w for w in (a.get("watchlist_names") or []) if isinstance(w, str)]

    # distinct incoming ERC-20 tokens -- a spam-airdrop exposure proxy. DESCRIPTIVE
    # only: spam lands on every active wallet, so it informs a reviewer but never gates.
    cp = (a.get("hash") or "").lower()
    distinct_tokens = len({(it.get("token") or {}).get("address_hash", "").lower()
                           for it in items} - {""})

    # REVIEW is the ONLY thing that touches the verdict, and only via is_scam.
    review = is_scam
    reasons = []
    if is_scam:
        reasons.append(
            "Blockscout flags this address as scam-associated -- behavioral/crowd tag, "
            "REVIEW only (NOT a sanctions clear or hit; verify with a compliance feed)")

    return {
        "is_scam": is_scam,
        "is_contract": is_contract,
        "has_ens": has_ens,
        "tags": tags,
        "distinct_incoming_tokens": distinct_tokens,
        "review": review,
        "reasons": reasons,
        "source": "blockscout-base",
    }


def _default_fetch(url, timeout):
    import http_util
    return http_util.get_json(url, timeout=timeout)


class BlockscoutEnrichmentSource:
    """Live, fail-open Blockscout enrichment for a counterparty. OPT-IN (a network call
    on the verdict hot path). `fetch(url, timeout) -> dict` is injectable for tests."""

    def __init__(self, base_url=BLOCKSCOUT_BASE, *, fetch=None, timeout=8, cache=True):
        self.base = base_url.rstrip("/")
        self._fetch = fetch or _default_fetch
        self.timeout = timeout
        self._cache = {} if cache else None

    def enrich(self, counterparty):
        """Return the enrichment signal dict, or None. FAIL-OPEN: any error (offline,
        403, rate-limit, malformed) -> None, never raises.

        The address fetch carries the ONLY gating field (`is_scam` -> review) and is
        cheap; the token-transfers fetch feeds only the DESCRIPTIVE
        distinct_incoming_tokens and is expensive on high-volume addresses (it can time
        out). So the two are decoupled: the address fetch is required, transfers are
        best-effort -- a slow/failed transfers page must not discard the review signal."""
        key = (counterparty or "").lower()
        if not key:
            return None
        if self._cache is not None and key in self._cache:
            return self._cache[key]
        sig = None
        try:
            addr = self._fetch("%s/api/v2/addresses/%s" % (self.base, key), self.timeout)
            items = None
            try:
                tt = self._fetch("%s/api/v2/addresses/%s/token-transfers?type=ERC-20"
                                 % (self.base, key), self.timeout)
                items = (tt or {}).get("items") if isinstance(tt, dict) else None
            except Exception:
                items = None   # transfers are descriptive-only; keep the review signal
            sig = address_enrichment(addr, items)
        except Exception:
            sig = None   # FAIL-OPEN -- enrichment must never break a verdict
        if self._cache is not None:
            self._cache[key] = sig
        return sig
