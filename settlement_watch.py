#!/usr/bin/env python3
"""
settlement_watch.py -- autonomous on-chain settlement confirmation for Blackwall.

The gate-#1 audit surfaced the moat's integrity hole: outcome reports to the
ledger are unauthenticated, so a caller could fake `settled` to inflate a
counterparty's reputation. The fix is to NOT trust self-reports for the one
fact that lives on-chain -- did the payment actually settle, for the right
amount, to the right recipient. This watcher reads that from Base and writes a
CHAIN-CONFIRMED `settled` outcome, keyed by the verdict's receipt_id.

Conceptual split this enforces:
  * SETTLEMENT (money moved, amount, recipient) -> trustless, from chain (here).
  * DELIVERY quality (did the API actually deliver) -> genuinely off-chain;
    still a report, but now decoupled from the un-fakeable settlement backbone.

Two modes:
  * confirm_by_tx  -- agent points at a settlement tx; we INDEPENDENTLY verify
    its token transfers (can't be faked -- pointing != forging).
  * scan_for_settlement -- zero agent cooperation: match the counterparty's
    recent inbound USDC against the expected amount/recipient/time window.

The matching core (extract_usdc_transfers / find_settlement) is pure and
unit-tested offline. Data source: Blockscout for Base (no key). Stdlib only.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
DEFAULT_BASE_URL = "https://base.blockscout.com"
DEFAULT_UA = "blackwall-settlement-watch/0.1"
HTTP_TIMEOUT = 12.0


# ===========================================================================
# Time + amount helpers
# ===========================================================================
def parse_ts(s):
    """Parse an ISO-8601 UTC timestamp (with or without microseconds / 'Z')."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_decimal(v):
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


# ===========================================================================
# PURE matching core (unit-tested offline)
# ===========================================================================
def extract_usdc_transfers(items, usdc_address=BASE_USDC):
    """
    PURE: normalize Blockscout token-transfer items to USDC transfers only.

    Each result: {to, from, amount(Decimal), tx_hash, timestamp(str)}. Non-USDC
    tokens are dropped, so a settlement match can't be spoofed by a same-amount
    transfer of some worthless lookalike token.
    """
    out = []
    usdc = usdc_address.lower()
    for it in items or []:
        tok = it.get("token") or {}
        # Blockscout v2 carries the contract under `address_hash`; older/other
        # shapes use `address`. Identify USDC by CONTRACT (trustworthy), never by
        # `symbol` (spoofable -- anyone can mint a token called "USDC").
        token_addr = (tok.get("address_hash") or tok.get("address") or "").lower()
        if token_addr != usdc:
            continue
        tot = it.get("total") or {}
        amt = _to_decimal(tot.get("value"))
        dec = tot.get("decimals")
        if amt is None or dec is None:
            continue
        try:
            amount = amt / (Decimal(10) ** int(dec))
        except (InvalidOperation, ValueError, ArithmeticError):
            continue
        out.append({
            "to": ((it.get("to") or {}).get("hash") or "").lower(),
            "from": ((it.get("from") or {}).get("hash") or "").lower(),
            "amount": amount,
            "tx_hash": it.get("transaction_hash"),
            "timestamp": it.get("timestamp"),
        })
    return out


def find_settlement(transfers, counterparty, expected_amount,
                    since_ts=None, from_addr=None, tolerance="0"):
    """
    PURE: return the first USDC transfer that settles the expected payment, else
    None.

    A match requires: recipient == counterparty, amount == expected within
    `tolerance` (absolute USDC), timestamp >= since_ts (if given, guards against
    matching an OLD unrelated payment of the same size), and sender == from_addr
    (if the agent's wallet is known -- the strongest guard).
    """
    cp = (counterparty or "").lower()
    want = _to_decimal(expected_amount)
    tol = _to_decimal(tolerance) or Decimal(0)
    if want is None:
        return None
    since = parse_ts(since_ts) if since_ts else None
    want_from = (from_addr or "").lower() or None

    for t in transfers:
        if t["to"] != cp:
            continue
        if abs(t["amount"] - want) > tol:
            continue
        if want_from is not None and t["from"] != want_from:
            continue
        if since is not None:
            ts = parse_ts(t.get("timestamp"))
            if ts is not None and ts < since:
                continue
        return t
    return None


