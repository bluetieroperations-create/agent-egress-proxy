# Blackwall for the x402 agent-marketplace — peer-group cross-check

The signal-depth feature for marketplaces: **is this counterparty priced far above
comparable counterparties for the same resource class?** Per-class pricing (built
earlier) compares a counterparty to its *own* history; peer-group compares it to
the *market*. Together they close the pricing blind spot.

## The blind spot it closes
Per-class alone misses a counterparty that has **always** overcharged — its own
history is self-consistent, so nothing flags. Example: a weather-API provider that
has always charged $0.10/call looks normal per-class, but every *other* provider
charges $0.001 — a **100× market outlier**. Peer-group catches exactly this. It
also gives a **cold-start** price read on a brand-new counterparty (compare the
quote to the class market rate, instead of "price unknown").

## How it works (conservative by design)
- A **peer-group market median** per resource class = the median of each
  counterparty's own median price, taken across **distinct counterparties**
  (`peer_group_median`, `build_peer_class_index`). Median-of-medians so one
  high-volume actor can't move the market; a class needs ≥ `MIN_PEER_COUNTERPARTIES`
  (3) distinct peers or it's omitted (no market).
- The verdict compares the counterparty's **own class median** (or, at cold-start,
  the quoted amount) to that market median (`peer_anomaly_ratio`). At ≥
  `PEER_HOLD_RATIO` (3×) above market it **escalates to HOLD** and adds a reason;
  the ratio is surfaced as `signals.peer_price_ratio`.
- **Expensive is not fraud.** A peer outlier **only blocks an automatic GO
  (→ HOLD)** — it never STOPs on its own, and it never *grants* a GO. This is the
  load-bearing safety property.

## Using it
Peer-group is **opt-in**: pass a `peer_index` (`{resource_class: market_median}`)
to `forecast(...)` / `BlackwallServer(peer_index=...)`, and have callers send
`resource_class` (the shared category, e.g. `"weather-call"`) on the request.
Build the index with `build_peer_class_index(observations)` where each observation
is `{counterparty, resource_class, amount}` — sourced from marketplace price data
or from Blackwall's own settlements (see the follow-up below). With no `peer_index`
or no `resource_class`, the check is simply inactive (no effect, not advertised).

> **Taxonomy is the hard part.** `resource_class` must mean the same thing across
> *different* counterparties (a shared category), unlike the per-counterparty
> `resource`/invoice id. A bad grouping (apples vs oranges) is worse than none —
> get the class key right.

## Sybil bounds (audited)
The market rate can be influenced by fake counterparties, but the blast radius is
bounded and always toward caution:
- **Distinct-counterparty requirement** — one actor's many observations count as
  one peer; a class needs ≥3 distinct counterparties.
- **Down-manipulation** (fake cheap peers to flag a rival) → at worst extra
  **HOLDs** (human review). Safe.
- **Up-manipulation** (fake expensive peers to normalize an overprice) → the
  check simply doesn't fire → **no worse than not having peer-group**. It can
  never cause a wrongful auto-release.
- Because `peer_hold` only *adds* caution (never grants GO, never sets STOP), a
  manipulated market cannot produce a wrong approval or a wrong block.

## Status / follow-ups
- ✅ **Engine built & tested** — pure `peer_group_median` / `build_peer_class_index`
  / `peer_anomaly_ratio`, verdict integration (HOLD-only), `forecast`/server
  `peer_index` injection, `resource_class` on the request. Suite green.
- ⏭ **Self-populating index from the ledger** — `build_peer_class_index` consumes
  `{counterparty, resource_class, amount}` observations, but Blackwall's ledger
  currently records `resource` (per-counterparty), not the shared `resource_class`,
  and aggregates per-counterparty. Auto-building/refreshing the index from the
  flywheel needs: (a) recording `resource_class` on verdicts, (b) a
  cross-counterparty class-observation export, (c) a periodic rebuild (the
  rolling-aggregate infra). Until then, inject a `peer_index` built from external
  market data.
- ⏭ **Per-cp median hardening** — `build_peer_class_index` doesn't yet require
  distinct *payers* within a single counterparty's median, so a fake counterparty
  could self-wash its own contributed median. Bounded by the distinct-*counterparty*
  requirement and the HOLD-only effect; harden if peer data becomes adversarial.
