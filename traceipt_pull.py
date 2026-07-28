#!/usr/bin/env python3
"""
traceipt_pull.py -- pull signed Traceipt receipts and fold them into reputation.

This closes the receipts -> reputation flywheel (integration B's live half): fetch
each receipt ENVELOPE from Traceipt (`GET /receipts/{id}`), verify its Ed25519
signature against the issuer JWKS (`GET /jwks.json`), and ingest ONLY the
authenticated payment receipts into a `ReputationStore`. Authentication is
fail-CLOSED: a receipt whose signature doesn't verify is dropped, never ingested
(`traceipt_ingest.envelopes_to_transfers` + `traceipt_verify.verify_envelope`).

Traceipt exposes no bulk receipt listing, so receipts are pulled BY ID -- the agent
already holds its `receipt_id`s from its own `POST /receipts` responses. Pass them
via `--receipt-id` (repeatable) or `--ids-file` (one id per line).

    python traceipt_pull.py --base-url https://api.traceipt.xyz \
        --store rep.db --ids-file my_receipt_ids.txt

Stdlib only (pure-Python Ed25519 verify via traceipt_verify -> cdp_auth). The HTTP
transport is injectable (`_get`, `_fetch_jwks`) for tests.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from traceipt_ingest import envelopes_to_transfers
from traceipt_verify import fetch_jwks


def _urllib_get(url, timeout):
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def fetch_envelope(base_url, receipt_id, *, timeout=10, _get=None):
    """`GET /receipts/{id}` -> the signed envelope dict, or None on any non-200 /
    error / non-object body. NEVER raises."""
    try:
        rid = urllib.parse.quote(str(receipt_id), safe="")
        url = base_url.rstrip("/") + "/receipts/" + rid
        status, body = (_get or _urllib_get)(url, timeout)
        return body if status == 200 and isinstance(body, dict) else None
    except Exception:
        return None


def pull_envelopes(base_url, receipt_ids, *, timeout=10, _get=None):
    """Fetch many receipt envelopes by id; drop any that couldn't be fetched."""
    out = []
    for rid in receipt_ids or []:
        env = fetch_envelope(base_url, rid, timeout=timeout, _get=_get)
        if env is not None:
            out.append(env)
    return out


def pull_and_ingest(store, base_url, receipt_ids, *, jwks=None, timeout=10,
                    _get=None, _fetch_jwks=None):
    """Fetch receipts by id, verify signatures against the issuer JWKS, and ingest
    the authenticated payment receipts into `store`.

    Returns a stage-by-stage summary: {requested, fetched, verified, ingested}
    (and `reason` when nothing could be authenticated). `verified` < `fetched`
    means signatures failed or the receipt wasn't a payment; `ingested` < `verified`
    means duplicates (idempotent on tx_hash). Fail-CLOSED: with no JWKS, nothing is
    ingested."""
    ids = list(receipt_ids or [])
    if jwks is None:
        jwks = (_fetch_jwks or fetch_jwks)(base_url, timeout=timeout)
    envelopes = pull_envelopes(base_url, ids, timeout=timeout, _get=_get)
    if not isinstance(jwks, dict) or "keys" not in jwks:
        return {"requested": len(ids), "fetched": len(envelopes),
                "verified": 0, "ingested": 0, "reason": "jwks_unavailable"}
    transfers = envelopes_to_transfers(envelopes, jwks)   # signature-gated
    ingested = store.ingest_transfers(transfers) if transfers else 0
    return {"requested": len(ids), "fetched": len(envelopes),
            "verified": len(transfers), "ingested": ingested}


def _read_ids(args):
    ids = list(args.receipt_id or [])
    if args.ids_file:
        with open(args.ids_file, "r", encoding="utf-8") as f:
            ids.extend(line.strip() for line in f if line.strip())
    return ids


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Pull signed Traceipt receipts into a Blackwall reputation store.")
    p.add_argument("--base-url", required=True, help="Traceipt base URL, e.g. https://api.traceipt.xyz")
    p.add_argument("--store", required=True, help="path to the ReputationStore SQLite db")
    p.add_argument("--receipt-id", action="append", help="a receipt id to pull (repeatable)")
    p.add_argument("--ids-file", help="file with one receipt id per line")
    p.add_argument("--timeout", type=int, default=15)
    args = p.parse_args(argv)

    ids = _read_ids(args)
    if not ids:
        sys.stderr.write("traceipt_pull: no receipt ids (pass --receipt-id or --ids-file)\n")
        return 2

    from reputation_store import ReputationStore
    store = ReputationStore(args.store)
    summary = pull_and_ingest(store, args.base_url, ids, timeout=args.timeout)
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    dropped = summary["fetched"] - summary["verified"]
    if dropped:
        sys.stdout.write("note: %d fetched receipt(s) were NOT authenticated "
                         "(bad signature / not a payment) and were dropped.\n" % dropped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
