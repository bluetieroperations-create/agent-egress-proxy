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


class SettlementResult:
    def __init__(self, ok: bool, method: str, reason: str = ""):
        self.ok = ok
        self.method = method  # "rpc" | "mock" | "unverified"
        self.reason = reason


def _topic_to_addr(topic: str) -> str:
    """A topic is a 32-byte hex word; an indexed address is its last 20 bytes."""
    return "0x" + topic[-40:].lower()


class RpcVerifier:
    def __init__(self, rpc_url: str, transport=None, timeout: float = 15.0):
        self._url = rpc_url
        self._timeout = timeout
        self._transport = transport or self._http_transport

    def _http_transport(self, request_obj: dict) -> dict:
        req = urllib.request.Request(
            self._url,
            data=json.dumps(request_obj).encode("utf-8"),
            headers={"Content-Type": "application/json"},
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

        for log in rec.get("logs", []):
            topics = log.get("topics", [])
            if len(topics) != 3 or topics[0].lower() != TRANSFER_TOPIC:
                continue
            if log.get("address", "").lower() != want_contract:
                continue
            if _topic_to_addr(topics[1]) != want_payer:
                continue
            if _topic_to_addr(topics[2]) != want_payee:
                continue
            amount = int(log.get("data", "0x0"), 16)
            if amount != want_amount:
                return SettlementResult(
                    False, "rpc",
                    f"amount mismatch: chain says {amount}, claim says {want_amount}",
                )
            return SettlementResult(True, "rpc")
        return SettlementResult(
            False, "rpc",
            "no matching USDC Transfer log (contract/payer/payee) in this transaction",
        )


class MockVerifier:
    """Accepts everything. For local development and tests ONLY — receipts
    it produces carry verification_method='mock' so they are visibly
    non-attestations."""

    def verify(self, settlement: dict) -> SettlementResult:
        return SettlementResult(True, "mock")


def make_verifier(mode: str, chain: str, rpc_url: str | None = None):
    if mode == "rpc":
        return RpcVerifier(rpc_url or RPC_DEFAULTS[chain])
    if mode == "mock":
        return MockVerifier()
    raise ValueError(f"unknown settlement mode {mode!r}")
