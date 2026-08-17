#!/usr/bin/env python3
"""
rwa_readiness.py -- pre-trade TRANSFER-RESTRICTION readiness for tokenized RWAs.

Blackwall's core verdict scores the COUNTERPARTY being paid (an address) and the
USDC/stablecoin payment leg. But when an agent pays stablecoins to ACQUIRE a
tokenized real-world asset -- a tokenized stock / treasury bill / fund share -- a
second, ASSET-SIDE question appears that a stablecoin-only x402 flow never raises:

    Will the security actually transfer to the agent's wallet, or REVERT?

Many tokenized securities are PERMISSIONED tokens (ERC-3643 / T-REX, ERC-1400, or a
simpler allowlist ERC-20): transfers are gated by an on-chain identity registry /
compliance module, and a transfer to a wallet that is not KYC-verified / whitelisted
(or that is frozen, or while the token is paused) REVERTS. An agent that signs the
USDC leg without checking this can:
  * pay and receive nothing, when mint/settlement is NON-ATOMIC, or
  * burn gas on a guaranteed revert, when the buy is an ATOMIC swap.
No stablecoin-only payment has this failure mode -- it is unique to buying restricted
RWAs, and it is the differentiated wedge here: Blackwall as the PRE-TRADE gate for
agents buying tokenized RWAs with stablecoins.

This module answers it pre-trade, mirroring readiness.py's design exactly:

  * `assess_transfer_readiness(probe)` -- PURE. Maps a normalized on-chain
    restriction probe -> {grade: ready|blocked|unknown, ...}. `blocked` = the
    transfer would fail; `unknown` = not a recognized permissioned token / could not
    determine (the COMMON case -- a plain ERC-20 wrapper is unrestricted, so absence
    of a restriction interface must read as "no gate", never as "blocked").
  * `apply_rwa_readiness(verdict, signal)` -- PURE fold. CONSERVATIVE-ONLY: escalate
    GO -> HOLD on `blocked`; NEVER upgrade a HOLD/STOP to GO; None/unknown-empty is a
    no-op. Same monotonic-toward-caution contract as apply_readiness.
  * `RwaReadinessSource` -- the live half. Given the acquired token + the receiving
    wallet (the request `payer`), eth_call the token's restriction interface and
    return the grade. Normalizes across the deployed permissioned-token standards:
      - ERC-3643 / T-REX -- `identityRegistry().isVerified(to)` (+ `isFrozen`/`paused`)
      - Securitize DS Protocol -- `preTransferCheck(from,to,value)` -> (code, reason)
      - ERC-1404 -- `detectTransferRestriction(from,to,value)` -> uint8 code
      - allowlist ERC-20 -- `isWhitelisted(to)` (+ `isFrozen`/`paused`)
    Multi-standard normalization (not the raw read, which is public) is the moat.
    FAIL-OPEN (any error -> unknown), OPT-IN (network on the hot path), transport
    injected for tests.

Two design rules, inherited from readiness.py + blockscout.py:

  1. FAIL OPEN. This is enrichment, not safety. An unreachable RPC, a plain ERC-20
     with no restriction methods, or junk data -> `unknown` and the verdict proceeds
     on Blackwall's own signals. (Contrast sanctions.py, which fails CLOSED.)
  2. HARD BOUNDARY -- SETTLEMENT-SAFETY, NOT COMPLIANCE. `blocked` may only push a
     would-be GO to HOLD (REVIEW). It NEVER produces or clears a STOP, and it never
     touches hard_stop. A token contract's own freeze/whitelist is NOT a sanctions
     source -- OFAC screening (sanctions.py) stays the STOP authority. (We escalate
     to HOLD, not STOP, deliberately: we cannot tell an atomic swap -- a revert only
     wastes gas -- from a non-atomic settle, and a mis-read interface must not hard-
     block a legit buy. A human/spending-cap layer confirms eligibility.)

Stdlib only (reuses keccak.py for the 4-byte selector + addresses.py).
"""
from __future__ import annotations

from addresses import is_evm_address
from keccak import keccak256

RWA_GRADES = ("ready", "blocked", "unknown")

# `standard` hints (from the request `acquires.standard`, case/format-insensitive)
# that DECLARE a token permissioned -- so that "we could not check" is surfaced as an
# actionable unknown rather than silently ignored.
_PERMISSIONED_HINTS = {
    "erc3643", "erc-3643", "t-rex", "trex", "erc1400", "erc-1400",
    "erc1404", "erc-1404", "permissioned", "restricted", "security-token",
}


