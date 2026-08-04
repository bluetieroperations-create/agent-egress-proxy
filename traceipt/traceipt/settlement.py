"""
Settlement verification: does the claimed USDC transfer actually exist
on-chain, with the claimed payer, payee, and amount?

Uses plain JSON-RPC over urllib (no web3 dependency):
  eth_getTransactionReceipt -> status must be success, and the logs must
  contain an ERC-20 Transfer event on the expected token contract matching
  payer/payee/amount.

The transport is injectable so tests run against a fake RPC.
"""
from __future__ import annotations

import json
import urllib.request

# keccak256("Transfer(address,address,uint256)") — the ERC-20 Transfer topic.
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Native USDC contracts.
USDC_CONTRACTS = {
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}

RPC_DEFAULTS = {
    "base": "https://mainnet.base.org",
    "base-sepolia": "https://sepolia.base.org",
}

# CAIP-2 network identifiers. x402 v2 requires CAIP-2 (`eip155:8453`), not the
# bare chain name — the service keeps the human name internally and converts at
# the protocol edge (the 402 challenge + facilitator envelope).
CAIP2 = {
    "base": "eip155:8453",
    "base-sepolia": "eip155:84532",
}

# EIP-712 domain (name, version) of each chain's USDC, carried in the v2 402
# challenge `extra` so a facilitator/client can reconstruct the domain and
# verify the EIP-3009 signature without an on-chain read. USDC domains are
# stable: Base mainnet is "USD Coin"/"2", Base Sepolia (Circle testnet) is
# "USDC"/"2".
EIP712_DOMAINS = {
    "base": {"name": "USD Coin", "version": "2"},
    "base-sepolia": {"name": "USDC", "version": "2"},
}


BLOCKSCOUT_BASES = {
    "base": "https://base.blockscout.com",
    "base-sepolia": "https://base-sepolia.blockscout.com",
}


class SettlementResult:
    def __init__(self, ok: bool, method: str, reason: str = "", *,
                 reachable: bool = True, cross_check: dict | None = None):
        self.ok = ok
        self.method = method  # "rpc" | "mock" | "blockscout" | "rpc+blockscout" | ...
        self.reason = reason
        # For a secondary/cross-check source: False means the source could not
        # be reached / had not indexed the tx (a lag, NOT a contradiction), so a
        # cross-checker must not fail a settlement on it. True + ok=False is a
        # real contradiction.
        self.reachable = reachable
        # Optional cross-check annotation, e.g. {"blockscout": "agree"}.
        self.cross_check = cross_check


def _topic_to_addr(topic: str) -> str:
    """A topic is a 32-byte hex word; an indexed address is its last 20 bytes."""
    return "0x" + topic[-40:].lower()


