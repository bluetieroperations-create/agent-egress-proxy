#!/usr/bin/env python3
"""
calldata.py -- payload simulation Phase 3: screen a contract-call payment.

Phases 1/2 assume the payment is a gasless EIP-3009 `transferWithAuthorization`.
But an agent can instead be about to sign a RAW transaction that is an arbitrary
contract call -- and the dangerous ones don't move money to a payee at all, they
grant it away. The classic drainers:

  * `approve(spender, UNLIMITED)` / `increaseAllowance` / EIP-2612 `permit` with an
    effectively-infinite allowance -- hands `spender` the keys to the token.
  * `setApprovalForAll(operator, true)` -- hands over EVERY NFT in a collection.
  * `transfer(to, ...)` / `transferFrom(.., to, ..)` whose `to` is NOT the
    counterparty the agent says it is paying.

This module decodes the 4-byte selector (keccak256 of the signature) + the static
ABI args and flags those patterns against the claim. A CRITICAL finding is a hard
STOP ("this is not the payment you described"); a WARN is advisory (an approval is
never itself a payment; an unrecognized call can't be verified). Pure + stdlib
(keccak.py + addresses.py). Static types only (address/uint256/bool) -- exactly
what the screened functions use.

LIMITATIONS (audited & accepted):
  * "Unlimited" threshold is 2**128 -- catches the dominant infinite-approval
    conventions (uint256-max, uint160-max/Permit2) while never false-flagging a
    real payment. A rare uint96-max "infinite" (~7.9e28) falls to WARN, not STOP.
  * Bounded approvals / setApprovalForAll(false) / unrecognized selectors are
    advisory WARNINGS, not blocks -- an approval is suspicious-as-a-payment but not
    a certain drain, and we don't screen the spender's reputation here (the
    counterparty sanctions/known-bad path already covers addresses).
  * The transfer/transferFrom AMOUNT cross-check assumes USDC 6dp and runs only
    when the called token (tx.to) IS the claimed asset (else decimals are unknown);
    it errs SAFE. Recipient is always checked.
  * We screen the CALLDATA, not the target contract's bytecode -- a malicious
    implementation behind a benign-looking selector is out of scope (Phase 3 is the
    calldata lane, not full simulation).
"""
from __future__ import annotations

from addresses import addresses_equal, is_evm_address
from keccak import keccak256
from x402 import to_atomic

# An allowance at/above this is treated as "effectively unlimited" (covers the
# uint256-max and uint160-max "infinite" conventions; dwarfs any real payment).
UNLIMITED_MIN = 1 << 128


def selector(signature: str) -> str:
    """4-byte function selector for a Solidity signature -> '0x' + 8 hex."""
    return "0x" + keccak256(signature.encode("ascii"))[:4].hex()


# Screened functions: selector -> (name, [arg types]). Static types only.
_FUNCTIONS = {
    selector("approve(address,uint256)"): ("approve", ["address", "uint256"]),
    selector("increaseAllowance(address,uint256)"):
        ("increaseAllowance", ["address", "uint256"]),
    selector("transfer(address,uint256)"): ("transfer", ["address", "uint256"]),
    selector("transferFrom(address,address,uint256)"):
        ("transferFrom", ["address", "address", "uint256"]),
    selector("setApprovalForAll(address,bool)"):
        ("setApprovalForAll", ["address", "bool"]),
    selector("permit(address,address,uint256,uint256,uint8,bytes32,bytes32)"):
        ("permit", ["address", "address", "uint256", "uint256",
                    "uint8", "bytes32", "bytes32"]),
}


def _to_bytes(data):
    """Accept a 0x-hex string or bytes -> bytes, or None if not decodable."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        s = data[2:] if data.lower().startswith("0x") else data
        try:
            return bytes.fromhex(s)
        except ValueError:
            return None
    return None


def _decode_word(typ, word):
    if typ == "address":
        return "0x" + word[-20:].hex()
    if typ == "bool":
        return int.from_bytes(word, "big") != 0
    return int.from_bytes(word, "big")            # uint*/bytes32-as-int


def decode_call(data):
    """Decode a contract-call payload -> {selector, name, args:{}} or a bare
    {selector, name:None, args:{}} for an unrecognized selector, or None if the
    data isn't even a 4-byte selector. NEVER raises."""
    raw = _to_bytes(data)
    if raw is None or len(raw) < 4:
        return None
    sel = "0x" + raw[:4].hex()
    spec = _FUNCTIONS.get(sel)
    if spec is None:
        return {"selector": sel, "name": None, "args": {}}
    name, types = spec
    body = raw[4:]
    words = [body[i:i + 32] for i in range(0, len(body), 32)]
    if len(words) < len(types):                   # truncated args
        return {"selector": sel, "name": name, "args": {}, "truncated": True}
    args = {}
    argnames = {
        "approve": ["spender", "amount"],
        "increaseAllowance": ["spender", "amount"],
        "transfer": ["to", "amount"],
        "transferFrom": ["from", "to", "amount"],
        "setApprovalForAll": ["operator", "approved"],
        "permit": ["owner", "spender", "value", "deadline", "v", "r", "s"],
    }[name]
    for i, typ in enumerate(types):
        args[argnames[i]] = _decode_word(typ, words[i])
    return {"selector": sel, "name": name, "args": args}


