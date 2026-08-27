# Ecosystem history — the recorded series

## The idea in one paragraph

Public data is searchable, so someone always found it first. Nineteen candidates
died that way (see `docs/SPANISH_DIG.md`). Data you *measure* is different: it did
not exist until the probe ran, and it cannot be reconstructed afterwards. A
competitor who copies this idea in six months still will not have the six months.

`directory_liveness.py` already produces that kind of measurement — 195 x402
endpoints, each classified by whether its payment requirements can actually be
parsed. This turns that one-off reading into a dated, append-only series and
derives the signals that only exist across time.

## What it gives you that one snapshot cannot

- `diff(prev, curr)` — who appeared, who disappeared, who went from payable to
  unpayable (an endpoint can be perfectly UP and still unparseable), and per-host
  settlement growth.
- `churn_rate(prev, curr)` — what share of sellers stopped being advertised.
- `survival(snapshots)` — the mortality table: first seen, last seen, days
  observed per host. This is the artifact with no substitute.
- `still_alive(snapshots, as_of)` — point-in-time reconstruction, so a backtest
  cannot see the future.

## Rules baked in

- **Append-only.** `store()` raises rather than overwrite an existing date.
  Replacing a past measurement destroys the only thing here that is unbuyable.
- **Keyed on host, not URL**, so an endpoint moving its path is not counted as a
  death plus a birth.
- **Settlement growth clamps at zero**, because cumulative on-chain history cannot
  fall; a drop means the backfill window moved.
- **`disappeared` is not `dead`.** Missing from the directory and probed-and-silent
  are different facts and are kept apart.

## Baseline on record

`data/snapshots/2026-08-25.json` — 195 hosts, 73 payable, 122 not.

## Running it

Re-survey and store this month's reading:

```sh
python directory_liveness.py --out data/liveness.json
python -c "import sys,json;sys.path.insert(0,'ecosystem_history');import history as h;from datetime import date;h.store('data/snapshots',json.load(open('data/liveness.json')),date.today())"
```

Tests (run from this directory):

```sh
cd ecosystem_history && python -m unittest test_history
```

## Honest limitation

This measures the x402 seller ecosystem, which is small — 195 endpoints. The
*mechanism* (record a decaying public surface on a schedule; sell the history) is
what generalizes; this is the proof that we can run it. Whether x402 becomes a
market worth selling into is an open bet, not an established one.
