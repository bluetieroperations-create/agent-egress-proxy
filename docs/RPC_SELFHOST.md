# Running our own RPC endpoint

The simulation gates (`transfer_sim.py`, `settlement_sim.py`) are the first part of
Blackwall that puts a **query leak** on the hot path: every check tells whichever RPC we
use *"this payer is about to pay this payee this amount."* That is precisely the leak
`readiness.py` exists to avoid ("no third-party call, no query leak"), so the simulation
ships with an endpoint we control.

## What `rpc_node.py` is — and is not

It is **not** an Ethereum node. Syncing one is terabytes of state and days of work, and a
node is *operated*, not imported. `rpc_node.py` is the front door that makes running your
own node a one-line config change, and shrinks the leak immediately either way.

| Property | Effect |
|---|---|
| **Single egress point** | Every chain read goes through one auditable process, not scattered calls to a provider (the `egress_proxy.py` idea, applied to chain reads). |
| **Cache** | A repeat check is served locally and never re-leaks. **Measured live: 3 upstream calls cold, 0 on the repeat pass, identical verdicts.** |
| **Single-flight** | *Concurrent* duplicates coalesce into ONE upstream call. The cache alone only stops **sequential** re-leaks — an audit measured 8 parallel identical checks producing 8 disclosures. **Now: 8 → 1.** |
| **Method allowlist** | Only read-only methods forward. A caller cannot use our endpoint to broadcast a transaction or reach `admin_`/`personal_`/`debug_`. |
| **Self-host switch** | Point `--upstream` at your own node and third-party leakage is **zero**, with no code change. |

## Run it

```sh
python rpc_node.py --upstream https://<your-node-or-provider> --port 8599
```

Then point Blackwall at it — the env vars already accept any URL:

```sh
export BLACKWALL_RWA_RPC_URL=http://127.0.0.1:8599
export BLACKWALL_BASE_RPC_URL=http://127.0.0.1:8599
export BLACKWALL_SETTLEMENT_SIM=1
```

`GET /healthz` reports cache size and hit/miss counts.

## Staleness is a safety tradeoff

These reads back a **security decision**. A payee blacklisted five minutes ago must not be
served a stale "fine" answer, so the TTL defaults to **30s** (~2 blocks) and `--ttl 0`
disables caching entirely. Do not raise it without deciding how stale a compliance answer
is allowed to be.

Two deliberate caching rules follow from the same reasoning:

- An **EVM revert is cached** (30s). It is a deterministic answer for that block — and a
  blacklisted payee is *exactly* the case that reverts, so skipping it would re-leak the
  most sensitive query on every check.
- A **bare error is not cached** (rate limit, node hiccup). Transient failures must not be
  pinned for the TTL.

## Getting to zero third-party leakage

`--upstream` is the only thing that talks to the outside world. In increasing order of
independence:

1. **A provider** (default today) — leak reduced by caching, not eliminated.
2. **A private/dedicated endpoint** — leak limited to one contracted party.
3. **Your own node** (Reth, Geth, Erigon; Base runs an OP-stack node) — **zero third-party
   leakage.** This is an operational commitment: disk, sync time, monitoring.

Blackwall needs only `eth_call` at `latest` plus a few read methods, so even a pruned
node is sufficient — archive state is not required.

## Measured leak reduction (live, mainnet USDC)

| Check | Upstream disclosures |
|---|---|
| Cold, blacklisted payee | 2 (target + control) |
| Cold, clean payee | 1 (control skipped — target succeeded) |
| Repeat of either | **0** |
| 6 concurrent identical checks | **2** (was 6) |

Ten checks cost five disclosures instead of fifteen.

## Honest limitations

- The cache is **per-process and in-memory**; restarting clears it, and multiple server
  processes each keep their own.
- With a provider upstream, a *first-time* payer/payee pair is still disclosed. Only
  self-hosting removes that.
- The allowlist protects our endpoint from misuse; it does not authenticate callers. Bind
  to `127.0.0.1` (the default) or put it behind your own auth.