def screen_transaction(claim, transaction, *, unlimited_min=UNLIMITED_MIN):
    """Screen a contract-call payment against the claim.

    `transaction` = {to, data, value?} (the raw tx the agent is about to sign).
    Returns {"checked", "matches", "mismatches", "warnings"} -- same shape as
    payload_sim.check_payment_authorization, so it folds into the verdict the same
    way (mismatches -> hard STOP, warnings -> advisory). NEVER raises.
    """
    if not transaction:
        return {"checked": False, "matches": True, "mismatches": [], "warnings": []}
    tx = transaction if isinstance(transaction, dict) else {}
    claim = claim if isinstance(claim, dict) else {}
    cp = claim.get("counterparty")
    mismatches, warnings = [], []

    decoded = decode_call(tx.get("data"))
    if decoded is None:
        # no calldata: a plain-value transfer. If it targets the counterparty it's a
        # normal native payment; otherwise we simply have nothing to decode.
        return {"checked": True, "matches": True, "mismatches": [], "warnings": []}

    name = decoded.get("name")
    args = decoded.get("args", {})
    if decoded.get("truncated"):
        mismatches.append("contract call %s has truncated/garbled calldata -- "
                          "cannot verify what it does" % decoded["selector"])
        return {"checked": True, "matches": False,
                "mismatches": mismatches, "warnings": warnings}

    if name is None:
        warnings.append("contract call selector %s is unrecognized -- Blackwall "
                        "cannot verify this is the payment you described"
                        % decoded["selector"])

    elif name in ("approve", "increaseAllowance"):
        amt = args.get("amount", 0)
        spender = args.get("spender")
        if amt >= unlimited_min:
            mismatches.append(
                "UNLIMITED token approval to %s -- grants unbounded spending of your "
                "tokens (drainer pattern), not a payment" % _show(spender))
        else:
            warnings.append(
                "this is an %s granting %s an allowance of %s -- an approval is not "
                "itself a payment" % (name, _show(spender), amt))

    elif name == "permit":
        amt = args.get("value", 0)
        spender = args.get("spender")
        if amt >= unlimited_min:
            mismatches.append(
                "UNLIMITED permit approval to %s -- gasless unbounded allowance "
                "(drainer pattern), not a payment" % _show(spender))
        else:
            warnings.append("this is a permit granting %s an allowance of %s -- not "
                            "itself a payment" % (_show(spender), amt))

    elif name == "setApprovalForAll":
        if args.get("approved") is True:
            mismatches.append(
                "setApprovalForAll(%s, true) -- grants control of ALL your NFTs in "
                "this collection, not a payment" % _show(args.get("operator")))

    elif name in ("transfer", "transferFrom"):
        to = args.get("to")
        if cp and to and not addresses_equal(to, cp):
            mismatches.append(
                "contract %s sends tokens to %s but you asked me to score paying %s "
                "(recipient mismatch)" % (name, _show(to), _show(cp)))
        elif cp and to and is_evm_address(cp) and not is_evm_address(to):
            warnings.append("contract %s recipient %s is not a valid address"
                            % (name, _show(to)))
        # amount cross-check -- only when the token being called (tx.to) IS the
        # claimed asset, so the decimals are known (USDC 6dp; see LIMITATIONS).
        amt = args.get("amount")
        asset, tx_to = claim.get("asset"), tx.get("to")
        if amt is not None and is_evm_address(asset) and tx_to \
                and addresses_equal(tx_to, asset):
            want = to_atomic(claim.get("amount"), 6)
            if want is not None and amt != want:
                mismatches.append(
                    "contract %s moves %s atomic units but you asked me to score %s "
                    "(amount mismatch)" % (name, amt, want))

    return {"checked": True, "matches": not mismatches,
            "mismatches": mismatches, "warnings": warnings}


def _show(v):
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "..."
