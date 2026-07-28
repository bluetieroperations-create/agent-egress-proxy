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


class SettlementResult:
    def __init__(self, ok: bool, method: str, reason: str = ""):
        self.ok = ok
        self.method = method  # "rpc" | "mock" | "unverified"
        self.reason = reason


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


class MockVerifier:
    """Accepts everything. For local development and tests ONLY — receipts
    it produces carry verification_method='mock' so they are visibly
    non-attestations."""

    def verify(self, settlement: dict) -> SettlementResult:
        return SettlementResult(True, "mock")


def make_verifier(mode: str, chain: str, rpc_url: str | None = None,
                  min_confirmations: int = 0):
    if mode == "rpc":
        return RpcVerifier(rpc_url or RPC_DEFAULTS[chain],
                           min_confirmations=min_confirmations)
    if mode == "mock":
        return MockVerifier()
    raise ValueError(f"unknown settlement mode {mode!r}")
