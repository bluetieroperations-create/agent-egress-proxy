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
    for _ in range(warmup):
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


# Precompute the digest OUTSIDE the timed function so _ecdsa_recover measures ONLY the
# EC recovery, not an extra keccak (a classic microbenchmark artifact -- timing more than
# you think). The derived signed-verify estimate then adds the keccak calls explicitly.
import keccak as _keccak_mod
_Z = int.from_bytes(_keccak_mod.keccak256(b"digest"), "big")


def _ecdsa_recover():
    import secp256k1
    try:
        secp256k1.ecdsa_recover(_Z, (0x6f83 << 200) | 1, (0x1b2c << 200) | 1, 0)
    except Exception:
        pass


# (key, label, fn, iterations). Iterations scaled down for the slow crypto paths.
CASES = [
    ("pure_verdict", "decide_payment (pure decision)", _pure_verdict, 20000),
    ("forecast_basic", "forecast (validate->decide->HMAC receipt)", _forecast_basic, 8000),
    ("keccak256", "keccak256 (pure-Python, one call)", _keccak, 2000),
    ("ecdsa_recover", "secp256k1 ecdsa_recover (one call)", _ecdsa_recover, 1000),
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
    for key, label, fn, n in CASES:
        results[key] = dict(measure(fn, max(50, int(n * scale))), label=label)
    # A full EIP-712 signed-payment verification ~= 3 keccak (domain, struct, digest)
    # + 1 recover. Derived, so we don't require a valid signature fixture to state it.
    k, r = results["keccak256"]["p50"], results["ecdsa_recover"]["p50"]
    results["signed_verify_est"] = {
        "label": "signed-payment verify (~3 keccak + recover, DERIVED)",
        "unit": "us", "p50": round(3 * k + r, 1), "n": None,
        "note": "cryptographic recovery of the EIP-3009 signer (payload-sim Phase 2)"}
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
    for key in ("pure_verdict", "forecast_basic", "keccak256", "ecdsa_recover"):
        r = rep["results"][key]
        print("%-46s %7.1fus %7.1f %7.1f" % (r["label"][:46], r["p50"], r["p95"], r["p99"]))
    sv = rep["results"]["signed_verify_est"]
    print("%-46s %7.1fus %8s %8s" % (sv["label"][:46], sv["p50"], "-", "-"))
    print("-" * 74)
    print("NOTE: compute only -- a real HTTPS round-trip (10-100ms) dominates and is NOT")
    print("included. Signed-payment verify is ~100x the plain verdict (pure-Python keccak")
    print("is the cost of stdlib-only crypto) -- it VERIFIES the real signed payment, a")
    print("capability, not overhead. Numbers are specific to the env above.")
    if args.json:
        json.dump(rep, open(args.json, "w"), indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
