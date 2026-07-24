#!/usr/bin/env python3
"""
x402_budget.py -- pure, testable security-boundary functions for the x402
spend guardrail ("the third lock"). See docs/budget-proxy-design.md.

This module is the MONEY analog of the pure functions at the top of
egress_proxy.py (parse_connect_target / host_allowed / decide): it contains
NO sockets and NO I/O in the decision path, so every branch is unit-testable
in isolation. Stdlib only.

Philosophy carried over verbatim from the network lock:
  * FAIL CLOSED. An offer we cannot parse, an asset we don't recognize, a
    payment we can't price -> BLOCK (in enforcement truth), never a silent pass.
  * NO FLOATS ON MONEY. Every amount is compared as an integer in atomic units
    (e.g. USDC 6-decimals). Decimal policy strings are converted to atomic ints
    exactly once, at load time.
  * "NO SILENT SPEND" -- the caller logs every payment attempt (allow / block /
    prompt), mirroring the "no silent egress" invariant.

budget_decide() computes ENFORCEMENT TRUTH (what an enforcing proxy would do).
The caller decides whether to act on it: in budget-mode `observe` it logs the
decision and forwards anyway; in `enforce` it blocks/holds. Same split as the
existing observe/enforce modes.
"""
from __future__ import annotations

import base64
import binascii
import json
from collections import namedtuple

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
MAX_OFFER_BYTES = 64 * 1024        # cap a 402 body we will parse (oversize guard)
MAX_PAYMENT_HEADER_BYTES = 32 * 1024  # cap an X-PAYMENT header we will decode
DAY_SECONDS = 86400

# ---------------------------------------------------------------------------
# Types (immutable value objects, like the (host, port) tuple in the net lock)
# ---------------------------------------------------------------------------
# amount_atomic: int, in atomic units of `asset`. host/path parsed from resource.
Offer = namedtuple("Offer",
                   ["amount_atomic", "asset", "network", "pay_to",
                    "resource", "host", "path"])
Payment = namedtuple("Payment", ["amount_atomic", "asset", "network"])
Decision = namedtuple("Decision", ["action", "reason", "amount_atomic"])

# Decision.action values
ALLOW = "allow"
BLOCK = "block"
PROMPT = "prompt"

# Names of the limit dimensions a policy scope may set.
_LIMIT_NAMES = ("per_call_max", "per_session_total", "per_day_total",
                "prompt_above")


class Policy:
    """Loaded budget policy. All limit amounts are ATOMIC INTS (converted from
    decimal strings once, here). Mutating a Policy after construction is not
    supported -- treat it as read-only."""

    __slots__ = ("decimals", "asset", "networks", "asset_addresses",
                 "global_limits", "per_host", "per_tool", "block_if_unlimited",
                 "on_over_budget", "unknown_offer")

    def __init__(self, decimals=6, asset=None, networks=None,
                 asset_addresses=None, global_limits=None, per_host=None,
                 per_tool=None, block_if_unlimited=True,
                 on_over_budget="block", unknown_offer="block"):
        self.decimals = decimals
        self.asset = asset
        self.networks = networks or set()                # normalized lower
        self.asset_addresses = asset_addresses or set()  # normalized lower
        self.global_limits = global_limits or {}         # name -> atomic int
        self.per_host = per_host or {}                   # host -> {name: atomic}
        self.per_tool = per_tool or {}                   # path -> {name: atomic}
        self.block_if_unlimited = block_if_unlimited
        self.on_over_budget = on_over_budget
        self.unknown_offer = unknown_offer


