#!/usr/bin/env python3
"""
bench.py -- reproducible COMPUTE latency for the verdict path.

Latency numbers are easy to mislead with, so this is honest by construction:

  * PERCENTILES, never mean/max. p50 is stable here to ~1% across runs, but a single
    call's max swings 10-40x on OS scheduling jitter -- so mean/max read as "flaky"
    even when the operation isn't. p50/p95/p99 are the honest, reproducible numbers.
  * WARMUP + GC disabled during timing + perf_counter.
  * COMPUTE ONLY, with the environment stamped. NETWORK/HTTP IS EXCLUDED -- a real
    HTTPS round-trip is 10-100ms and dominates any deployment; it is not ours to claim.
    These are the server's own CPU work, on THIS machine.
  * PATHS SEPARATED. A plain verdict and a full signed-payment verification differ
    ~100x (pure-Python keccak dominates the latter), so a single headline number lies.

NOT a comparison and NOT a marketing claim -- numbers are environment-specific. Reproduce
with `python bench.py`; stamp the printed environment next to any number you quote.
"""
from __future__ import annotations

import gc
import platform
import statistics
import sys
import time

import blackwall as bw

_GOOD = {"settlement_count": 500, "dispute_rate": 0.0, "distinct_payers": 30}
_STABLE = ["0.09", "0.09", "0.088", "0.092"]
_CP = "0x" + "1" * 40


class _Src:
    def lookup(self, cp):
        return dict(_GOOD)


def measure(fn, n, *, warmup=200):
    """Return a percentile summary (microseconds) for `fn`, run `n` times after warmup.
    GC is disabled during timing so a collection can't be misattributed to one call."""
    for _ in range(max(1, warmup)):
        fn()
    gc.collect()
    gc.disable()
    try:
        xs = []
        for _ in range(n):
            t0 = time.perf_counter()
            fn()
            xs.append((time.perf_counter() - t0) * 1e6)
    finally:
        gc.enable()
    xs.sort()
    def _pct(p):
        return round(xs[min(n - 1, int(n * p))], 1)
    return {"n": n, "unit": "us", "p50": _pct(0.50), "p95": _pct(0.95),
            "p99": _pct(0.99), "min": round(xs[0], 1)}


def _pure_verdict():
    return bw.decide_payment("0.09", dict(_GOOD), _STABLE, counterparty=_CP)


def _forecast_basic():
    return bw.forecast({"counterparty": _CP, "amount": "0.09", "asset": "USDC",
                        "chain": "base", "resource": "https://x/weather"}, _Src())


def _keccak():
    import keccak
    return keccak.keccak256(b"\x19\x01" + b"\x11" * 64)


# A VALID signature, precomputed once. This is CRITICAL for honesty: ecdsa_recover with
# invalid (r,s) short-circuits BEFORE the expensive EC scalar-multiplication, so it
# under-measures the real recovery by ~100x. Only a signature that actually recovers a
# point exercises the real cost. (This trap made an earlier draft publish ~1.7ms for a
# path that is really ~30ms.) `ecdsa_sign` returns (r, s, rec_id).
_DOMAIN = {"name": "USD Coin", "version": "2", "chainId": 8453,
           "verifyingContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}
_MSG = {"from": "0x" + "1" * 40, "to": "0x" + "2" * 40, "value": 90000,
        "validAfter": 0, "validBefore": 9999999999, "nonce": b"\x00" * 32}


def _valid_sig():
    import eip712
    import secp256k1
    z = eip712.transfer_authorization_digest(_DOMAIN, _MSG)
    d = 0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef
    r, s, rid = secp256k1.ecdsa_sign(z, d)
    return z, r, s, rid


_Z, _R, _S, _RID = _valid_sig()


def _ecdsa_recover():
    import secp256k1
    secp256k1.ecdsa_recover(_Z, _R, _S, _RID)   # VALID sig -> full scalar-mult


# The REAL signed-payment verification (payload-sim Phase 2): EIP-712 digest + a
# SUCCESSFUL signer recovery (full EC scalar-mult, the dominant cost) + address
# derivation. Measured with a valid signature, not estimated.
def _signed_verify():
    import eip712
    import secp256k1
    z = eip712.transfer_authorization_digest(_DOMAIN, _MSG)
    pt = secp256k1.ecdsa_recover(z, _R, _S, _RID)
    eip712.pubkey_to_address(pt)


# (key, label, fn, iterations, warmup). Warmup is per-case: the pure-Python EC paths are
# ~30ms each, so a fixed 200-iter warmup would take seconds -- they need only a few.
CASES = [
    ("pure_verdict", "decide_payment (pure decision)", _pure_verdict, 20000, 200),
    ("forecast_basic", "forecast (validate->decide->HMAC receipt)", _forecast_basic, 8000, 200),
    ("keccak256", "keccak256 (pure-Python, one call)", _keccak, 2000, 100),
    ("ecdsa_recover", "secp256k1 ecdsa_recover (valid sig, full)", _ecdsa_recover, 60, 4),
    ("signed_verify", "signed-payment verify (EIP-712 digest+recover+addr)",
     _signed_verify, 50, 4),
]


def _cpu_model():
    # platform.processor() returns the bare arch on Linux; the real model lives in
    # /proc/cpuinfo. A quoted latency number is meaningless without the actual CPU.
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine() or "unknown"


def environment():
    return {"python": sys.version.split()[0], "machine": platform.machine(),
            "system": platform.system(), "processor": _cpu_model()}


def run(scale=1.0):
    """Run every case; return {environment, results:{key: summary}, derived}."""
    results = {}
    for key, label, fn, n, warm in CASES:
        results[key] = dict(measure(fn, max(8, int(n * scale)),
                                    warmup=max(2, int(warm * scale))), label=label)
    return {"environment": environment(), "results": results}


def main(argv=None):
    import argparse
    import json
    p = argparse.ArgumentParser(description="Reproducible COMPUTE latency (no network).")
    p.add_argument("--json", help="write the full report JSON here")
    p.add_argument("--scale", type=float, default=1.0, help="scale iteration counts")
    args = p.parse_args(argv)
    rep = run(scale=args.scale)
    env = rep["environment"]
    print("Blackwall verdict COMPUTE latency -- percentiles, network EXCLUDED")
    print("env: Python %s | %s | %s" % (env["python"], env["processor"], env["machine"]))
    print("-" * 74)
    print("%-46s %8s %8s %8s" % ("path", "p50", "p95", "p99"))
    print("-" * 74)
    for key in ("pure_verdict", "forecast_basic", "keccak256", "ecdsa_recover",
                "signed_verify"):
        r = rep["results"][key]
        print("%-46s %7.1fus %7.1f %7.1f" % (r["label"][:46], r["p50"], r["p95"], r["p99"]))
    print("-" * 74)
    print("NOTE: compute only -- network EXCLUDED. The plain verdict is tens of us. The")
    print("signed-payment verify is ~30ms, dominated by pure-Python secp256k1 EC recovery")
    print("(the cost of stdlib-only crypto) -- it VERIFIES the real signed payment, which a")
    print("metadata scanner does not do at all. That path is opt-in (only on a signed")
    print("payload). Numbers are specific to the env above.")
    if args.json:
        json.dump(rep, open(args.json, "w"), indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
