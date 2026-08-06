#!/usr/bin/env python3
"""
secret_scan.py -- detect leaked SECRETS / PII in an x402 payment payload before the
agent signs it. An x402 payment (and any memo/metadata it carries) settles ON-CHAIN --
public and irreversible -- so a private key, seed phrase, or API key that leaks into a
payment field is catastrophic and cannot be undone. This is the payload-hygiene layer
(a common vector: a prompt-injected agent exfiltrating a credential inside a payment).

╔══════════════════════════════════════════════════════════════════════════════════╗
║ TWO HARD RULES:                                                                     ║
║  1. NEVER echo, log, or persist the matched secret. Findings carry a TYPE, a FIELD, ║
║     and a REDACTED hint (first/last few chars) only -- never the value.             ║
║  2. Scan FREE-TEXT fields only, NOT the structural crypto fields (counterparty,     ║
║     asset, tx `to`/`data`, signatures, hashes). Those legitimately hold 64-hex and  ║
║     addresses -- scanning them would flag every tx hash as a "private key". Avoiding ║
║     that false-positive is the whole game (a false STOP blocks a legit payment).    ║
╚══════════════════════════════════════════════════════════════════════════════════╝

Severity -> verdict (folded in blackwall.decide_payment):
  * HIGH  (private key, seed phrase, cloud/API credential) -> STOP. Signing would
    publish a live credential on-chain; non-negotiable.
  * MEDIUM (SSN / other PII, or a bare 0x-64-hex in free text) -> HOLD (review): lower
    stakes or higher false-positive risk, so escalate to a human rather than block.

PURE + stdlib-only. `scan_payload(payload)` walks the request body; `scan_text(s)` is
the single-string primitive both it and the tests use.

KNOWN LIMITATIONS (conservative by design -- a false STOP blocks a legit payment):
  * A private key as bare 64-hex with NO `0x` prefix and NO nearby key/seed LABEL is
    NOT flagged -- indistinguishable from a hash without context. Labeled ones and
    0x-prefixed ones ARE caught. (A bare 64-hex under a STRUCTURAL field is skipped for
    the same reason; a *credential* like AKIA/sk-/ghp_ hidden there IS caught, since
    those never collide with hex.)
  * The BIP-39 detector is a SHAPE heuristic (stdlib-only, no 2048-word list) -> MEDIUM
    (HOLD), never a hard STOP, so a rare false positive escalates rather than blocks.
  * Detection is pattern-based; a novel credential format, or one obfuscated with
    unicode/zero-width chars, is a miss (false-negative), never a false alarm.
"""
from __future__ import annotations

import re

# Structural crypto/protocol fields that legitimately hold hex, addresses, signatures,
# and hashes -- scanning them would false-positive on every tx hash / calldata blob.
# Matched case-insensitively against the JSON key.
STRUCTURAL_KEYS = frozenset({
    "counterparty", "payer", "expected_recipient", "recipient", "payto", "pay_to",
    "asset", "chain", "network", "amount", "resource_class", "payment_authorization",
    "transaction", "nonce", "hash", "txhash", "tx_hash", "signature", "sig", "r", "s",
    "v", "to", "from", "data", "value", "token", "address", "contract", "digest",
    "receipt_id", "id",
})

