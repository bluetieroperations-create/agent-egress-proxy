#!/usr/bin/env python3
"""
rams_readiness.py -- DORMANT-BUT-READY ERC-8226 (RAMS) agent-authorization readiness.

RAMS (Regulated Agent Mandate Standard, Brickken; Draft ERC) is the agent-AUTHORIZATION
axis that sits ABOVE our eligibility axis (see docs/TOKENIZED_RWA.md, COMPETITIVE.md). A
regulated token validates an agent-initiated transfer via an on-chain mandate registry:

    IAgentMandate.canExecute(agent, principal, asset, action, amount)
        -> (bool allowed, ExecutionReason reason)

allowed=False means the principal's mandate does NOT permit this action (OVER_TX_CAP /
AGENT_FROZEN / REVOKED / NOT_ACTIVE / expired) -- a SECOND revert cause our
eligibility reads (isVerified/canTransfer) never see. This module reads that predicate
exactly like `rwa_readiness` reads `detectTransferRestriction`, and folds it through the
SAME `apply_rwa_readiness` (HOLD-only, conservative, fail-open).

WHY "dormant-but-ready": real assets exposing a queryable RAMS registry are ~zero today
(Draft, one Sepolia deployment). So this source is a NO-OP until a request advertises a
mandate registry (`acquires.mandate_registry`) -- then it activates with ZERO code
change. It shares the `check(acquires, payer, counterparty)` signature of the RWA
sources, so it slots straight into `CombinedRwaReadinessSource`: wire it once (idle), and
the day an asset ships a RAMS hook, it just starts firing.

Inputs it needs (fail-open / dormant when absent): the mandate registry address, the
AGENT (the request `payer`, i.e. the wallet signing), and the PRINCIPAL
(`acquires.principal`, the delegator). Optional `acquires.action` (bytes32; default =
the ERC-20 transferFrom selector) and `acquires.mandate_amount` (token units; default 0,
which still exercises the revoked/frozen/expired/action gates, just not the per-tx cap).

CAVEAT: the ExecutionReason enum numbering is not finalized in the Draft, so a nonzero
reason is reported generically ("reason code N"), not mapped to a name. Stdlib; reuses
rwa_readiness's ABI helpers.
"""
from __future__ import annotations

from addresses import is_evm_address
from rwa_readiness import decode_bool, decode_uint, selector

# Default action if the request doesn't specify one: an agent-initiated ERC-20 transfer.
_DEFAULT_ACTION = selector("transferFrom(address,address,uint256)")  # 0x + 8 hex (4 bytes)


def _addr_word(a):
    return a[2:].lower().rjust(64, "0")


def _bytes32_word(hexval):
    """bytes32 is LEFT-aligned (value in the high bytes) -> right-pad to 64 nibbles."""
    s = hexval[2:] if hexval.lower().startswith("0x") else hexval
    return (s[:64]).ljust(64, "0")


def _uint_word(v):
    try:
        return format(int(str(v)), "x").rjust(64, "0")
    except (TypeError, ValueError):
        return "0" * 64


def encode_can_execute(agent, principal, asset, action, amount):
    """Calldata for canExecute(address,address,address,bytes32,uint256)."""
    sel = selector("canExecute(address,address,address,bytes32,uint256)")
    return (sel + _addr_word(agent) + _addr_word(principal) + _addr_word(asset)
            + _bytes32_word(action) + _uint_word(amount))


def decode_allowed_reason(result):
    """Decode a `(bool allowed, ExecutionReason reason)` return -> (allowed:bool|None,
    reason:int|None). Tolerates a single-word `bool` return (older RAMS) -> reason None.
    NEVER raises."""
    allowed = decode_bool(result)                    # reads word[0], strict 0/1 -> bool
    reason = None
    if isinstance(result, str):
        s = result[2:] if result.lower().startswith("0x") else result
        if len(s) >= 128:
            reason = decode_uint("0x" + s[64:128])
    return (allowed, reason)


def assess_mandate(allowed, reason=None):
    """PURE: a canExecute result -> a readiness signal in rwa_readiness's shape.
      * allowed False -> blocked (the mandate does not permit this; the transfer would
        revert on authorization).
      * allowed True  -> ready.
      * allowed None  -> unknown (couldn't determine).
    NEVER raises."""
    if allowed is False:
        rc = (" (reason code %d)" % reason) if isinstance(reason, int) and reason else ""
        return {"grade": "blocked", "standard": "erc8226-rams",
                "signals": {"standard": "erc8226-rams", "mandate_allowed": False,
                            "reason_code": reason},
                "reasons": ["the agent's on-chain MANDATE does not permit this action%s "
                            "(ERC-8226 RAMS) -- the transfer would revert on authorization "
                            "(not eligibility)" % rc]}
    if allowed is True:
        return {"grade": "ready", "standard": "erc8226-rams",
                "signals": {"standard": "erc8226-rams", "mandate_allowed": True},
                "reasons": ["agent mandate authorizes this action (ERC-8226 RAMS)"]}
    return {"grade": "unknown", "standard": "erc8226-rams", "signals": {}, "reasons": []}


class RamsReadinessSource:
    """Reads ERC-8226 `canExecute` from a mandate registry. DORMANT until a request
    carries `acquires.mandate_registry` (or an operator `default_registry`) AND the
    agent (payer) + principal are known. Same `check(...)` signature as the RWA sources
    -> drop into CombinedRwaReadinessSource. FAIL-OPEN; NEVER raises out of check().

    `transport` is the test/RPC seam: callable(to, data_hex) -> result_hex | None."""

    def __init__(self, rpc_url=None, timeout=2.5, transport=None, default_registry=None):
        self.rpc_url = (rpc_url or "").rstrip("/")
        self.timeout = timeout
        self._transport = transport
        self.default_registry = default_registry if is_evm_address(default_registry) else None

    def check(self, acquires, payer=None, counterparty=None):
        if not isinstance(acquires, dict):
            return None
        registry = acquires.get("mandate_registry") or self.default_registry
        principal = acquires.get("principal")
        asset = acquires.get("token")
        # DORMANT unless a registry is advertised AND we have agent + principal + asset.
        if not (is_evm_address(registry) and is_evm_address(payer)
                and is_evm_address(principal) and is_evm_address(asset)):
            return None
        action = acquires.get("action")
        if not (isinstance(action, str) and action.lower().startswith("0x")):
            action = _DEFAULT_ACTION
        amount = acquires.get("mandate_amount", 0)
        data = encode_can_execute(payer, principal, asset, action, amount)
        allowed, reason = decode_allowed_reason(self._eth_call(registry, data))
        if allowed is None:
            return None                              # couldn't read -> stay dormant/no-op
        return assess_mandate(allowed, reason)

    def _eth_call(self, to, data):
        try:
            if self._transport is not None:
                return self._transport(to, data)
            return self._json_rpc(to, data)
        except Exception:
            return None

    def _json_rpc(self, to, data):
        import json
        import urllib.request
        if not self.rpc_url:
            return None
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": to, "data": data}, "latest"]}
        req = urllib.request.Request(
            self.rpc_url, data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json",
                     "user-agent": "Blackwall-rams/1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            d = json.loads(r.read(1 << 16))
        res = d.get("result") if isinstance(d, dict) else None
        return res if isinstance(res, str) else None
