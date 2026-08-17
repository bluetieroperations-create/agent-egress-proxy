#!/usr/bin/env python3
"""
transfer_sim.py -- SHARED transfer-simulation core: ask the chain "would this transfer
actually succeed?" by `eth_call`ing it, then decode and attribute the revert.

WHY THIS EXISTS (measured, not assumed). Our restriction checks used to ask a token
"do you expose a permissioned interface?" -- and a live probe of ALL 535 corpus tokens x 9
interface probes found 535/535 alive but **0/535 exposing any interface**, so
`rwa_readiness` returned "unknown" for the entire corpus. Simulating the transfer instead
gives a DEFINITIVE answer with NO interface required: it is how we discovered every
permissioned issuer we now hold (Ondo OUSG, BlackRock BUIDL, Matrixdock STBT).

THE CONTROL IS THE POINT. A naive simulation blames the RECEIVER for every revert, but the
transfer may have failed because of the SENDER (their own lock-up, their own blacklisting,
an empty balance). So every assessment runs TWICE -- once to the real target, once to a
CONTROL address -- and attributes the failure only when the two disagree:

    target fails + control OK      -> the RECEIVER is blocked   (actionable)
    target fails + control fails   -> the SENDER/token is at fault (NOT the receiver)
    target OK                      -> transfer is live
    anything indeterminate         -> unknown (fail-open)

This is the same discipline as the `decimals()` control that caught a broken interface
probe: never trust a negative result without a positive control.

Reuses `revert_scan.classify_revert` -- the classifier calibrated on REAL revert strings
(USDC blacklist, BUIDL registry/lock-up, STBT NO_RECEIVE_PERMISSION), including its
word-boundary + negation handling. Pure core + injected transport. Stdlib. Fail-open.
"""
from __future__ import annotations

from revert_scan import classify_revert
from rwa_readiness import eth_call_data

# Attribution outcomes.
OK = "ok"
RECEIVER_BLOCKED = "receiver_blocked"
SENDER_BLOCKED = "sender_blocked"
UNKNOWN = "unknown"

# A fresh, never-used address: for a FREELY transferable token it is a valid recipient,
# so it makes a good "would anything work?" control. (For a PERMISSIONED token a fresh
# address is itself un-allowlisted -- pass a known-good holder as the control instead.)
FRESH_CONTROL = "0x" + "5c" * 20

# Revert classes that mean "the token's own rules rejected this transfer".
_RESTRICTION_CLASSES = ("restriction",)

UINT256_MAX = (1 << 256) - 1


def clamp_amount(amount):
    """Any input -> a uint256-safe positive amount for the probe. AUDIT-DRIVEN: a
    negative amount encoded garbage into the calldata word, and an absurd one (e.g. from
    "1e400") exceeds uint256 and would silently truncate. We are probing FEASIBILITY, so
    clamping is correct: a too-large amount simply reverts on balance (classified
    `balance` -> a note, never a block). NEVER raises."""
    try:
        v = int(amount)
    except (TypeError, ValueError, OverflowError):
        return 1
    if v < 1:
        return 1
    return v if v <= UINT256_MAX else UINT256_MAX


def decode_revert_error(err):
    """Extract the human revert reason from an eth_call error object.
    Handles the ABI `Error(string)` payload (selector 0x08c379a0) and falls back to the
    node's message. Returns a string or None. NEVER raises."""
    if not isinstance(err, dict):
        return None
    data = err.get("data")
    if isinstance(data, dict):                      # some nodes nest it
        data = data.get("data") or data.get("originalError", {}).get("data")
    if isinstance(data, str) and data.startswith("0x08c379a0"):
        try:
            b = bytes.fromhex(data[10:])
            off = int.from_bytes(b[0:32], "big")
            ln = int.from_bytes(b[off:off + 32], "big")
            s = b[off + 32:off + 32 + ln].decode("utf-8", errors="replace")
            if s:
                return s
        except Exception:
            pass
    msg = err.get("message")
    return msg if isinstance(msg, str) and msg else None


