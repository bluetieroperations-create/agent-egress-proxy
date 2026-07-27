"""
On-chain anchor publisher: writes a Merkle root to the chain so an anchor's
existence-by-time is provable against a public ledger, not just our database.

The root is carried in the calldata of a 0-value self-send from a dedicated
GAS WALLET (never the receiving wallet). Signing is offline via eth-account;
broadcast is eth_sendRawTransaction over plain JSON-RPC. Both the RPC
transport and (for tests) the signer are injectable, so this is testable
with no funds and no network.

eth-account is an OPTIONAL dependency: the module only imports it when a
real publisher is constructed, keeping the core install stdlib + cryptography.
"""
from __future__ import annotations

import json
import urllib.request

CHAIN_IDS = {"base": 8453, "base-sepolia": 84532}


def make_publisher(mode, chain, rpc_url=None, private_key=None, transport=None):
    """Return a publisher callable `f(root_hex) -> (network, tx_hash) | None`
    suitable for Ledger.create_anchor(publisher=...), or None when off.

      mode="off"    -> None (record root locally only; onchain_tx stays null)
      mode="mock"   -> returns a deterministic fake tx hash (tests/dev)
      mode="onchain"-> signs + broadcasts a real data-carrying transaction
    """
    if mode == "off":
        return None
    if mode == "mock":
        return _MockPublisher()
    if mode == "onchain":
        if chain not in CHAIN_IDS:
            raise ValueError(f"unknown chain {chain!r}")
        if not private_key:
            raise ValueError("onchain publisher requires a gas-wallet private key")
        return OnchainPublisher(chain=chain, rpc_url=rpc_url,
                                private_key=private_key, transport=transport)
    raise ValueError(f"unknown publisher mode {mode!r}")


class _MockPublisher:
    def __call__(self, root_hex: str):
        # deterministic, clearly-fake tx hash derived from the root
        return "mock", "0x" + root_hex[:64].ljust(64, "0")


class OnchainPublisher:
    def __init__(self, *, chain, rpc_url, private_key, transport=None,
                 timeout=30.0):
        from eth_account import Account  # lazy: optional dependency
        self._Account = Account
        self._acct = Account.from_key(private_key)
        self.chain = chain
        self.chain_id = CHAIN_IDS[chain]
        self._rpc = rpc_url
        self._timeout = timeout
        self._transport = transport or self._http_rpc

    @property
    def address(self) -> str:
        return self._acct.address

    def _http_rpc(self, method: str, params: list):
        req = urllib.request.Request(
            self._rpc,
            data=json.dumps({"jsonrpc": "2.0", "id": 1,
                             "method": method, "params": params}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self._timeout) as r:
            resp = json.loads(r.read().decode())
        if "error" in resp:
            raise RuntimeError(f"rpc {method}: {resp['error']}")
        return resp["result"]

    def __call__(self, root_hex: str):
        """Sign and broadcast a tx carrying the root in calldata. Returns
        (network, tx_hash). Raises on RPC failure — create_anchor treats a
        raising publisher as 'no on-chain tx' only if the caller guards it;
        here we let it propagate so a failed publish is visible."""
        nonce = int(self._rpc_or(self._transport("eth_getTransactionCount",
                                                  [self.address, "pending"]), "0x0"), 16)
        gas_price = int(self._rpc_or(self._transport("eth_gasPrice", []), "0x3b9aca00"), 16)
        # calldata: a domain tag + the 32-byte root, so the tx is self-describing
        data = "0x" + b"TRACEIPT-ANCHOR\x01".hex() + root_hex
        tx = {
            "to": self.address,           # 0-value self-send; only the data matters
            "value": 0,
            "nonce": nonce,
            "gas": 40000,
            "gasPrice": gas_price,
            "chainId": self.chain_id,
            "data": data,
        }
        signed = self._Account.sign_transaction(tx, self._acct.key)
        raw = signed.raw_transaction.hex()
        if not raw.startswith("0x"):
            raw = "0x" + raw
        tx_hash = self._transport("eth_sendRawTransaction", [raw])
        return self.chain, tx_hash

    @staticmethod
    def _rpc_or(value, default):
        return value if isinstance(value, str) and value.startswith("0x") else default
