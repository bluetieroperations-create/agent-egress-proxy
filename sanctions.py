#!/usr/bin/env python3
"""
sanctions.py -- OFAC sanctions screening (Blackwall's "superset of free").

The free facilitator baseline (e.g. Coinbase CDP's KYT) declines sanctioned
addresses. For Blackwall to be strictly BETTER than free -- "everything the free
check does, PLUS counterparty reputation + price-anomaly, in one call" -- it
screens the counterparty against an OFAC sanctioned-address list and STOPs on a
hit, on top of its behavioral signals.

Source: the OFAC SDN list's digital-currency addresses, publicly published one
address per line (e.g. the community-maintained
`0xB10C/ofac-sanctioned-digital-currency-addresses`). Load from an operator-
supplied file and/or refresh from a URL. The operator owns keeping it current --
this is the screening *mechanism*, not legal advice.

`SanctionsScreeningSource` wraps ANY reputation source and OR-s in the sanctioned
flag, so screening composes orthogonally with the store / ledger / mock. The pure
parts (membership, file parse) are unit-tested offline. Stdlib only.
"""
from __future__ import annotations

import urllib.request

from addresses import is_evm_address

# Canonical OFAC digital-currency address list (EVM addresses; usable on Base).
DEFAULT_OFAC_URL = ("https://raw.githubusercontent.com/0xB10C/"
                    "ofac-sanctioned-digital-currency-addresses/lists/"
                    "sanctioned_addresses_ETH.txt")


def _normalize(addr):
    return addr.strip().lower() if isinstance(addr, str) else None


def parse_sanctioned_addresses(text):
    """Pure: extract EVM addresses from a plain-text OFAC list.

    One token per line; `#` comments and blank lines ignored; only tokens that
    validate as EVM addresses are kept (so a header line or a non-EVM chain entry
    can't poison the list). Returns them in file order (dedup happens in the set).
    """
    out = []
    for line in (text or "").splitlines():
        tok = line.strip().split("#", 1)[0].strip()
        if not tok:
            continue
        first = tok.split()[0]
        if is_evm_address(first):
            out.append(first)
    return out


class SanctionsList:
    """A set of sanctioned addresses with case-insensitive membership."""

    def __init__(self, addresses=None):
        self._set = set()
        for a in (addresses or ()):
            self.add(a)

    def add(self, addr):
        n = _normalize(addr)
        if n:
            self._set.add(n)

    def is_sanctioned(self, address):
        n = _normalize(address)
        return n is not None and n in self._set

    def __len__(self):
        return len(self._set)

    @classmethod
    def from_file(cls, path):
        """Load addresses from a plain-text file, keeping ONLY tokens that
        validate as EVM addresses (via parse_sanctioned_addresses -- symmetric
        with refresh_from_url). Junk can never match a real counterparty, but it
        would inflate len() and make sanctions_enabled() advertise screening while
        screening nothing real. `#` comments/blanks ignored; missing file -> empty."""
        s = cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return s
        for addr in parse_sanctioned_addresses(text):
            s.add(addr)
        return s

    def refresh_from_url(self, url=DEFAULT_OFAC_URL, timeout=15,
                         max_bytes=8 * 1024 * 1024):
        """Fetch a plain-text address list and merge in the EVM addresses.
        Returns the count of NEW (previously-unseen) addresses added. Network
        errors propagate to the caller (so the caller can fail-open).

        Merge is UNION-ONLY -- it never removes. This is deliberate: a truncated
        or failed fetch can never *weaken* screening. Trade-off: a de-listed
        address persists until the baked-in snapshot is regenerated
        (`python sanctions.py sanctions.txt`). New designations are picked up
        promptly; de-listings lag to a re-bake (conservative, over-screens).

        The body is capped at `max_bytes` (default 8MB; the real feed is tens of
        KB). A hijacked/MITM'd upstream or a CDN error page streaming megabytes
        would otherwise be read fully into memory and OOM the boot on a small box.
        We RAISE on overflow (caller fails open to the current list) rather than
        truncate -- truncation could silently drop real addresses (fail-open)."""
        req = urllib.request.Request(
            url, headers={"User-Agent": "blackwall-sanctions/0.1",
                          "Accept": "text/plain"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(max_bytes + 1)  # one past the cap to DETECT overflow
        if len(raw) > max_bytes:
            raise ValueError(
                "sanctions feed exceeds %d-byte cap (refusing; keeping current "
                "list)" % max_bytes)
        text = raw.decode("utf-8", "replace")
        before = len(self._set)
        for addr in parse_sanctioned_addresses(text):
            self.add(addr)
        return len(self._set) - before


class SanctionsScreeningSource:
    """
    Drop-in reputation source that adds OFAC screening on top of any inner
    source: it returns the inner record with `sanctioned` OR-ed with a hit on the
    sanctions list. decide_payment treats `sanctioned` as a hard STOP, so a
    sanctioned counterparty is blocked regardless of its behavioral signals.
    """

    def __init__(self, inner, sanctions):
        self.inner = inner
        self.sanctions = sanctions

    def lookup(self, counterparty):
        rec = dict(self.inner.lookup(counterparty))
        if self.sanctions.is_sanctioned(counterparty):
            rec["sanctioned"] = True
        return rec


def start_background_refresh(sanctions_list, url=DEFAULT_OFAC_URL, timeout=15,
                            refresher=None, log=None):
    """Refresh `sanctions_list` from the live OFAC feed in a DAEMON thread, so a
    slow / degraded / drip-feeding upstream can NEVER block startup or the socket
    bind (a synchronous refresh could: socket timeouts bound per-read inactivity,
    not total transfer time). The server binds immediately and serves the
    baked-in snapshot; the live list merges in when the fetch returns.

    The merge is in-place, union-only, grow-only -- CPython set ops are atomic
    under the GIL, so a concurrent is_sanctioned() read always sees a valid set
    (at worst it misses a brand-new designation for the few ms of the merge, never
    corrupts). The descriptor advertises screening dynamically (len-based), so it
    flips ON on its own once addresses land. Fail-open: any error keeps the
    current list. Returns the started Thread. `refresher(list)->int` and `log(str)`
    are injectable for tests."""
    import threading

    def _run():
        try:
            n = (refresher(sanctions_list) if refresher
                 else sanctions_list.refresh_from_url(url=url, timeout=timeout))
            if log:
                log("blackwall: sanctions refreshed from OFAC list "
                    "(+%d new, %d total)" % (n, len(sanctions_list)))
        except Exception as e:  # fail-open -- keep whatever is already loaded
            if log:
                log("blackwall: sanctions refresh failed (%s); keeping current "
                    "list (%d addresses)" % (e, len(sanctions_list)))

    t = threading.Thread(target=_run, name="sanctions-refresh", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    # Refresh a local sanctions file from the published OFAC list.
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "sanctions.txt"
    sl = SanctionsList()
    n = sl.refresh_from_url()
    with open(out, "w", encoding="utf-8") as f:
        f.write("# OFAC sanctioned EVM addresses -- refreshed from %s\n" % DEFAULT_OFAC_URL)
        for a in sorted(sl._set):
            f.write(a + "\n")
    sys.stdout.write("wrote %d sanctioned addresses to %s\n" % (n, out))