def attribute(target, control):
    """PURE: combine a TARGET and a CONTROL simulation result into an attribution.

    Each result is {"ok": bool|None, "reason": str|None}; ok=None means indeterminate
    (transport error) -> the whole assessment degrades to `unknown` (fail-open).

    Returns {"outcome", "reason", "revert_class"}. The control is what makes a
    RECEIVER_BLOCKED claim trustworthy: without it, a sender-side failure would be
    misattributed to the receiver. NEVER raises."""
    t = target if isinstance(target, dict) else {}
    c = control if isinstance(control, dict) else {}
    t_ok, c_ok = t.get("ok"), c.get("ok")
    reason = t.get("reason")
    cls = classify_revert(reason) if reason else None

    if t_ok is True:
        return {"outcome": OK, "reason": None, "revert_class": None}
    if t_ok is None:
        return {"outcome": UNKNOWN, "reason": reason, "revert_class": cls}
    # target reverted -- the control decides WHO to blame.
    if c_ok is True:
        return {"outcome": RECEIVER_BLOCKED, "reason": reason, "revert_class": cls}
    if c_ok is False:
        return {"outcome": SENDER_BLOCKED, "reason": reason, "revert_class": cls}
    return {"outcome": UNKNOWN, "reason": reason, "revert_class": cls}


def to_readiness_probe(attribution):
    """PURE: map an attribution -> the `probe` shape `rwa_readiness.assess_transfer_readiness`
    already understands, so simulation folds through the EXISTING verdict path unchanged.

    CONSERVATIVE: only a RECEIVER_BLOCKED with a RESTRICTION-class revert sets
    can_receive=False. A balance/gas/opaque revert, or a sender-side failure, leaves it
    None ("did not determine") -- fail-open by construction. NEVER raises."""
    a = attribution if isinstance(attribution, dict) else {}
    out = {"standard": "simulation", "receiver_verified": None, "receiver_frozen": None,
           "paused": None, "can_receive": None, "reason_code": None}
    if a.get("outcome") == OK:
        out["can_receive"] = True
    elif (a.get("outcome") == RECEIVER_BLOCKED
            and a.get("revert_class") in _RESTRICTION_CLASSES):
        out["can_receive"] = False
        out["reason_code"] = a.get("reason")
    return out