# ===========================================================================
# Chain client (Blockscout/Base) -- injectable for tests
# ===========================================================================
class BlockscoutChain:
    def __init__(self, base_url=DEFAULT_BASE_URL, usdc_address=BASE_USDC,
                 user_agent=DEFAULT_UA, timeout=HTTP_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.usdc_address = usdc_address
        self.user_agent = user_agent
        self.timeout = timeout

    def _get(self, path):
        req = urllib.request.Request(
            self.base_url + path,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def tx_token_transfers(self, tx_hash):
        """USDC-normalized transfers carried by a single transaction."""
        data = self._get("/api/v2/transactions/%s/token-transfers" % tx_hash)
        return extract_usdc_transfers(data.get("items") or [], self.usdc_address)

    def recent_inbound(self, counterparty):
        """Counterparty's recent inbound ERC-20, normalized to USDC only.

        Uses the unfiltered recent page (newest first) and filters client-side
        -- the token-filtered endpoint is unusably slow on the free indexer
        (see docs/DATA_SOURCE_SPIKE.md), and a just-settled payment is on the
        first page anyway.
        """
        data = self._get(
            "/api/v2/addresses/%s/token-transfers?type=ERC-20&filter=to"
            % counterparty)
        return extract_usdc_transfers(data.get("items") or [], self.usdc_address)


# ===========================================================================
# The watcher
# ===========================================================================
class SettlementWatcher:
    """Confirms settlements on-chain and writes CHAIN-CONFIRMED outcomes."""

    def __init__(self, chain, ledger, tolerance="0"):
        self.chain = chain
        self.ledger = ledger
        self.tolerance = tolerance

    def confirm_by_tx(self, receipt_id, counterparty, amount, tx_hash,
                      from_addr=None):
        """Verify a specific tx settles the payment; record `settled` if so.

        Returns the matching transfer dict, or None if the tx does not contain a
        matching USDC transfer (in which case NOTHING is recorded)."""
        transfers = self.chain.tx_token_transfers(tx_hash)
        match = find_settlement(transfers, counterparty, amount,
                                from_addr=from_addr, tolerance=self.tolerance)
        if match is None:
            return None
        self.ledger.record_outcome(
            receipt_id=receipt_id, outcome="settled",
            observed_amount=match["amount"], settlement_tx=tx_hash,
            source="chain-watch")
        return match

    def scan_for_settlement(self, counterparty, amount, since_ts=None,
                            from_addr=None):
        """Zero-cooperation match against the counterparty's recent inbound."""
        transfers = self.chain.recent_inbound(counterparty)
        return find_settlement(transfers, counterparty, amount,
                               since_ts=since_ts, from_addr=from_addr,
                               tolerance=self.tolerance)

    def confirm_pending(self, limit=None):
        """
        Scan the ledger for GO verdicts that have no outcome yet and try to
        confirm each on-chain. Records a chain-confirmed `settled` per match.
        Returns the number newly confirmed.
        """
        events = list(self.ledger.read_events())
        have_outcome = {e.get("receipt_id") for e in events
                        if e.get("kind") == "outcome"}
        # A given on-chain tx settles exactly ONE payment, so it may confirm at
        # most one receipt -- otherwise a single inbound payment would over-count
        # every pending GO of the same amount to the same counterparty (audit:
        # 1 tx wrongly confirmed N receipts). Seed from txs already used.
        used_tx = {e.get("settlement_tx") for e in events
                   if e.get("kind") == "outcome" and e.get("settlement_tx")}
        pending = [e for e in events
                   if e.get("kind") == "verdict"
                   and e.get("verdict") == "GO"
                   and e.get("receipt_id") not in have_outcome]
        if limit is not None:
            pending = pending[:limit]

        confirmed = 0
        for e in pending:
            match = self.scan_for_settlement(
                e.get("counterparty"), e.get("amount"), since_ts=e.get("ts"))
            if match is None:
                continue
            tx = match.get("tx_hash")
            if tx is None or tx in used_tx:
                continue  # unidentifiable or already-claimed settlement
            self.ledger.record_outcome(
                receipt_id=e.get("receipt_id"), outcome="settled",
                observed_amount=match["amount"],
                settlement_tx=tx, source="chain-watch")
            used_tx.add(tx)
            confirmed += 1
        return confirmed


if __name__ == "__main__":
    import sys
    from ledger import EventLedger
    path = sys.argv[1] if len(sys.argv) > 1 else "blackwall_ledger.jsonl"
    w = SettlementWatcher(BlockscoutChain(), EventLedger(path))
    n = w.confirm_pending()
    print("chain-confirmed %d pending GO settlement(s) in %s" % (n, path))
