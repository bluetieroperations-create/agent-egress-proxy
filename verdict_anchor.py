#!/usr/bin/env python3
"""
verdict_anchor.py -- OPTIONAL server-side auto-anchor of every verdict to Traceipt.

When enabled (BLACKWALL_ANCHOR=1), the verdict server fires each verdict's canonical
digest at Traceipt `POST /attest` so an operator gets a tamper-evident, time-stamped
audit trail ("Blackwall issued verdict X at time T") WITHOUT changing the verdict
contract -- the response the caller sees is byte-for-byte unchanged.

Three hard invariants, learned the hard way (the lazy-ingest 502):
  * NON-BLOCKING -- the anchor is a network POST; it runs on a background daemon
    thread and can NEVER delay, block, or fail a verdict response. No network on
    the hot path.
  * FAIL-OPEN -- any error (offline, 402, spoofed challenge, eth-account missing)
    is swallowed and logged; the verdict is untouched.
  * KEY-FREE CORE -- signing an x402 payment needs a funded key + eth-account, the
    `clients/x402_pay` dep boundary. That import is LAZY and optional here, so the
    stdlib verdict core never imports a private-key path unless an operator opts in
    with SIGNER_PRIVATE_KEY.

Cost + reliability caveat (why this is OFF by default): `/attest` is x402-PAID
(~0.002 USDC per verdict), and Traceipt currently DROPS pending attestations on
restart (docs/TRACEIPT_ATTEST_FINDING.md), so anchoring is opt-in, spend-capped,
and strictly best-effort. For a durable audit trail, confirm delivery out-of-band
with `traceipt_attest.poll_proof` rather than trusting the 201.

Env:
  BLACKWALL_ANCHOR             1/true/... to enable (default off)
  BLACKWALL_ANCHOR_URL         Traceipt base URL (default https://api.traceipt.xyz)
  BLACKWALL_ANCHOR_MAX_ATOMIC  refuse to auto-pay above N atomic USDC (default 1e6=$1)
  SIGNER_PRIVATE_KEY           funded throwaway key that pays /attest (no key -> the
                               anchor hits 402 and fails open; nothing is signed)
  BLACKWALL_ANCHOR_NETWORK     x402 network for the payment (default base-sepolia)
  BLACKWALL_ANCHOR_RPC         optional JSON-RPC to read the token EIP-712 domain
"""
from __future__ import annotations

import os
import sys
import threading

import traceipt_attest

DEFAULT_URL = "https://api.traceipt.xyz"
# $1.00 USDC. A spoofed/compromised /attest can't drain the signer by naming a huge
# amount -- anchoring is a micro-payment, so anything above this is refused.
DEFAULT_MAX_ATOMIC = 1_000_000

# Fields of the verdict RESPONSE that must NOT enter the anchored digest:
#  * report_token -- a SECRET (authorizes outcome reporting); keep it out of a
#    public, third-party-reproducible proof.
#  * receipt_id is KEPT: it's the public handle that ties the proof to the verdict.
_ANCHOR_DROP = ("report_token",)


def anchorable(verdict_obj):
    """The projection of a verdict actually anchored: the full response minus
    secrets (report_token). PURE. A third party holding the tokenless verdict can
    reproduce this digest and check it against Traceipt's Merkle root."""
    if not isinstance(verdict_obj, dict):
        return verdict_obj
    return {k: v for k, v in verdict_obj.items() if k not in _ANCHOR_DROP}


class VerdictAnchor:
    """Fire-and-forget anchor hook wired into the verdict server. `submit()` returns
    immediately; the actual POST runs on `runner` (a daemon thread by default)."""

    def __init__(self, base_url, *, pay=None, max_amount_atomic=DEFAULT_MAX_ATOMIC,
                 timeout=10, runner=None, log=None, _anchor=None):
        self.base_url = base_url
        self.pay = pay
        self.max_amount_atomic = max_amount_atomic
        self.timeout = timeout
        self._runner = runner or _thread_runner
        self._log = log or (lambda m: (sys.stderr.write(m + "\n"), sys.stderr.flush()))
        self._anchor = _anchor or traceipt_attest.anchor_verdict

    def submit(self, verdict_obj):
        """Schedule an anchor for this verdict. NON-BLOCKING, NEVER raises -- even
        scheduling failures are swallowed so the caller (the hot path) is safe."""
        try:
            view = anchorable(verdict_obj)
            self._runner(lambda: self._run(view))
        except Exception:
            pass

    def _run(self, view):
        try:
            res = self._anchor(self.base_url, view, pay=self.pay,
                               max_amount_atomic=self.max_amount_atomic,
                               timeout=self.timeout)
            if isinstance(res, dict) and res.get("ok"):
                self._log("blackwall: anchored %s -> %s"
                          % (str(res.get("digest"))[:23], res.get("attestation_id")))
            else:
                reason = res.get("reason") if isinstance(res, dict) else "unknown"
                self._log("blackwall: anchor skipped (%s)" % reason)
        except Exception as e:  # background thread: never propagate
            self._log("blackwall: anchor error %s: %s" % (type(e).__name__, e))


def _thread_runner(fn):
    threading.Thread(target=fn, daemon=True).start()


def pay_from_env():
    """Build the optional x402 `pay` callback from SIGNER_PRIVATE_KEY via the
    `clients/x402_pay` eth-account boundary. Returns a callable, or None when there
    is no key OR eth-account is unavailable (both -> anchor fails open, unsigned).
    Keeps the stdlib core key-free: nothing here imports eth-account unless a key
    is actually present."""
    pk = os.environ.get("SIGNER_PRIVATE_KEY")
    if not pk:
        return None
    try:
        clients_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "clients")
        if clients_dir not in sys.path:
            sys.path.insert(0, clients_dir)
        from x402_pay import make_pay  # brings in eth-account (optional dep)
        network = os.environ.get("BLACKWALL_ANCHOR_NETWORK", "base-sepolia")
        rpc = os.environ.get("BLACKWALL_ANCHOR_RPC")
        return make_pay(pk, network=network, rpc_url=rpc)
    except Exception as e:
        sys.stderr.write("blackwall: anchor signer unavailable (%s: %s) -- "
                         "anchoring will fail open unsigned\n"
                         % (type(e).__name__, e))
        sys.stderr.flush()
        return None


def from_env(flag_reader, *, pay=None, log=None):
    """Build a VerdictAnchor from BLACKWALL_ANCHOR* env, or None if disabled.
    `flag_reader(name) -> bool` is injected (blackwall._env_flag) to avoid an import
    cycle. `pay`/`log` are injectable for tests; `pay` defaults to pay_from_env()."""
    if not flag_reader("BLACKWALL_ANCHOR"):
        return None
    base_url = os.environ.get("BLACKWALL_ANCHOR_URL", DEFAULT_URL)
    raw = os.environ.get("BLACKWALL_ANCHOR_MAX_ATOMIC")
    try:
        cap = int(raw) if raw else DEFAULT_MAX_ATOMIC
    except (TypeError, ValueError):
        cap = DEFAULT_MAX_ATOMIC
    signer = pay if pay is not None else pay_from_env()
    return VerdictAnchor(base_url, pay=signer, max_amount_atomic=cap, log=log)