class TransferSimulator:
    """Runs `eth_call transfer(to, amount)` with an explicit `from`. Transport injected
    for tests; the default is a keyless JSON-RPC POST. NEVER raises out of `simulate`."""

    def __init__(self, rpc_url=None, timeout=4.0, transport=None):
        self.rpc_url = rpc_url
        self.timeout = timeout
        self._transport = transport

    def simulate(self, token, frm, to, amount):
        """-> {"ok": bool|None, "reason": str|None}. ok=None means indeterminate."""
        try:
            data = eth_call_data("transfer(address,uint256)", (to, clamp_amount(amount)))
        except Exception:
            return {"ok": None, "reason": None}
        try:
            res = self._call({"to": token, "from": frm, "data": data})
        except Exception:
            return {"ok": None, "reason": None}
        if not isinstance(res, dict):
            return {"ok": None, "reason": None}
        if res.get("error"):
            return {"ok": False, "reason": decode_revert_error(res["error"])}
        if "result" in res:
            return {"ok": True, "reason": None}
        return {"ok": None, "reason": None}

    def assess(self, token, frm, to, amount, control=FRESH_CONTROL):
        """Target + CONTROL simulation -> attribution. The control call is skipped when
        the target succeeds (nothing to attribute), keeping the hot path to one call in
        the common case."""
        t = self.simulate(token, frm, to, amount)
        if t.get("ok") is True or t.get("ok") is None:
            return attribute(t, {"ok": None})
        c = self.simulate(token, frm, control, amount)
        return attribute(t, c)

    # -- transport --
    def _call(self, params):
        if self._transport is not None:
            return self._transport(params)
        import json
        import urllib.request
        if not self.rpc_url:
            return {}
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                           "params": [params, "latest"]}).encode()
        req = urllib.request.Request(
            self.rpc_url, data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "blackwall/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())


class BlockscoutHolderLookup:
    """Find a REAL holder of a token, so the simulation has a funded sender.

    Simulation needs a `from` that actually holds the token -- an empty wallet reverts on
    BALANCE, which `to_readiness_probe` correctly refuses to treat as a restriction, so
    without a holder the source silently answers "unknown" for every request. A live
    request never carries one, so we look it up.

    Results are CACHED per token: the top holder is stable, and re-fetching it on every
    verdict would add a request to the hot path and leak the queried token repeatedly.
    Keyless, fail-open (None on any error -> the source no-ops)."""

    def __init__(self, bases=None, timeout=6.0, fetch=None, max_entries=512):
        from holder_concentration import BLOCKSCOUT_BASES
        self.bases = bases or BLOCKSCOUT_BASES
        self.timeout = timeout
        self._fetch = fetch
        self._cache = {}
        self.max_entries = max_entries

    def __call__(self, token, chain):
        # Coerce, don't assume: a non-string chain/token must no-op, not raise. (Caught
        # by the module's own smoke test -- `5 or ""` is 5, and 5.strip() explodes.)
        def _s(x):
            return x.strip().lower() if isinstance(x, str) else ""
        key = (_s(chain), _s(token))
        if key in self._cache:
            return self._cache[key]
        holder = None
        try:
            holder = self._lookup(key[1], key[0])
        except Exception:
            holder = None
        if len(self._cache) < self.max_entries:
            self._cache[key] = holder          # cache misses too: a token with no
        return holder                          # readable holders shouldn't be re-fetched

    def _lookup(self, token, chain):
        base = self.bases.get(chain)
        if not base or not token:
            return None
        if self._fetch is not None:
            data = self._fetch(token, chain)
        else:
            import http_util
            data = http_util.get_json("%s/api/v2/tokens/%s/holders" % (base, token),
                                      timeout=self.timeout)
        for item in (data.get("items") or []) if isinstance(data, dict) else []:
            addr = item.get("address") if isinstance(item, dict) else None
            h = addr.get("hash") if isinstance(addr, dict) else addr
            if isinstance(h, str) and h.startswith("0x"):
                return h
        return None


class SimulationReadinessSource:
    """RWA readiness via SIMULATION -- the definitive answer the interface probe can't
    give (0/535 corpus tokens expose an interface). Drop-in for the `rwa_source` seam:
    `check(acquires, payer, counterparty)` -> the same signal shape as
    `RwaReadinessSource`, folded by the shared `apply_rwa_readiness`.

    Needs a `from` address that actually HOLDS the token (you cannot simulate a transfer
    out of an empty wallet), supplied per-request via `acquires.holder` or by an injected
    `holder_lookup(token, chain)`. Without one it returns None (no-op, fail-open)."""

    def __init__(self, simulator=None, holder_lookup=None, rpc_url=None):
        self.simulator = simulator or TransferSimulator(rpc_url=rpc_url)
        self.holder_lookup = holder_lookup

    def check(self, acquires, payer, counterparty=None):
        from rwa_readiness import assess_transfer_readiness
        if not isinstance(acquires, dict) or not payer:
            return None
        token = acquires.get("token")
        if not token:
            return None
        holder = acquires.get("holder")
        if not holder and self.holder_lookup is not None:
            try:
                holder = self.holder_lookup(token, acquires.get("chain"))
            except Exception:
                holder = None
        if not holder:
            return None                       # cannot simulate without a funded sender
        # The CONTROL must be an address the token already accepts -- another real holder.
        # Falling back to the sender itself is a valid self-transfer control on most ERC-20s.
        control = acquires.get("control_holder") or holder
        try:
            amount = int(acquires.get("sim_amount") or 1)
            att = self.simulator.assess(token, holder, payer, amount, control=control)
        except Exception:
            return None
        signal = assess_transfer_readiness(to_readiness_probe(att))
        # RETURN None, NOT an "unknown" signal. CombinedRwaReadinessSource takes the
        # FIRST non-None result, and this source is ordered FIRST -- so answering
        # "unknown" here would SHADOW the interface probe and the RAMS axis instead of
        # deferring to them. Declining lets the fallbacks have their turn.
        if not signal or signal.get("grade") == "unknown":
            return None
        return signal