class RpcVerifier:
    def __init__(self, rpc_url: str, transport=None, timeout: float = 15.0,
                 min_confirmations: int = 0):
        self._url = rpc_url
        self._timeout = timeout
        # Finality guard: require the settlement's block to be buried under at
        # least this many confirmations before attesting it. 0 = attest as soon
        # as the tx is mined (fine for the fast soft-finality of an L2 like Base
        # / for testnet); set a few for real mainnet money so a tx that later
        # reorgs out is never turned into a durable receipt. See DEPLOY.md.
        self._min_confirmations = max(0, int(min_confirmations))
        self._transport = transport or self._http_transport
        self.label = "rpc"

    def _http_transport(self, request_obj: dict) -> dict:
        req = urllib.request.Request(
            self._url,
            data=json.dumps(request_obj).encode("utf-8"),
            # A User-Agent is required in practice: Cloudflare-fronted public
            # nodes (e.g. mainnet.base.org / sepolia.base.org) answer 403 to the
            # default Python-urllib UA, which would fail every settlement with
            # "rpc unreachable". Send a plain identifying UA + explicit accept.
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Traceipt/0.2 settlement-verifier",
            },
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def verify(self, settlement: dict) -> SettlementResult:
        """Check settlement claim against the chain. Never raises on a
        mismatch — returns a SettlementResult with a precise reason."""
        try:
            resp = self._transport({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_getTransactionReceipt",
                "params": [settlement["tx_hash"]],
            })
        except Exception as e:
            return SettlementResult(False, "rpc", f"rpc unreachable: {e}")

        rec = resp.get("result")
        if not rec:
            return SettlementResult(False, "rpc", "transaction not found")
        if rec.get("status") != "0x1":
            return SettlementResult(False, "rpc", "transaction reverted (status != 0x1)")

        want_contract = settlement["asset_contract"].lower()
        want_payer = settlement["payer"].lower()
        want_payee = settlement["payee"].lower()
        want_amount = int(settlement["amount_base_units"])

        # Scan ALL logs: a tx may legitimately contain several Transfer events
        # (router contracts, fee splits). Success = any log fully matches.
        # Only if none does do we report the most specific failure seen.
        near_miss = None
        for log in rec.get("logs", []):
            topics = log.get("topics", [])
            if len(topics) != 3 or not isinstance(topics[0], str) \
                    or topics[0].lower() != TRANSFER_TOPIC:
                continue
            if log.get("address", "").lower() != want_contract:
                continue
            if _topic_to_addr(topics[1]) != want_payer:
                continue
            if _topic_to_addr(topics[2]) != want_payee:
                continue
            try:
                amount = int(log.get("data", "0x0"), 16)
            except (ValueError, TypeError):
                continue  # malformed log data: not a match, never a crash
            if amount == want_amount:
                # Full match. Apply the finality guard once (all logs share
                # this tx's block) before attesting.
                conf_ok, conf_reason = self._confirmations_ok(rec)
                if not conf_ok:
                    return SettlementResult(False, "rpc", conf_reason)
                return SettlementResult(True, "rpc")
            near_miss = (f"amount mismatch: chain says {amount}, "
                         f"claim says {want_amount}")
        return SettlementResult(
            False, "rpc",
            near_miss or
            "no matching USDC Transfer log (contract/payer/payee) in this transaction",
        )

    def _confirmations_ok(self, rec: dict) -> tuple[bool, str]:
        """True if the settlement's block is at least `min_confirmations` deep.
        With the guard off (0) this is a no-op and costs no extra RPC call.
        Fail-safe: an unreadable head / block number is treated as NOT
        confirmed, so a receipt is never issued on an unverifiable depth."""
        if self._min_confirmations <= 0:
            return True, ""
        try:
            tx_block = int(rec.get("blockNumber", ""), 16)
        except (ValueError, TypeError):
            return False, "receipt is missing a block number"
        try:
            head_resp = self._transport({
                "jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber",
                "params": [],
            })
            head = int(head_resp.get("result", ""), 16)
        except (ValueError, TypeError) as e:
            return False, f"could not read chain head for confirmations: {e}"
        except Exception as e:  # transport failure -> fail safe
            return False, f"could not read chain head for confirmations: {e}"
        confirmations = head - tx_block + 1
        if confirmations < self._min_confirmations:
            return False, (f"insufficient confirmations "
                           f"({confirmations} of {self._min_confirmations} required)")
        return True, ""