# ===========================================================================
# Pure verdict logic
# ===========================================================================
def assess_transfer_readiness(probe):
    """PURE: map a normalized restriction `probe` -> a readiness signal dict.

    `probe` (all fields optional, None = "did not determine"):
      {standard, receiver_verified: bool|None, receiver_frozen: bool|None,
       paused: bool|None, can_receive: bool|None, reason_code: str|None}

    Returns {"grade", "standard", "reasons": [str], "signals": {...}}.
      * grade "blocked" -- ANY definitive failure signal is present (transfer would
        revert). Blocking DOMINATES a positive (verified but frozen -> blocked).
      * grade "ready"   -- a definitive positive (verified / can_receive) and NO
        blocking signal.
      * grade "unknown" -- nothing definitive (the plain-ERC-20 / no-interface case).
    NEVER raises."""
    if not isinstance(probe, dict):
        return {"grade": "unknown", "standard": None, "reasons": [], "signals": {}}

    std = probe.get("standard")
    blocked = []
    # Each condition is checked with `is True` / `is False` so that None ("unknown")
    # never counts as a failure -- fail-open is structural, not incidental.
    if probe.get("paused") is True:
        blocked.append("token transfers are PAUSED on the contract")
    if probe.get("receiver_frozen") is True:
        blocked.append("the receiving wallet is FROZEN on the token contract")
    if probe.get("receiver_verified") is False:
        blocked.append("the receiving wallet is NOT KYC-verified / whitelisted in the "
                       "token's identity registry")
    if probe.get("can_receive") is False:
        code = probe.get("reason_code")
        blocked.append("the token's on-chain canTransfer check rejects this transfer"
                       + (" (code %s)" % code if code else ""))

    positive = (probe.get("receiver_verified") is True
                or probe.get("can_receive") is True)

    if blocked:
        grade, reasons = "blocked", blocked
    elif positive:
        grade = "ready"
        reasons = ["receiving wallet is verified/whitelisted for the acquired security"]
    else:
        grade, reasons = "unknown", []

    signals = {k: probe.get(k) for k in
               ("standard", "receiver_verified", "receiver_frozen", "paused",
                "can_receive", "reason_code")}
    return {"grade": grade, "standard": std, "reasons": reasons, "signals": signals}


def apply_rwa_readiness(verdict, signal):
    """PURE fold of an RWA transfer-readiness `signal` into a verdict dict.

    CONSERVATIVE-ONLY: escalates GO -> HOLD when the acquired security's transfer is
    `blocked`; NEVER upgrades a HOLD/STOP to GO; a None / non-dict / unrecognized-grade
    signal is a no-op. Returns a NEW dict (does not mutate the input). Records the grade
    under signals.rwa_transfer_readiness, clearly distinct from the counterparty
    (address-based) reputation signals -- different identifier, different question."""
    if not signal or not isinstance(signal, dict):
        return verdict
    grade = signal.get("grade")
    if grade not in RWA_GRADES:
        return verdict

    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    detail = signal.get("signals") if isinstance(signal.get("signals"), dict) else {}
    v["signals"]["rwa_transfer_readiness"] = dict(
        {"grade": grade, "standard": signal.get("standard")}, **detail)
    reasons = list(v.get("reasons") or [])
    sig_reasons = [r for r in (signal.get("reasons") or []) if isinstance(r, str)]

    if grade == "blocked":
        if sig_reasons:
            for r in sig_reasons:
                reasons.append(
                    "acquired RWA settlement risk: %s -- the security transfer to your "
                    "wallet would REVERT (you could pay stablecoins and receive nothing)"
                    % r)
        else:
            reasons.append("acquired RWA transfer would be rejected by the token's "
                           "on-chain restrictions -- the security would not reach your wallet")
        if v.get("verdict") == "GO":
            v["verdict"] = "HOLD"
            reasons.append("escalated GO->HOLD: the tokenized RWA you are buying cannot "
                           "transfer to your wallet as-is (KYC / whitelist / freeze / pause) "
                           "-- verify eligibility before signing the stablecoin payment")
    elif grade == "ready":
        reasons.append("acquired RWA transfer readiness: receiving wallet is "
                       "verified/whitelisted for this security")
    else:  # unknown -- surface only an actionable note, if the source supplied one
        for r in sig_reasons:
            reasons.append("acquired RWA readiness: %s" % r)

    v["reasons"] = reasons
    return v