# ===========================================================================
# Atomic-unit conversion (NO FLOATS)
# ===========================================================================
def to_atomic(s, decimals):
    """
    Decimal money string -> integer atomic units. `"0.10"`, decimals=6 -> 100000.

    Rejects (ValueError): non-string, garbage, and -- deliberately -- MORE
    fractional digits than `decimals` (silently truncating money is a bug, so
    we refuse rather than round). Accepts an optional leading sign.
    """
    if not isinstance(s, str):
        raise ValueError("amount must be a string, got %r" % type(s).__name__)
    t = s.strip()
    if not t:
        raise ValueError("empty amount")
    neg = False
    if t[0] in "+-":
        neg = t[0] == "-"
        t = t[1:]
    if "." in t:
        intp, frac = t.split(".", 1)
    else:
        intp, frac = t, ""
    if intp == "":
        intp = "0"
    if not intp.isdigit():
        raise ValueError("bad integer part: %r" % s)
    if frac != "" and not frac.isdigit():
        raise ValueError("bad fractional part: %r" % s)
    if len(frac) > decimals:
        raise ValueError("more precision than %d decimals: %r" % (decimals, s))
    frac = frac.ljust(decimals, "0")
    val = int(intp) * (10 ** decimals) + (int(frac) if frac else 0)
    return -val if neg else val


def from_atomic(atomic, decimals):
    """Atomic int -> decimal string for display/logging. Inverse of to_atomic
    for non-negative values; keeps trailing zeros trimmed but always >= '0'."""
    if not isinstance(atomic, int):
        raise ValueError("atomic must be int")
    neg = atomic < 0
    a = -atomic if neg else atomic
    if decimals == 0:
        body = str(a)
    else:
        whole, frac = divmod(a, 10 ** decimals)
        frac_str = str(frac).rjust(decimals, "0").rstrip("0")
        body = str(whole) if not frac_str else "%d.%s" % (whole, frac_str)
    return ("-" + body) if neg else body


def parse_atomic_int(s):
    """
    Parse an x402 amount that is ALREADY in atomic units (offers carry
    `maxAmountRequired` as an atomic-unit string, and X-PAYMENT authorizations
    carry `value` the same way). Accepts a non-negative integer string or an
    int. Returns int, or None to REJECT (fail closed).
    """
    if isinstance(s, bool):
        return None                      # bool is an int subclass; refuse it
    if isinstance(s, int):
        return s if s >= 0 else None
    if not isinstance(s, str):
        return None
    t = s.strip()
    if not t.isdigit():                  # no sign, no dot, no space -> atomic int
        return None
    return int(t)


# ===========================================================================
# Resource / URL helpers
# ===========================================================================
def resource_split(resource):
    """
    Split an x402 `resource` into (host, path). Accepts a full URL
    ('https://api.example.com/tool/search') or a bare path ('/tool/search').
    Returns (host_or_None, path). Never raises.
    """
    if not resource or not isinstance(resource, str):
        return (None, "/")
    r = resource.strip()
    for scheme in ("https://", "http://"):
        if r.lower().startswith(scheme):
            rest = r[len(scheme):]
            slash = rest.find("/")
            if slash == -1:
                authority, path = rest, "/"
            else:
                authority, path = rest[:slash], rest[slash:]
            if "@" in authority:
                authority = authority.rsplit("@", 1)[1]
            # strip :port for host keying
            if authority.startswith("["):        # [ipv6]:port
                end = authority.find("]")
                host = authority[1:end] if end != -1 else authority
            else:
                host = authority.rsplit(":", 1)[0] if ":" in authority else authority
            return (host.lower() or None, path or "/")
    # bare path (or something we don't recognize as a URL)
    if r.startswith("/"):
        return (None, r)
    return (None, "/")


