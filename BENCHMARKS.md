# Blackwall latency — compute, honest, reproducible

**These are COMPUTE percentiles with network EXCLUDED. Reproduce with `python bench.py`;
always quote them next to the environment they were measured on.** This file exists so a
number can be stated *without getting burned* — it is deliberately not a marketing claim
and not a comparison.

## Why it's framed this way (the traps)

1. **Percentiles, not mean/max.** p50 is stable to ~1% across runs here, but a single
   call's *max* swings 10–40× on OS scheduling jitter. A benchmark that reports mean or
   max reads as "flaky" even when the operation isn't. Only p50/p95/p99 are honest.
2. **Network is excluded and dominates.** The verdict compute is tens of microseconds; a
   real HTTPS round-trip is **10–100 ms** and dwarfs it. Any "sub-millisecond, including
   HTTP" figure is an in-process measurement, not a deployed one. We do not claim network
   time — it isn't ours.
3. **One number would lie.** A plain verdict and a full signed-payment verification differ
   ~100× (see below), so a single headline figure hides the path that matters.

## Numbers (env: Python 3.11.15, Intel Xeon @ 2.80 GHz, x86_64, Linux — one container)

| Path | p50 | p95 | p99 |
|---|---|---|---|
| `decide_payment` (pure decision) | **~17 µs** | ~30 µs | ~60 µs |
| `forecast` (validate → decide → HMAC receipt) | **~85 µs** | ~150 µs | ~190 µs |
| `keccak256` (pure-Python, one call) | ~525 µs | ~640 µs | ~880 µs |
| `secp256k1` `ecdsa_recover` (one call) | ~145 µs | ~195 µs | ~245 µs |
| **signed-payment verify** (~3 keccak + recover, derived) | **~1.7 ms** | — | — |

Numbers drift with hardware/Python — re-run `bench.py` and re-stamp before quoting.

## The one thing to understand

The **plain verdict is ~17–85 µs**. The **signed-payment verification path is ~1.7 ms** —
~100× slower — because it runs **pure-Python `keccak256`** (~525 µs/call) to recover the
real EIP-3009 signer. That cost is the price of the repo's **stdlib-only** rule (no native
keccak; `hashlib.sha3_256` is FIPS SHA3, not Ethereum keccak, so it can't substitute).

This is a **capability, not overhead**: that path cryptographically confirms the agent's
*actual signed payment* matches what was scored — something a metadata-only scanner does
not do at all. "Slower there" means "does more there."

## If this path ever becomes hot

`keccak256` is the bottleneck (not the ECDSA recovery). Options, in order of preference:
1. Leave it — it only runs when a caller supplies a signed payment (opt-in), and 1.7 ms is
   still far under a network round-trip.
2. Cache/skip re-hashing within a request where safe.
3. Only as a last resort, and only if the constraint is relaxed: a native keccak
   (`pysha3`/`pycryptodome`) is ~4× faster — but that breaks stdlib-only, a deliberate
   architectural choice. Do not do this without a decision to change that rule.

## Reproduce

```sh
python bench.py                 # text table + environment stamp
python bench.py --json out.json # machine-readable
```

`test_bench.py` guards the harness (percentile ordering, every case runs, environment
stamped) — it makes **no absolute-latency assertion**, because a microsecond threshold in
a test is itself a flake. That is intentional.