# ===========================================================================
# ABI call helpers (stdlib; the minimal encode/decode the restriction reads need)
# ===========================================================================
def selector(signature):
    """4-byte function selector for a Solidity signature -> '0x' + 8 hex."""
    return "0x" + keccak256(signature.encode("ascii"))[:4].hex()


def _pad_word(hex_no0x):
    """Left-pad a hex fragment to a 32-byte (64-hex) ABI word."""
    return hex_no0x.rjust(64, "0")


def eth_call_data(signature, args=()):
    """Encode calldata for a view call with STATIC address/uint args -> 0x-hex.
    Only the arg types the restriction reads use (address) are needed here."""
    body = ""
    for a in args or ():
        if is_evm_address(a):
            body += _pad_word(a[2:].lower())
        else:
            # numeric (uint256) fallback -- accepts int or decimal string.
            try:
                body += _pad_word(format(int(str(a)), "x"))
            except (TypeError, ValueError):
                body += _pad_word("")
    return selector(signature) + body


def decode_bool(result):
    """Decode a 32-byte ABI bool return -> True/False, or None when the result is
    empty/undecodable/non-canonical.

    CRUCIAL (fail-open): an empty '0x' (method absent / reverted) decodes to None
    (unknown), NEVER False -- absence of a restriction is not a rejection. STRICT:
    only the canonical ABI bool words (0 -> False, 1 -> True) decode to a bool; any
    OTHER value (a token whose `paused()`/`isFrozen()` returns a non-bool word, e.g.
    a uint timestamp) decodes to None, NOT True -- so a quirky return can never
    manufacture a false 'blocked'."""
    if not isinstance(result, str):
        return None
    s = result[2:] if result.lower().startswith("0x") else result
    if len(s) < 64:                      # need a full word; "0x" / "" -> unknown
        return None
    try:
        val = int(s[:64], 16)
    except ValueError:
        return None
    return False if val == 0 else True if val == 1 else None


def decode_address(result):
    """Decode a 32-byte ABI address return -> '0x'+40hex, or None when empty /
    undecodable / the zero address (an unset registry pointer)."""
    if not isinstance(result, str):
        return None
    s = result[2:] if result.lower().startswith("0x") else result
    if len(s) < 64:
        return None
    addr = "0x" + s[24:64].lower()
    if not is_evm_address(addr) or int(addr, 16) == 0:
        return None
    return addr


def decode_uint(result):
    """Decode a 32-byte ABI uint return -> int, or None when empty/undecodable.
    Used for the ERC-1404 `detectTransferRestriction` RESTRICTION CODE (0 = no
    restriction; any other value = a coded reason the transfer would be rejected)."""
    if not isinstance(result, str):
        return None
    s = result[2:] if result.lower().startswith("0x") else result
    if len(s) < 64:
        return None
    try:
        return int(s[:64], 16)
    except ValueError:
        return None


def decode_code_and_reason(result):
    """Decode a `(uint256 code, string reason)` ABI return -> (code:int|None,
    reason:str|None). This is Securitize DS Protocol's `preTransferCheck` shape:
    code 0 = transfer allowed; nonzero = rejected, with a human reason string
    (e.g. "Wallet not in registry"). The reason is BEST-EFFORT -- a failure to
    decode the dynamic string leaves it None but keeps the (authoritative) code.
    NEVER raises."""
    if not isinstance(result, str):
        return (None, None)
    s = result[2:] if result.lower().startswith("0x") else result
    if len(s) < 64:
        return (None, None)
    try:
        code = int(s[:64], 16)
    except ValueError:
        return (None, None)
    reason = None
    try:
        # word[1] = byte offset (from the start of the return data) to the string.
        off = int(s[64:128], 16) * 2                 # bytes -> hex-nibble index
        slen = int(s[off:off + 64], 16) * 2          # string length in nibbles
        raw = s[off + 64:off + 64 + slen]
        if len(raw) == slen:
            reason = bytes.fromhex(raw).decode("utf-8", "replace") or None
    except (ValueError, IndexError):
        reason = None
    return (code, reason)


