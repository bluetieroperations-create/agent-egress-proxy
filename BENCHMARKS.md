# Blackwall latency — compute, honest, reproducible

**These are COMPUTE percentiles with network EXCLUDED. Reproduce with `python bench.py`;
always quote them next to the environment they were measured on.** This file exists so a
number can be stated *without getting burned* — it is deliberately not a marketing claim
and not a comparison.

## Why it's framed this way (the traps — all three bit us here)

1. **Percentiles, not mean/max.** p50 is stable to ~1% across runs, but a single call's
   *max* swings 10–40× on OS scheduling jitter. Reporting mean/max reads as "flaky" even
   when the operation isn't. Only p50/p95/p99 are honest.
2. **Network is excluded and dominates.** The plain verdict is tens of microseconds; a
   real HTTPS round-trip is **10–100 ms** and dwarfs it. Any "sub-millisecond including
   HTTP" figure is in-process, not deployed. We do not claim network time.
3. **Measure the real path — never estimate, and beware fast-fail.** An earlier draft of
   this file published **~1.7 ms** for signed-payment verify, derived from a keccak-count
   guess and an `ecdsa_recover` micro-benchmark that used an *invalid* signature. Invalid
   `(r,s)` short-circuits **before** the expensive EC scalar-multiplication, under-measuring
   the real recovery by ~20×. The real number, measured with a **valid** signature, is
   **~33 ms**. If a benchmark can fast-fail, it will — and it will lie low.

## Numbers (env: Python 3.11.15, Intel Xeon @ 2.80 GHz, x86_64, Linux — one container)

| Path | p50 | p95 | p99 |
|---|---|---|---|
| `decide_payment` (pure decision) | **~17 µs** | ~28 µs | ~60 µs |
| `forecast` (validate → decide → HMAC receipt) | **~90 µs** | ~180 µs | ~250 µs |
| `keccak256` (pure-Python, one call) | ~550 µs | ~680 µs | ~880 µs |
| `secp256k1` `ecdsa_recover` (valid sig, full) | **~30 ms** | ~41 ms | ~45 ms |
| **signed-payment verify** (EIP-712 digest + recover + address) | **~33 ms** | ~45 ms | ~47 ms |

Numbers drift with hardware/Python — re-run `bench.py` and re-stamp before quoting.

## The one thing to understand (and it's a real tradeoff, not a win)

The **plain verdict is ~17–90 µs** — genuinely fast. But **signed-payment verification is
~33 ms**, dominated by **pure-Python secp256k1 EC scalar-multiplication** in the signer
recovery (keccak is a minor ~2 ms of it, contrary to an earlier note). That is **slower
than a typical network round-trip** — a real cost, stated plainly, not spun.

Two things keep it honest and bounded:
- It **verifies the agent's actual signed payment** (recovers the real EIP-3009 signer and
  confirms it matches what was scored) — a capability a metadata-only scanner does not have
  at all. "Slower there" is "does something there," but 33 ms is 33 ms.
- It is **opt-in / conditional**: it only runs when a caller supplies a signed payload
  (payload-sim Phase 2). A plain forecast never pays it.

## If this path is on your hot path

The bottleneck is `secp256k1.ecdsa_recover` (~30 ms), **not** keccak. Options:
1. **Don't run it inline.** For high-throughput callers, treat signed-payment verification
   as an async/second-stage check, not a blocking pre-sign gate — the fast verdict
   (~90 µs) still gates in-line.
2. **Cache** per-(nonce, signature) within a request/session so a repeat doesn't re-recover.
3. **Only if stdlib-only is explicitly relaxed:** a native lib (`coincurve`/`pycryptodome`)
   does the recovery in microseconds (~1000× faster). This is a deliberate architectural
   change — the pure-Python secp256k1 exists precisely to keep the repo dependency-free.
   Do not do this without a decision to change that rule.

## Reproduce

```sh
python bench.py                 # text table + environment stamp
python bench.py --json out.json # machine-readable
```

`test_bench.py` guards the harness (percentile ordering, every case runs, environment
stamped) — it makes **no absolute-latency assertion**, because a microsecond threshold in
a test is itself a flake. That is intentional.
