#!/usr/bin/env python3
"""
solana_rwa.py -- the SOLANA leg of the tokenized-RWA transfer-restriction readiness
gate. The SPL Token-2022 analogue of rwa_readiness.py's EVM `eth_call` reads.

A lot of tokenized-stock volume (Backed/xStocks, Ondo) settles on SOLANA as SPL
**Token-2022** mints, not EVM ERC-20s -- so the EVM probe can't see them. Token-2022
carries per-transfer restrictions as MINT EXTENSIONS (TLV-encoded in the account
data) plus the token account's own frozen state:

  * DefaultAccountState = Frozen -- new holder accounts are frozen until an authority
    thaws them (a permissioning/allowlist gate; an unthawed wallet can't use tokens).
  * TransferHook -- every transfer must be approved by an external program (can
    enforce KYC/allowlist); eligibility can't be predicted off-chain.
  * NonTransferable -- soulbound; a secondary transfer reverts.
  * PermanentDelegate -- an authority can move/burn your tokens (clawback risk).
  * Account state = Frozen -- this specific receiving account is frozen.

`assess_solana_readiness` maps these to the SAME signal shape as
rwa_readiness.assess_transfer_readiness ({grade, standard, reasons, signals}), so it
folds through `rwa_readiness.apply_rwa_readiness` unchanged (blocked -> GO->HOLD;
unknown-with-reason -> surfaced note; conservative-only, fail-open).

Pure parsers + base58 + an injected-RPC live source. Stdlib only.

LIMITATIONS (audited & accepted):
  * Receiver-account frozen check needs the receiver's ASSOCIATED TOKEN ACCOUNT
    address. Auto-deriving it (an off-curve program-derived address) is a separable
    crypto primitive and is DEFERRED -- pass `acquires.receiver_token_account` to
    enable the per-wallet frozen read; without it we grade the MINT-level restriction
    only (default-frozen/hook/non-transferable/permanent-delegate), which is already
    the dominant permissioning signal.
  * A TransferHook can allow OR deny a given receiver; we cannot execute it
    off-chain, so a hook is a CAUTION (unknown + surfaced), never a hard block.
"""
from __future__ import annotations

import base64

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Token-2022 extension type discriminators (u16 LE) we care about.
_EXT_DEFAULT_ACCOUNT_STATE = 6
_EXT_NON_TRANSFERABLE = 9
_EXT_PERMANENT_DELEGATE = 12
_EXT_TRANSFER_HOOK = 14

_ACCOUNT_TYPE_MINT = 1          # the byte at offset 165 for an extended mint
_MINT_BASE_LEN = 82
_ACCOUNT_STATE_FROZEN = 2       # token-account state byte (offset 108); 0 uninit,1 init,2 frozen
_DEFAULT_STATE_FROZEN = 2       # DefaultAccountState value


def b58decode(s):
    """Base58 (Bitcoin/Solana alphabet) -> bytes, or None. NEVER raises."""
    if not isinstance(s, str) or not s:
        return None
    num = 0
    for ch in s:
        idx = _B58.find(ch)
        if idx < 0:
            return None
        num = num * 58 + idx
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


def b58encode(raw):
    """bytes -> base58 string."""
    if not isinstance(raw, (bytes, bytearray)):
        return None
    num = int.from_bytes(raw, "big")
    out = ""
    while num > 0:
        num, rem = divmod(num, 58)
        out = _B58[rem] + out
    pad = len(raw) - len(bytes(raw).lstrip(b"\x00"))
    return "1" * pad + out


def is_solana_address(s):
    """True if `s` decodes to a 32-byte base58 pubkey (and is not an EVM 0x address)."""
    if not isinstance(s, str) or s.startswith("0x"):
        return False
    raw = b58decode(s)
    return raw is not None and len(raw) == 32


def parse_mint_extensions(raw):
    """PURE: parse an SPL Token-2022 MINT account's TLV extensions -> restriction dict.

    A legacy/plain mint (<= base length, or no extension area) -> all-False/None
    (unrestricted). NEVER raises. Returns:
      {is_token2022, default_account_state: int|None, non_transferable,
       transfer_hook, permanent_delegate}."""
    out = {"is_token2022": False, "default_account_state": None,
           "non_transferable": False, "transfer_hook": False,
           "permanent_delegate": False}
    if not isinstance(raw, (bytes, bytearray)):
        return out
    raw = bytes(raw)
    # Extensions live after the account-type byte at offset 165; a plain 82-byte mint
    # (or anything without that area) has none.
    if len(raw) <= 166 or raw[165] != _ACCOUNT_TYPE_MINT:
        return out
    out["is_token2022"] = True
    i, n = 166, len(raw)
    while i + 4 <= n:
        etype = int.from_bytes(raw[i:i + 2], "little")
        elen = int.from_bytes(raw[i + 2:i + 4], "little")
        i += 4
        if etype == 0:                      # uninitialized -> end of TLV
            break
        val = raw[i:i + elen]
        if etype == _EXT_DEFAULT_ACCOUNT_STATE and len(val) >= 1:
            out["default_account_state"] = val[0]
        elif etype == _EXT_NON_TRANSFERABLE:
            out["non_transferable"] = True
        elif etype == _EXT_PERMANENT_DELEGATE:
            out["permanent_delegate"] = True
        elif etype == _EXT_TRANSFER_HOOK:
            out["transfer_hook"] = True
        i += elen
    return out