def decode_string(result):
    """Decode a plain `string` ABI return -> str, or None. This is ERC-1404's
    `messageForTransferRestriction(uint8)` shape (offset, length, bytes). NEVER raises."""
    if not isinstance(result, str):
        return None
    s = result[2:] if result.lower().startswith("0x") else result
    if len(s) < 64:
        return None
    try:
        off = int(s[:64], 16) * 2                     # bytes -> hex-nibble index
        slen = int(s[off:off + 64], 16) * 2
        raw = s[off + 64:off + 64 + slen]
        if len(raw) == slen:
            return bytes.fromhex(raw).decode("utf-8", "replace") or None
    except (ValueError, IndexError):
        return None
    return None


# ===========================================================================
# Live source -- probe the token's restriction interface (OPT-IN, FAIL-OPEN)
# ===========================================================================
class RwaReadinessSource:
    """Probe a tokenized RWA's on-chain transfer restrictions for a given receiver.

    Reads only the RECEIVER-side gates (they need just the receiving wallet, so one
    address is enough): ERC-3643 `identityRegistry().isVerified(to)`, an allowlist
    `isWhitelisted(to)`, plus `isFrozen(to)` and `paused()`. ERC-1400's
    `canTransfer(...)` needs from/value/data and is out of this receiver-only spike
    (the pure assessor still consumes a `can_receive`/`reason_code` if a caller
    supplies one).

    FAIL-OPEN: any per-call failure / empty result leaves that field unknown; if
    NOTHING resolved (a plain ERC-20 with none of these methods, or our own network
    down) check() returns None -> the verdict is unaffected. `transport` is the test
    seam: callable(to_address:str, data_hex:str) -> result_hex:str | None (an
    eth_call). NEVER raises out of check()."""

    def __init__(self, rpc_url=None, timeout=2.5, transport=None, standard_hint=None):
        self.rpc_url = (rpc_url or "").rstrip("/")
        self.timeout = timeout
        self._transport = transport
        self.standard_hint = (standard_hint or "").strip().lower() or None

    def check(self, acquires, payer, counterparty=None):
        """Return an RWA readiness signal for acquiring `acquires`, or None (no-op).

        `acquires` = {token, chain?, standard?, amount?, seller?}; `payer` = the wallet
        that will RECEIVE the security (the request's `payer`). `counterparty` = the
        USDC recipient, used as a BEST-EFFORT token sender for the from-requiring
        standards (ERC-1404 / Securitize) when `acquires.seller` is absent."""
        if not isinstance(acquires, dict):
            return None
        token = acquires.get("token")
        if not is_evm_address(token):
            return None
        hint = (acquires.get("standard") or "").strip().lower() or self.standard_hint

        if not is_evm_address(payer):
            # We cannot check receiver-side gates without a receiver. Surface it ONLY
            # when the asset is DECLARED permissioned (else stay silent -- fail-open).
            if hint in _PERMISSIONED_HINTS:
                return {"grade": "unknown", "standard": hint, "signals": {},
                        "reasons": ["acquiring a permissioned RWA (%s) but no receiving "
                                    "wallet (`payer`) was supplied -- cannot verify the "
                                    "security transfer will not revert; pass `payer`"
                                    % hint]}
            return None

        # Best-effort token sender for from-requiring standards: an explicit
        # `acquires.seller` wins; else the USDC counterparty (in a direct issuer sale
        # the payee IS the token sender). None -> those standards are skipped.
        seller = acquires.get("seller")
        sender = seller if is_evm_address(seller) else (
            counterparty if is_evm_address(counterparty) else None)

        probe = self._probe(token, payer, sender)
        if probe is None:
            return None                       # nothing resolved -> unknown no-op
        return assess_transfer_readiness(probe)

    def _probe(self, token, payer, sender=None):
        probe = {"standard": None, "receiver_verified": None, "receiver_frozen": None,
                 "paused": None, "can_receive": None, "reason_code": None}

        # 1) Detect a recognized PERMISSIONED standard, SHORT-CIRCUITING on the first
        #    hit. If none matches the token is a plain (unrestricted) ERC-20 -> return
        #    None ("unknown"). Receiver-only standards (ERC-3643 / allowlist) always
        #    run; the from-requiring ones (Securitize / ERC-1404) run only when we have
        #    a `sender`. Order is cheapest-authoritative-first.
        #
        #  a) ERC-3643 / T-REX -- identity registry (receiver-only).
        registry = self._call_address(token, "identityRegistry()")
        if registry is not None:
            probe["standard"] = "erc3643"
            v = self._call_bool(registry, "isVerified(address)", payer)
            if v is not None:
                probe["receiver_verified"] = v
            self._add_freeze_pause(probe, token, payer)
            return probe

        #  b) Securitize DS Protocol -- preTransferCheck(from,to,value) -> (code,reason).
        #     Gas-free and publicly callable; a single comprehensive verdict.
        if sender is not None:
            res = self._eth_call(token, eth_call_data(
                "preTransferCheck(address,address,uint256)", (sender, payer, 0)))
            code, reason = decode_code_and_reason(res)
            if code is not None:
                probe["standard"] = "securitize"
                probe["can_receive"] = (code == 0)
                if code != 0:
                    probe["reason_code"] = reason or str(code)
                return probe

        #  c) ERC-1404 -- detectTransferRestriction(from,to,value) -> uint8 code.
        if sender is not None:
            code = decode_uint(self._eth_call(token, eth_call_data(
                "detectTransferRestriction(address,address,uint256)", (sender, payer, 0))))
            if code is not None:
                probe["standard"] = "erc1404"
                probe["can_receive"] = (code == 0)
                if code != 0:
                    msg = decode_string(self._eth_call(token, eth_call_data(
                        "messageForTransferRestriction(uint8)", (code,))))
                    probe["reason_code"] = msg or ("code %d" % code)
                return probe

        #  d) Allowlist-style ERC-20 (isWhitelisted on the token itself; receiver-only).
        w = self._call_bool(token, "isWhitelisted(address)", payer)
        if w is not None:
            probe["standard"] = "allowlist"
            probe["receiver_verified"] = w
            self._add_freeze_pause(probe, token, payer)
            return probe

        return None                           # plain ERC-20 -> unknown no-op

    def _add_freeze_pause(self, probe, token, payer):
        """Freeze + pause gates -- meaningful only for the field-based standards
        (ERC-3643 / allowlist); the code-based standards already fold these into
        their single restriction verdict."""
        frozen = self._call_bool(token, "isFrozen(address)", payer)
        if frozen is not None:
            probe["receiver_frozen"] = frozen
        paused = self._call_bool(token, "paused()")
        if paused is not None:
            probe["paused"] = paused

    # -- call primitives (fail-open) --
    def _call_bool(self, to, signature, addr_arg=None):
        args = (addr_arg,) if addr_arg is not None else ()
        return decode_bool(self._eth_call(to, eth_call_data(signature, args)))

    def _call_address(self, to, signature):
        return decode_address(self._eth_call(to, eth_call_data(signature)))

    def _eth_call(self, to, data):
        """One eth_call; returns the result hex or None. NEVER raises."""
        try:
            if self._transport is not None:
                return self._transport(to, data)
            return self._json_rpc_eth_call(to, data)
        except Exception:
            return None                       # FAIL OPEN

    def _json_rpc_eth_call(self, to, data):
        import json
        import urllib.request
        if not self.rpc_url:
            return None
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": to, "data": data}, "latest"]}
        req = urllib.request.Request(
            self.rpc_url, data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json",
                     "user-agent": "Blackwall-rwa-readiness/1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            d = json.loads(r.read(1 << 16))
        res = d.get("result") if isinstance(d, dict) else None
        return res if isinstance(res, str) else None


class CombinedRwaReadinessSource:
    """Dispatch an RWA readiness check across multiple sources (e.g. the EVM
    `RwaReadinessSource` + the Solana `solana_rwa.SolanaRwaReadinessSource`), returning
    the FIRST non-None signal. Each source self-selects by token format (EVM `0x...` vs
    base58 mint) and returns None when the token isn't its chain, so ordering is safe.
    Fail-open: a raising source is skipped. Lets `forecast`'s single `rwa_source` cover
    every chain."""

    def __init__(self, sources):
        self.sources = [s for s in (sources or []) if s is not None]

    def check(self, acquires, payer=None, counterparty=None):
        for s in self.sources:
            try:
                sig = s.check(acquires, payer, counterparty=counterparty)
            except Exception:
                sig = None
            if sig is not None:
                return sig
        return None


def is_rwa_descriptor(acquires):
    """DESCRIPTIVE: does the request carry a usable tokenized-RWA acquisition
    descriptor (an `acquires` object naming a token contract to receive)? Never
    gates on its own -- it only decides whether the readiness probe is worth running."""
    return isinstance(acquires, dict) and is_evm_address(acquires.get("token"))
