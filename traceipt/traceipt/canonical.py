"""
Canonical JSON + hashing.

The signature and the hash chain are only meaningful if the same receipt
always serializes to the same bytes. We restrict the value domain so that
Python's json module IS a canonical serializer:

  * allowed types: dict (str keys), list, str, int, bool, None
  * floats are REJECTED (canonical float serialization is where JCS/RFC 8785
    implementations disagree; money is represented as base-unit int strings)
  * keys sorted, no whitespace, UTF-8, non-ASCII passed through

Within that domain the output matches RFC 8785 (JCS).
"""
from __future__ import annotations

import hashlib
import json

HASH_PREFIX = "sha256:"
GENESIS_HASH = HASH_PREFIX + "0" * 64


def _check_domain(obj, path="$"):
    if obj is None or isinstance(obj, (str, bool)):
        return
    if isinstance(obj, int):
        # bool is a subclass of int; already handled above.
        return
    if isinstance(obj, float):
        raise ValueError(f"float not allowed in canonical JSON at {path} "
                         "(represent amounts as base-unit integer strings)")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ValueError(f"non-string key {k!r} at {path}")
            _check_domain(v, f"{path}.{k}")
        return
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            _check_domain(v, f"{path}[{i}]")
        return
    raise ValueError(f"type {type(obj).__name__} not allowed at {path}")


def canonical_json(obj) -> bytes:
    """Serialize obj to canonical JSON bytes. Raises ValueError on floats
    or other non-canonical types."""
    _check_domain(obj)
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return HASH_PREFIX + hashlib.sha256(data).hexdigest()


def hash_obj(obj) -> str:
    """Canonical hash of a JSON-domain object, as 'sha256:<hex>'."""
    return sha256_hex(canonical_json(obj))