def parse_token_account(raw):
    """PURE: parse an SPL token ACCOUNT -> {mint, owner, amount, frozen, initialized},
    or None if too short. State byte at offset 108: 2 = Frozen. NEVER raises."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 109:
        return None
    raw = bytes(raw)
    state = raw[108]
    return {"mint": b58encode(raw[0:32]), "owner": b58encode(raw[32:64]),
            "amount": int.from_bytes(raw[64:72], "little"),
            "frozen": state == _ACCOUNT_STATE_FROZEN, "initialized": state != 0}


def assess_solana_readiness(mint_ext, account=None):
    """PURE: map parsed Token-2022 mint extensions (+ optional receiver account state)
    -> a readiness signal in rwa_readiness's shape {grade, standard, reasons, signals}.

    Grade precedence: blocked > caution(unknown) > ready > unknown-empty.
      * blocked -- non-transferable, receiver account frozen, or default-frozen with
        no confirmed thaw (a fresh receiver can't use the tokens).
      * unknown+reason -- a transfer hook or permanent delegate (can't predict / risk).
      * ready -- default-frozen but the receiver account is present and NOT frozen
        (explicitly thawed/whitelisted).
    NEVER raises."""
    me = mint_ext if isinstance(mint_ext, dict) else {}
    blocked, cautions = [], []

    if me.get("non_transferable"):
        blocked.append("token is NON-TRANSFERABLE (soulbound) -- a secondary transfer reverts")
    if account and account.get("frozen"):
        blocked.append("the receiving token account is FROZEN")

    default_frozen = me.get("default_account_state") == _DEFAULT_STATE_FROZEN
    if default_frozen and account is None:
        blocked.append("token FREEZES new accounts by default -- your wallet's token "
                       "account must be explicitly thawed/whitelisted or the received "
                       "tokens are unusable")
    receiver_thawed = default_frozen and account is not None and not account.get("frozen")

    if me.get("transfer_hook"):
        cautions.append("transfers are gated by an on-chain transfer-hook program -- "
                        "eligibility can't be predicted off-chain; verify with the issuer")
    if me.get("permanent_delegate"):
        cautions.append("a permanent delegate can move or burn your tokens without consent")

    if blocked:
        grade, reasons = "blocked", blocked + cautions
    elif cautions:                          # hook/delegate uncertainty overrides a bare ready
        grade, reasons = "unknown", cautions
    elif receiver_thawed:
        grade = "ready"
        reasons = ["receiving token account is present and not frozen (thawed/whitelisted)"]
    else:
        grade, reasons = "unknown", []

    return {"grade": grade, "standard": "spl-token-2022", "reasons": reasons,
            "signals": {"standard": "spl-token-2022",
                        "default_account_state": me.get("default_account_state"),
                        "non_transferable": bool(me.get("non_transferable")),
                        "transfer_hook": bool(me.get("transfer_hook")),
                        "permanent_delegate": bool(me.get("permanent_delegate")),
                        "receiver_frozen": (account.get("frozen") if account else None)}}


class SolanaRwaReadinessSource:
    """Live SPL Token-2022 restriction reader. Given the acquired mint (`acquires.token`
    on a Solana chain) and optionally the receiver's token account
    (`acquires.receiver_token_account`), read the mint extensions + account state and
    return a readiness signal that folds through rwa_readiness.apply_rwa_readiness.

    FAIL-OPEN (any error / plain mint -> None no-op). `rpc` is the test seam:
    callable(method, params) -> the JSON-RPC `result` object (or None). NEVER raises
    out of check()."""

    def __init__(self, rpc_url=None, timeout=2.5, rpc=None):
        self.rpc_url = (rpc_url or "").rstrip("/")
        self.timeout = timeout
        self._rpc = rpc

    def check(self, acquires, payer=None, counterparty=None):
        if not isinstance(acquires, dict):
            return None
        token = acquires.get("token")
        if not is_solana_address(token):
            return None                      # not a Solana mint -> let the EVM path handle it
        mint_raw = self._account_data(token)
        if mint_raw is None:
            return None                      # unreachable / no data -> unknown no-op
        mint_ext = parse_mint_extensions(mint_raw)
        if not mint_ext.get("is_token2022"):
            return None                      # plain SPL token, no extensions -> no-op

        account = None
        ata = acquires.get("receiver_token_account")
        if is_solana_address(ata):
            acct_raw = self._account_data(ata)
            if acct_raw is not None:
                account = parse_token_account(acct_raw)
        return assess_solana_readiness(mint_ext, account)

    def _account_data(self, pubkey):
        """getAccountInfo(pubkey, base64) -> raw account bytes, or None. FAIL-OPEN."""
        try:
            result = self._call("getAccountInfo",
                                 [pubkey, {"encoding": "base64"}])
            value = result.get("value") if isinstance(result, dict) else None
            data = value.get("data") if isinstance(value, dict) else None
            b64 = data[0] if isinstance(data, list) and data else None
            return base64.b64decode(b64) if isinstance(b64, str) else None
        except Exception:
            return None

    def _call(self, method, params):
        if self._rpc is not None:
            return self._rpc(method, params)
        return self._json_rpc(method, params)

    def _json_rpc(self, method, params):
        import json
        import urllib.request
        if not self.rpc_url:
            return None
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        req = urllib.request.Request(
            self.rpc_url, data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json",
                     "user-agent": "Blackwall-solana-rwa/1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            d = json.loads(r.read(1 << 17))
        return d.get("result") if isinstance(d, dict) else None
