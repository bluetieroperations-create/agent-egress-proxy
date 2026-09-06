# Durable ledger mirror (`remote_ledger.py`)

Keep the verdict→outcome ledger across a restart on a host with **no persistent
disk**, without handing a third party your customers' payment intent.

## The problem, measured

On the live free-tier deploy the container filesystem resets on restart. What
that actually costs was worth measuring rather than assuming:

* The **SQLite reputation store needs no durability.** Its only writer is
  `reputation_store.ingest_from_chain`, gated behind `BLACKWALL_INGEST`, which
  the deploy sets to `0`. Verified against the live service: settlement and
  distinct-payer counts for five payees were byte-identical to the baked
  `data/reputation_seed.db.gz` (46,031 rows), so it is read-only in production
  and reproducible from the image.
* The **ledger is the only thing that accumulates.** Every verdict lands via
  `ledger.record_verdict` (`blackwall.py:1710`), every outcome via
  `POST /v1/report-outcome` → `record_outcome`. `aggregate_counterparties` folds
  it back into reputation, including the recency-weighted `recent_dispute_rate`
  behind the `going_bad` gate.

So this is not "persist a database". It is "persist an append-only log of a few
hundred bytes per verdict".

## Design

`DurableEventLedger` subclasses `EventLedger` and overrides exactly two things:
the single write point (`_append`) and boot (`hydrate`). Every reader —
`read_events`, `aggregate`, `LedgerReputationSource`, the `going_bad` gate —
keeps reading the **local file**, unchanged.

* **Encrypted before it leaves the process.** AES-256-GCM per record, random
  12-byte nonce, envelope `BWL1.<b64 nonce>.<b64 ct>` with the version bound as
  AAD. The provider stores opaque blobs and timestamps. Verified live: none of
  the counterparty, amount, asset, outcome, or even the JSON field names appear
  in the stored rows.
* **The key is derived, not used raw** (`HMAC-SHA256(secret, KEY_LABEL)`), and
  `load_key` refuses a secret reused from `BLACKWALL_SIGNING_SEED` or
  `BLACKWALL_RECEIPT_KEY`.
* **At-least-once, deliberately.** A duplicate row is harmless —
  `aggregate_counterparties` dedupes settlements by tx hash (`ledger.py:134`) —
  while a lost row erases an outcome, and a missing dispute makes a bad
  counterparty look *better* than it is. So a transient failure is retried.
* **Fail-open on the payment path.** Local write happens first and
  unconditionally; the mirror is a single serialized worker; failures are counted
  (`stats`), never raised. A KV outage at boot leaves the service running with no
  history, which reads as cold-start HOLDs — fail-safe.
* **Bounded backlog.** `put_nowait` with a capped queue: the durability feature
  must not become the thing that OOMs the service it protects, and it must never
  block `record_verdict`.
* **Byte-exact restore.** `seal` serializes exactly as `_append` does, so a
  hydrate `diff`s clean against the file it replaced.

## No plaintext fallback

AES-GCM is not in the stdlib and this module does **not** hand-roll one. If the
cipher is unavailable the service **refuses to boot** (exit 2) rather than
mirroring in the clear or silently persisting nothing.

`ensure_cipher()` proves the cipher works with a real round trip at startup,
because "installed" is not "working": a broken native build imports fine and then
raises `pyo3_runtime.PanicException` on use — which derives from `BaseException`,
so a plain `except Exception` does not catch it. That escaped every fail-open
guard here and surfaced as a 500 on the payment path until `_guard` was added.
Found by running against a genuinely broken install, not by reading the code.

## Configure

All three are required together; setting some but not all is a fatal config error
(an operator who set two of three believes their data is durable).

| var | meaning |
|---|---|
| `BLACKWALL_LEDGER_KV_URL` | Redis-REST base URL (e.g. an Upstash database URL) |
| `BLACKWALL_LEDGER_KV_TOKEN` | bearer token |
| `BLACKWALL_LEDGER_KEY` | 32 bytes, 64 hex chars or base64. **Must differ from every other secret.** |
| `BLACKWALL_LEDGER_KV_KEY` | list key (default `blackwall:ledger`) |
| `BLACKWALL_LEDGER_MAX_HYDRATE` | cap on rows replayed at boot (default 50000, keeps the NEWEST) |

Generate a key without printing it into a shell history:

```sh
python3 -c "import os;print(os.urandom(32).hex())"
```

## Sharp edges

* **Lose `BLACKWALL_LEDGER_KEY` and the log is unreadable.** Rows sealed under an
  old key are skipped and counted, and the boot banner says so
  (`skipped N undecryptable`) — the service still starts, with that history gone.
  Back the key up somewhere other than the KV provider.
* **Volume.** One RPUSH per ledger event (a verdict and its outcome are two).
  A free KV tier's monthly command budget is the real ceiling on request volume,
  not the engine.
* **Not a backup.** It restores the ledger, nothing else. The SQLite store comes
  from the image by design.