# ===========================================================================
# x402 wire parsing (fail closed on anything unexpected)
# ===========================================================================
def parse_x402_offer(body_bytes, host_hint=None, prefer_network=None):
    """
    Parse a `402 Payment Required` JSON body into a normalized Offer, or None
    to REJECT (unparseable / empty / oversize / no usable offer => fail closed).

    Unknown/extra fields are ignored. When `accepts` has several offers, prefer
    one whose network matches `prefer_network`, else the first parseable one.
    `host_hint` fills Offer.host when the resource is a bare path.
    """
    if body_bytes is None:
        return None
    if isinstance(body_bytes, (bytes, bytearray)):
        if len(body_bytes) > MAX_OFFER_BYTES:
            return None
        try:
            text = bytes(body_bytes).decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(body_bytes, str):
        if len(body_bytes.encode("utf-8", "ignore")) > MAX_OFFER_BYTES:
            return None
        text = body_bytes
    else:
        return None

    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None

    accepts = doc.get("accepts")
    if not isinstance(accepts, list) or not accepts:
        return None

    # pick preferred-network offer if available, else first parseable
    chosen = None
    for entry in accepts:
        if not isinstance(entry, dict):
            continue
        if chosen is None:
            chosen = entry
        if prefer_network and str(entry.get("network", "")).lower() == \
                str(prefer_network).lower():
            chosen = entry
            break
    if chosen is None:
        return None

    amount = parse_atomic_int(chosen.get("maxAmountRequired"))
    if amount is None:
        return None
    asset = chosen.get("asset")
    asset = asset.lower() if isinstance(asset, str) else None
    network = chosen.get("network")
    network = network.lower() if isinstance(network, str) else None
    pay_to = chosen.get("payTo") if isinstance(chosen.get("payTo"), str) else None
    resource = chosen.get("resource") if isinstance(chosen.get("resource"), str) else None

    host, path = resource_split(resource)
    if host is None:
        host = host_hint.lower() if isinstance(host_hint, str) else None

    return Offer(amount_atomic=amount, asset=asset, network=network,
                 pay_to=pay_to, resource=resource, host=host, path=path)


def _find_amount(obj, depth=0):
    """Recursively locate the authorization amount in a decoded X-PAYMENT
    payload. x402 puts it at payload.authorization.value; we also accept a
    top-level `value`/`amount`. Returns atomic int or None. Bounded depth."""
    if depth > 6 or not isinstance(obj, dict):
        return None
    # Prefer the canonical location first.
    payload = obj.get("payload")
    if isinstance(payload, dict):
        auth = payload.get("authorization")
        if isinstance(auth, dict):
            v = parse_atomic_int(auth.get("value"))
            if v is not None:
                return v
    for key in ("value", "amount", "maxAmountRequired"):
        if key in obj:
            v = parse_atomic_int(obj.get(key))
            if v is not None:
                return v
    for v in obj.values():
        if isinstance(v, dict):
            found = _find_amount(v, depth + 1)
            if found is not None:
                return found
    return None