# HIGH-precision credential patterns. Distinctive prefixes/structure -> ~0 false pos,
# and crucially they NEVER match a bare hex hash or an EVM address -- so they are safe to
# run on EVERY field, including the structural crypto fields (a credential hidden under a
# key named "data" is still caught, without flagging a tx hash). (name, severity, regex).
_HIGH_PATTERNS = [
    ("aws_access_key_id", "high", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_pat", "high", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("github_fine_grained_pat", "high", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("slack_token", "high", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", "high", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    # Stripe/OpenAI/Anthropic keys. A KNOWN sub-prefix (live_/test_/proj-/ant-) needs
    # only a 16+ tail; a BARE `sk-<blob>` needs 40+ chars (real keys are ~48) so it does
    # NOT flag short hyphenated SKUs / promo codes like "sk-SUMMERSALE2024" (false STOP).
    ("api_secret_key", "high", re.compile(
        r"\bsk[-_](?:(?:live|test|proj|ant)[-_][A-Za-z0-9_-]{16,}|[A-Za-z0-9]{40,})\b")),
    ("private_key_pem", "high",
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
]

# AMBIGUOUS / lower-precision -- FREE-TEXT fields ONLY (they WOULD false-positive on the
# hex/addresses that legitimately fill structural fields). All MEDIUM -> HOLD, not STOP.
_MEDIUM_PATTERNS = [
    ("ssn", "medium", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
]

# A 0x-prefixed 32-byte hex in FREE TEXT (not a structural field) -- likely an EVM
# private key. Structural fields that hold 64-hex legitimately are excluded by field, so
# here it is suspicious. MEDIUM (a bare hash pasted into a memo is possible) -> HOLD.
_HEX64 = re.compile(r"\b0x[0-9a-fA-F]{64}\b")
# A private-key/seed LABEL adjacent to a long hex -> HIGH (unambiguous intent).
_LABELED_KEY = re.compile(
    r"(?i)(?:private[\s_-]?key|priv[\s_-]?key|secret[\s_-]?key|seed|mnemonic|"
    r"recovery[\s_-]?phrase)\b[\s:=\"']{0,6}(?:0x)?[0-9a-fA-F]{64}\b")
# BIP-39 mnemonic SHAPE: exactly 12/15/18/21/24 all-lowercase 3-8 letter words. No
# wordlist (stdlib-only), so it is a heuristic -> MEDIUM (HOLD), not a hard STOP.
_MNEMONIC_LEN = {12, 15, 18, 21, 24}
_WORD = re.compile(r"^[a-z]{3,8}$")


def _redact(s):
    """A safe hint that identifies WHERE without revealing the secret."""
    s = str(s)
    if len(s) <= 8:
        return s[0] + "***" if s else "***"
    return "%s***%s (len %d)" % (s[:4], s[-2:], len(s))


def scan_text(text, *, field="text", high_only=False):
    """Scan ONE string for secrets/PII. Returns findings [{type, severity, field, hint}];
    never the raw secret. PURE. `high_only` (used for structural fields) runs ONLY the
    HIGH-precision credential patterns -- the ones that never false-positive on hex -- so
    a credential hidden in a structural field is caught while a tx hash is not flagged."""
    if not isinstance(text, str) or not text:
        return []
    out = []
    seen = set()

    def _add(kind, severity, match):
        key = (kind, match)
        if key in seen:
            return
        seen.add(key)
        out.append({"type": kind, "severity": severity, "field": field,
                    "hint": _redact(match)})

    # HIGH-precision credential patterns -- run on ANY field.
    for name, severity, rx in _HIGH_PATTERNS:
        for m in rx.findall(text):
            _add(name, severity, m if isinstance(m, str) else m[0])
    for m in _LABELED_KEY.findall(text):
        _add("private_key_labeled", "high", m if isinstance(m, str) else text)

    if high_only:
        return out

    # AMBIGUOUS checks -- FREE-TEXT fields only (would false-positive on structural hex).
    for name, severity, rx in _MEDIUM_PATTERNS:
        for m in rx.findall(text):
            _add(name, severity, m if isinstance(m, str) else m[0])
    for m in _HEX64.findall(text):
        if not any(f["type"] == "private_key_labeled" for f in out):
            _add("hex64_in_freetext", "medium", m)
    words = text.strip().split()
    if len(words) in _MNEMONIC_LEN and all(_WORD.match(w) for w in words):
        _add("seed_phrase_shape", "medium", " ".join(words[:2]) + " …")

    return out


def scan_payload(payload, *, structural_keys=STRUCTURAL_KEYS):
    """Walk an x402 request body and scan every string value. FREE-TEXT fields get the
    full scan; STRUCTURAL crypto fields (which legitimately hold hex/addresses) get only
    the HIGH-precision credential patterns -- so a credential HIDDEN in a structural field
    is caught, but a tx hash / address / calldata is never mis-flagged. Recurses into
    nested dicts/lists (a field named `data`/`value` at any depth is treated structurally).
    Returns a flat list of findings. PURE."""
    findings = []

    def _walk(node, keypath):
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, str(k))
        elif isinstance(node, (list, tuple)):
            for it in node:
                _walk(it, keypath)
        elif isinstance(node, str):
            high_only = keypath.lower() in structural_keys
            findings.extend(scan_text(node, field=keypath, high_only=high_only))

    _walk(payload, "payload")
    return findings


def top_severity(findings):
    """'high' if any finding is high, else 'medium' if any, else None."""
    sev = {f.get("severity") for f in (findings or [])}
    return "high" if "high" in sev else ("medium" if "medium" in sev else None)


def redacted_summary(findings):
    """A human/reason-safe one-liner: the TYPES found, never the values."""
    types = sorted({f.get("type") for f in (findings or [])})
    return ", ".join(types)
