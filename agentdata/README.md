# agentdata — sell enriched data to AI agents, per call, over x402

A small **platform** for the A2 play: *buy/aggregate a dataset wholesale →
enrich it → resell it per-call to autonomous agents over [x402], on both an
HTTP endpoint and an MCP tool.* Agents self-discover and pay; no human
marketing funnel.

**Product #1 — Property Intelligence** ships here. Products #2 (Legal-Citation
Verification) and #3 (Semantic Change-Watch) are designed to drop in as sibling
files with **zero changes** to money, transport, cache, or analytics.

[x402]: https://github.com/coinbase/x402

---

## Why it's built as a platform, not a script

Everything reusable lives in `core/`; each product is a thin plug-in. The
per-call lifecycle is defined **once** (`core/platform.py`):

```
validate → [x402 gate] → cache-read → fetch+enrich (on miss)
        → cache-write → [settle] → sales-log → JSON
```

- **`core/money.py`** — integer atomic-unit math (no floats on money).
- **`core/payment.py`** — x402 seller-side: build the `402` offer, verify the
  `X-PAYMENT` retry, settle via a **pluggable facilitator** (mock offline;
  Coinbase/Cloudflare in prod). We never reinvent settlement — that's the
  commodity.
- **`core/storage.py`** — the **cache (the moat/flywheel)** + the JSONL **sales
  log** (the analytics exhaust).
- **`core/enrich.py`** — the enrichment layer (stub offline; Claude in prod).
  This is what an agent pays for instead of doing the aggregation itself.
- **`core/product.py`** — the `DataProduct` plug-in contract + registry.
- **`core/server.py`** (HTTP) and **`core/mcp.py`** (MCP stdio) — thin
  transports over the one pipeline.

## Run it (offline, zero setup)

```sh
# from the repo root

# HTTP API (paid gate, mock facilitator — no keys, no network)
python -m agentdata.run_http
#   POST /v1/property/lookup   (402 without X-PAYMENT, 200 with)
#   GET  /                     (product catalog)
#   GET  /healthz

# MCP server (stdio) — wire into Claude Desktop / Cursor as a stdio command
python -m agentdata.run_mcp

# iterate on data logic without paying:
AGENTDATA_REQUIRE_PAYMENT=false python -m agentdata.run_http
```

Example paid call:

```sh
XP=$(python -c "import base64,json;print(base64.b64encode(json.dumps({'scheme':'exact','network':'base','payload':{'authorization':{'value':'200000'}}}).encode()).decode())")
curl -X POST localhost:8402/v1/property/lookup \
  -H "X-PAYMENT: $XP" -d '{"address":"500 Congress Ave, Austin TX"}'
```

## Tests

```sh
python -m unittest discover -s agentdata/tests -t . -v   # 26 tests, all offline
```

## Configuration

Copy `config.example.json` → `config.json`, set `AGENTDATA_CONFIG=config.json`.
Key fields: `pay_to` (your seller wallet), `facilitator` (`mock` | `coinbase` |
`cloudflare`), `require_payment`, `enricher` (`stub` | `claude`). Common fields
also read from env (`AGENTDATA_PAY_TO`, `AGENTDATA_FACILITATOR`, …).

---

## Adding product #2 or #3 (the whole point)

1. **Data source** — implement `DataSource.fetch(query) -> dict` in
   `data_sources/` (e.g. `CourtListenerSource` for legal, a crawler for watch).
2. **Product** — subclass `DataProduct` in `products/` (like
   `products/property_intel.py`): set `name`, `price`, `input_fields`, and
   override `fetch` + `compute_flags` (and `enrich` if you want more than the
   default summary).
3. **Register** — add two lines in `core/platform.build_default_platform`:
   ```python
   registry.register(LegalCitation()); sources["legal"] = CourtListenerSource()
   ```

It's now live on **both** the HTTP endpoint and as an MCP tool, priced and
gated, cached, and logged — no other changes.

### Planned products
- **#2 Legal-Citation Verification** — `{claim, citations}` → each citation
  real? / supports the claim? / source link. Data: CourtListener/RECAP (free,
  public → no licensing risk).
- **#3 Semantic Change-Watch** — `{source, since}` → structured semantic diff.
  Inherently recurring call volume.

---

## From here to revenue (honest checklist)

This is the buildable core. To actually earn:

1. **Swap the stub source for a real one.** `StubPropertySource` is deterministic
   fake data. Replace with a licensed wholesale feed (ATTOM-class) or county
   open-data + permit datasets. **Verify the source's ToS permits per-call
   resale before you build on it** — this is the make-or-break for #1.
2. **Point `facilitator` at Coinbase/Cloudflare** and set your real `pay_to`.
3. **List** the endpoint + MCP tool in the x402 / MCP registries so agents
   discover it (this is your distribution).
4. **Watch the sales log** (`agentdata_sales.log`) — it's per-product,
   per-resource revenue and cache-hit data out of the box.

> Product #1's moat is the enriched, normalized, **cached** corpus that grows
> with every call — not the raw data. Aggregate multiple sources so you're not
> reselling a single feed anyone else can license.