def extract_payment_amount(x_payment_header):
    """
    Decode a base64 `X-PAYMENT` header and return a Payment(amount_atomic,
    asset, network), or None to REJECT (bad base64 / bad json / no amount).

    NOTE: the proxy NEVER mutates or re-emits this -- it only reads the amount
    to gate the retry. asset is usually absent from the payload (implied by the
    offer), so it may be None.
    """
    if not x_payment_header or not isinstance(x_payment_header, str):
        return None
    raw = x_payment_header.strip()
    if len(raw) > MAX_PAYMENT_HEADER_BYTES:
        return None
    # tolerate missing base64 padding
    pad = (-len(raw)) % 4
    try:
        decoded = base64.b64decode(raw + ("=" * pad), validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        doc = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    amount = _find_amount(doc)
    if amount is None:
        return None
    network = doc.get("network")
    network = network.lower() if isinstance(network, str) else None
    asset = doc.get("asset")
    asset = asset.lower() if isinstance(asset, str) else None
    return Payment(amount_atomic=amount, asset=asset, network=network)


# ===========================================================================
# Policy loading (fail closed, decimals -> atomic once)
# ===========================================================================
def _limits_to_atomic(d, decimals):
    """Convert a {name: decimal-string} limit block to {name: atomic-int}.
    Unknown names are ignored; a bad value raises (caller fails closed)."""
    out = {}
    if not isinstance(d, dict):
        return out
    for name in _LIMIT_NAMES:
        if name in d and d[name] is not None:
            out[name] = to_atomic(str(d[name]), decimals)
    return out


def load_policy(path):
    """
    Load a budget policy JSON file into a Policy. Returns None on any error
    (missing file, bad json, bad amount) -- the CALLER must treat None as
    fail-closed (block all spend), exactly as an empty allowlist blocks all
    egress in enforce mode.
    """
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return policy_from_dict(doc)


def policy_from_dict(doc):
    """Build a Policy from an already-decoded dict. Returns None on bad shape
    or a bad amount (fail closed). Split out from load_policy for testing."""
    if not isinstance(doc, dict):
        return None
    try:
        decimals = int(doc.get("unit_decimals", 6))
        if decimals < 0 or decimals > 30:
            return None
        asset = doc.get("asset")
        networks = set(str(n).lower() for n in doc.get("networks", []) or [])
        asset_addresses = set(str(a).lower()
                              for a in doc.get("asset_addresses", []) or [])
        global_limits = _limits_to_atomic(doc.get("limits", {}), decimals)
        per_host = {str(h).lower(): _limits_to_atomic(v, decimals)
                    for h, v in (doc.get("per_host", {}) or {}).items()}
        per_tool = {str(p): _limits_to_atomic(v, decimals)
                    for p, v in (doc.get("per_tool", {}) or {}).items()}
        block_if_unlimited = bool(doc.get("block_if_unlimited", True))
        on_over_budget = str(doc.get("on_over_budget", "block"))
        unknown_offer = str(doc.get("unknown_offer", "block"))
    except (ValueError, TypeError, AttributeError):
        return None
    return Policy(decimals=decimals, asset=asset, networks=networks,
                  asset_addresses=asset_addresses, global_limits=global_limits,
                  per_host=per_host, per_tool=per_tool,
                  block_if_unlimited=block_if_unlimited,
                  on_over_budget=on_over_budget, unknown_offer=unknown_offer)


# ===========================================================================
# THE GATE (pure) -- enforcement truth
# ===========================================================================
def _resolve_limit(policy, host, path, name):
    """Most-specific-wins limit resolution: per_tool > per_host > global.
    Returns atomic int or None (no such limit at any scope)."""
    tl = policy.per_tool.get(path)
    if tl and name in tl:
        return tl[name]
    hl = policy.per_host.get(host) if host else None
    if hl and name in hl:
        return hl[name]
    return policy.global_limits.get(name)


def budget_decide(offer, ledger_snapshot, policy):
    """
    The core money gate. Pure: (offer, cumulative-spend snapshot, policy) ->
    Decision(action, reason, amount_atomic). No mutation; the caller commits to
    the ledger only after an ALLOW that actually settles.

      ALLOW  - under every applicable limit (or a zero-amount offer)
      BLOCK  - unparseable offer / asset|network mismatch / bad amount /
               no limit set (fail-closed) / exceeds a hard limit
      PROMPT - within hard limits but above a `prompt_above` band

    `ledger_snapshot` is a dict: {"session": int, "day": int,
    "host": {host: int}, "tool": {path: int}} (day-windowed atomic totals).
    Missing keys read as 0. `policy` None => everything blocks (fail closed).
    """
    if policy is None:
        return Decision(BLOCK, "no-policy", 0)
    if offer is None:
        return Decision(BLOCK, "unknown-offer", 0)

    amt = offer.amount_atomic
    if not isinstance(amt, int) or amt < 0:
        return Decision(BLOCK, "bad-amount", amt if isinstance(amt, int) else 0)

    # asset / network identity checks (no conversion, no FX)
    if policy.asset_addresses and offer.asset and \
            offer.asset not in policy.asset_addresses:
        return Decision(BLOCK, "asset-mismatch", amt)
    if policy.networks and offer.network and \
            offer.network not in policy.networks:
        return Decision(BLOCK, "network-mismatch", amt)

    if amt == 0:
        return Decision(ALLOW, "zero-amount", 0)

    snap = ledger_snapshot or {}
    host, path = offer.host, offer.path

    def cumulative(bucket, key):
        b = snap.get(bucket) or {}
        return b.get(key, 0) if isinstance(b, dict) else 0

    # --- per-call ceiling (most specific). Absent -> fail closed by default. ---
    per_call = _resolve_limit(policy, host, path, "per_call_max")
    if per_call is None:
        if policy.block_if_unlimited:
            return Decision(BLOCK, "no-limit-set", amt)
    elif amt > per_call:
        return Decision(BLOCK, "over-per-call", amt)

    # --- cumulative hard limits (each bucket checked independently) ---
    sess_limit = policy.global_limits.get("per_session_total")
    if sess_limit is not None and snap.get("session", 0) + amt > sess_limit:
        return Decision(BLOCK, "over-session", amt)

    day_limit = policy.global_limits.get("per_day_total")
    if day_limit is not None and snap.get("day", 0) + amt > day_limit:
        return Decision(BLOCK, "over-day", amt)

    hl = policy.per_host.get(host) if host else None
    if hl and "per_day_total" in hl and \
            cumulative("host", host) + amt > hl["per_day_total"]:
        return Decision(BLOCK, "over-host-day", amt)

    tl = policy.per_tool.get(path)
    if tl and "per_day_total" in tl and \
            cumulative("tool", path) + amt > tl["per_day_total"]:
        return Decision(BLOCK, "over-tool-day", amt)

    # --- human-in-the-loop band (within hard limits but pricey) ---
    prompt_at = _resolve_limit(policy, host, path, "prompt_above")
    if prompt_at is not None and amt > prompt_at:
        return Decision(PROMPT, "above-prompt-threshold", amt)

    return Decision(ALLOW, "under-budget", amt)


# ===========================================================================
# Ledger -- thread-safe spend accounting (day-windowed)
# ===========================================================================
class Ledger:
    """
    Tracks SETTLED spend for the money lock. Thread-safe (the proxy is
    threaded). Session total is cumulative for the process lifetime; day totals
    are windowed to the last DAY_SECONDS so `per_day_total` is a rolling 24h.

    Authorized-vs-settled: the gate checks an offer's authorized amount at
    retry time, but only a CONFIRMED settlement (200 + X-PAYMENT-RESPONSE)
    calls record(). A blocked or failed payment never touches the ledger.

    `now`/`ts` are injected (never called implicitly) so the logic is
    deterministic and unit-testable, mirroring the pure-function discipline.
    """

    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self._entries = []   # list of (ts, host, path, amount_atomic)
        self._session_total = 0

    def record(self, amount_atomic, host, path, ts):
        """Record a settled payment. amount_atomic must be a non-negative int."""
        if not isinstance(amount_atomic, int) or amount_atomic < 0:
            raise ValueError("amount_atomic must be a non-negative int")
        with self._lock:
            self._entries.append((ts, host, path, amount_atomic))
            self._session_total += amount_atomic

    def snapshot(self, now):
        """Return the {session, day, host, tool} dict budget_decide() expects,
        with day/host/tool windowed to [now - DAY_SECONDS, now]."""
        cutoff = now - DAY_SECONDS
        day = 0
        host_day = {}
        tool_day = {}
        with self._lock:
            session = self._session_total
            for ts, host, path, amt in self._entries:
                if ts >= cutoff:
                    day += amt
                    if host is not None:
                        host_day[host] = host_day.get(host, 0) + amt
                    if path is not None:
                        tool_day[path] = tool_day.get(path, 0) + amt
        return {"session": session, "day": day, "host": host_day, "tool": tool_day}

    def prune(self, now):
        """Drop entries older than the day window to bound memory. Session
        total is preserved (it is cumulative, not windowed)."""
        cutoff = now - DAY_SECONDS
        with self._lock:
            self._entries = [e for e in self._entries if e[0] >= cutoff]
