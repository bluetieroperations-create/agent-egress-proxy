#!/usr/bin/env python3
"""
addresses.py -- EVM address validation + normalization (pure, stdlib-only).

Used wherever Blackwall compares or binds an on-chain identity: the recipient
mismatch check in the verdict, and the PAYER binding that makes settlement
confirmation agent-specific (settlement_watch).

We validate FORMAT (`0x` + 40 hex) and normalize by lowercasing. We deliberately
do NOT verify the EIP-55 mixed-case checksum: that needs keccak256, which is NOT
in the Python stdlib (`hashlib.sha3_256` is NIST SHA3 -- a *different* function
from Ethereum's keccak256), and this repo is stdlib-only. Case-insensitive
comparison is the correct basis for address equality regardless; the EIP-55
checksum is only an integrity hint, not the identity.
"""
import re

# \A ... \Z (not ^ ... $): Python's `$` also matches just before a trailing
# newline, so "0x<40hex>\n" would wrongly validate -- and the newline would then
# be stored and silently break the on-chain sender comparison. \Z anchors the
# absolute end of string.
_EVM_RE = re.compile(r"\A0x[0-9a-fA-F]{40}\Z")


def is_evm_address(s):
    """True iff `s` is a well-formed EVM address (`0x` + 40 hex chars)."""
    return isinstance(s, str) and _EVM_RE.match(s) is not None


def normalize_address(s):
    """Lowercased address if `s` is a valid EVM address, else None."""
    if not is_evm_address(s):
        return None
    return s.lower()


def addresses_equal(a, b):
    """
    Case-insensitive equality for on-chain identities.

    Two representations of the same address (e.g. an EIP-55 checksummed form and
    its lowercase form) ARE equal -- comparing them case-sensitively is a bug
    (it would flag a legitimate recipient as a mismatch). Non-strings, or a None,
    are never equal.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return a.lower() == b.lower()