class BlockscoutVerifier:
    """Verify a settlement against Blockscout's public API (keyless, free) — an
    INDEPENDENT second source to the RPC. Confirms the tx succeeded and carries a
    matching USDC Transfer (contract/payer/payee/amount).

    `reachable` on the result is the honest bit for a cross-checker: an
    unreachable / not-yet-indexed Blockscout returns reachable=False (a lag, do
    NOT block a valid settlement), while a tx that Blockscout HAS indexed but
    whose transfer contradicts the claim returns reachable=True, ok=False (a real
    contradiction worth blocking on). The transport is injectable for tests."""

    def __init__(self, chain: str, base_url: str | None = None, transport=None,
                 timeout: float = 15.0, min_confirmations: int = 0):
        self._base = (base_url or BLOCKSCOUT_BASES[chain]).rstrip("/")
        self._timeout = timeout
        self._min_confirmations = max(0, int(min_confirmations))
        self._transport = transport or self._http_get
        self.label = "blockscout"

    def _http_get(self, path: str) -> dict:
        req = urllib.request.Request(
            self._base + path,
            headers={"Accept": "application/json",
                     "User-Agent": "Traceipt/0.2 settlement-verifier"})
        with urllib.request.urlopen(req, timeout=self._timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def verify(self, settlement: dict) -> SettlementResult:
        tx = settlement["tx_hash"]
        want_contract = settlement["asset_contract"].lower()
        want_payer = settlement["payer"].lower()
        want_payee = settlement["payee"].lower()
        want_amount = int(settlement["amount_base_units"])

        # 1. tx status + confirmations. A 404 / transport error means Blockscout
        # has not indexed it (or is down): treat as UNAVAILABLE, never a block.
        try:
            txo = self._transport(f"/api/v2/transactions/{tx}")
        except Exception as e:  # noqa: BLE001
            return SettlementResult(False, "blockscout",
                                    f"blockscout unavailable: {e}", reachable=False)
        status = txo.get("status") if isinstance(txo, dict) else None
        if status == "error":
            return SettlementResult(False, "blockscout",
                                    "transaction reverted (status=error)", reachable=True)
        if status != "ok":
            return SettlementResult(False, "blockscout",
                                    f"transaction not confirmed on blockscout (status={status})",
                                    reachable=False)
        if self._min_confirmations > 0:
            try:
                conf = int(txo.get("confirmations") or 0)
            except (ValueError, TypeError):
                conf = 0
            if conf < self._min_confirmations:
                return SettlementResult(False, "blockscout",
                                        f"insufficient confirmations "
                                        f"({conf} of {self._min_confirmations})",
                                        reachable=True)

        # 2. the tx's ERC-20 transfers must contain the claimed USDC move.
        try:
            tt = self._transport(f"/api/v2/transactions/{tx}/token-transfers?type=ERC-20")
        except Exception as e:  # noqa: BLE001
            return SettlementResult(False, "blockscout",
                                    f"blockscout unavailable: {e}", reachable=False)
        items = tt.get("items") if isinstance(tt, dict) else None
        if not isinstance(items, list):
            return SettlementResult(False, "blockscout",
                                    "no token-transfer data", reachable=False)
        near_miss = None
        for i in items:
            tok = i.get("token") or {}
            addr = (tok.get("address") or tok.get("address_hash") or "").lower()
            frm = ((i.get("from") or {}).get("hash") or "").lower()
            to = ((i.get("to") or {}).get("hash") or "").lower()
            if addr != want_contract or frm != want_payer or to != want_payee:
                continue
            try:
                amount = int((i.get("total") or {}).get("value"))
            except (ValueError, TypeError):
                continue
            if amount == want_amount:
                return SettlementResult(True, "blockscout")
            near_miss = (f"amount mismatch: blockscout says {amount}, "
                         f"claim says {want_amount}")
        return SettlementResult(
            False, "blockscout",
            near_miss or "no matching USDC Transfer (contract/payer/payee) on blockscout",
            reachable=True)


class CrossCheckVerifier:
    """Run a PRIMARY verifier (authoritative) plus a SECONDARY cross-check.

    - primary fails            -> return the primary failure (unchanged).
    - primary ok, secondary ok -> method upgraded to 'primary+secondary'
                                  (the receipt now cites TWO independent sources).
    - primary ok, secondary UNAVAILABLE (unreachable / not indexed) -> the
                                  primary stands, annotated; a lagging indexer
                                  must never block a real payment.
    - primary ok, secondary CONTRADICTS -> FAIL. A second source that has the tx
                                  and disagrees is the whole point: it catches a
                                  forged/rogue RPC or a source-visible reorg."""

    def __init__(self, primary, secondary, secondary_name: str = "blockscout"):
        self._primary = primary
        self._secondary = secondary
        self._sname = secondary_name
        self.label = f"{getattr(primary, 'label', 'rpc')}+{secondary_name}"

    def verify(self, settlement: dict) -> SettlementResult:
        p = self._primary.verify(settlement)
        if not p.ok:
            return p
        s = self._secondary.verify(settlement)
        if s.ok:
            return SettlementResult(True, f"{p.method}+{self._sname}",
                                    cross_check={self._sname: "agree"})
        if not getattr(s, "reachable", True):
            return SettlementResult(
                True, p.method,
                f"verified by {p.method}; {self._sname} unavailable, not cross-checked",
                cross_check={self._sname: "unavailable", "reason": s.reason})
        return SettlementResult(
            False, f"{p.method}+{self._sname}",
            f"cross-check DISAGREEMENT: {self._sname} says: {s.reason}",
            cross_check={self._sname: "contradict", "reason": s.reason})


class MockVerifier:
    """Accepts everything. For local development and tests ONLY — receipts
    it produces carry verification_method='mock' so they are visibly
    non-attestations."""

    label = "mock"

    def verify(self, settlement: dict) -> SettlementResult:
        return SettlementResult(True, "mock")


def make_verifier(mode: str, chain: str, rpc_url: str | None = None,
                  min_confirmations: int = 0):
    if mode == "rpc":
        return RpcVerifier(rpc_url or RPC_DEFAULTS[chain],
                           min_confirmations=min_confirmations)
    if mode == "blockscout":
        return BlockscoutVerifier(chain, min_confirmations=min_confirmations)
    if mode == "rpc+blockscout":
        # RPC authoritative, Blockscout as an independent second source. A
        # concrete contradiction blocks; a lagging Blockscout does not.
        return CrossCheckVerifier(
            RpcVerifier(rpc_url or RPC_DEFAULTS[chain],
                        min_confirmations=min_confirmations),
            BlockscoutVerifier(chain, min_confirmations=min_confirmations))
    if mode == "mock":
        return MockVerifier()
    raise ValueError(f"unknown settlement mode {mode!r}")
