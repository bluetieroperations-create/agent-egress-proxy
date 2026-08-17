#!/usr/bin/env python3
"""
revert_scan.py -- the settlement-reliability axis's DATA TAP: read an RWA token's FAILED
transfer attempts from public chain history, decode the revert reason, and classify it so
only ISSUER-CAUSED restriction reverts (allowlist / KYC / frozen / paused / compliance)
count toward issuer trust -- never a fat-finger "insufficient balance" or a gas failure.

WHY THIS EXISTS (proven, not assumed). The RWA backfill is survivorship-biased: it ingests
only transfers that LANDED (successes), so `settle_rate` is 1.0 by construction and the
issuer grade can never discriminate. The missing denominator is ATTEMPTS -- specifically
FAILED attempts. A live spike confirmed:
  * Failed transfer txns to an RWA token ARE queryable (Blockscout `filter=to`, `status`).
  * The per-tx detail endpoint returns a DECODED `revert_reason` (the list view nulls it).
  * BUT the reverts observed on freely-transferable RWAs are generic ("ERC20: transfer
    amount exceeds balance"), NOT restriction reverts -- so the raw failure count is noise.
The signal is therefore the RESTRICTION-CLASS revert, which this module isolates.

DORMANT-BUT-READY (mirrors rams_readiness). The classifier + scanner are built and tested;
the axis stays inert until enough restriction-class reverts are observed for an issuer
(`MIN_RESTRICTION_EVIDENCE`). On a freely-transferable issuer (Backed today) it reports
`evidence_sufficient=False` -> contributes nothing. It lights up automatically if a
permissioned issuer (ERC-3643/T-REX, ERC-1404, allowlist) whose transfers actually revert
enters the corpus. Pure classifier + injected transport; HOLD-only philosophy, fail-open.
Stdlib.
"""
from __future__ import annotations

from collections import Counter

from holder_concentration import BLOCKSCOUT_BASES

# Restriction-class revert reasons -- ISSUER-CAUSED transfer blocks. Case-insensitive
# substrings, curated from the permissioned-token standards (ERC-3643/T-REX, ERC-1404,
# ERC-1400, allowlist ERC-20, OZ Pausable). These are the ONLY reverts that speak to
# issuer trust; everything else (balance, allowance, gas) is caller error and dropped.
RESTRICTION_PATTERNS = (
    "not whitelisted", "whitelist", "allowlist", "not allowed", "not allowlisted",
    "not verified", "identity", "onchainid", "kyc", "not eligible", "eligibility",
    "compliance", "not compliant", "transfer not possible", "transferrestriction",
    "restricted", "restriction", "frozen", "freeze", "paused", "pausable",
    "blacklist", "blocklist", "blocked", "sanction", "forbidden",
    "not authorized", "unauthorized", "receiver not", "sender not",
)
# Explicitly NON-issuer reverts (caller error / infra) -- classified so they never leak
# into the restriction bucket even if a substring brushes a pattern.
BALANCE_PATTERNS = ("exceeds balance", "insufficient balance", "amount exceeds balance",
                    "transfer amount exceeds")
ALLOWANCE_PATTERNS = ("exceeds allowance", "insufficient allowance")


def extract_reason(revert_reason):
    """Pull the human revert string out of Blockscout's `revert_reason` shape. Handles the
    decoded form ({parameters:[{name:"reason", value:"..."}]}), a bare string, and the
    undecoded {raw: "0x.."} form. Returns the reason string, or None. NEVER raises."""
    try:
        if revert_reason is None:
            return None
        if isinstance(revert_reason, str):
            return revert_reason or None
        if isinstance(revert_reason, dict):
            params = revert_reason.get("parameters")
            if isinstance(params, list):
                for p in params:
                    if isinstance(p, dict) and p.get("name") == "reason" \
                            and isinstance(p.get("value"), str):
                        return p["value"] or None
                for p in params:                       # any string parameter
                    if isinstance(p, dict) and isinstance(p.get("value"), str):
                        return p["value"] or None
            raw = revert_reason.get("raw")
            if isinstance(raw, str) and raw and raw != "0x":
                return raw                             # undecoded -> classified "unknown"
    except Exception:
        return None
    return None


def classify_revert(reason):
    """PURE: a decoded revert reason string -> its class. `restriction` is issuer-caused
    (the signal); `balance`/`allowance`/`gas` are caller error; `other`/`unknown` are
    unclassified. Restriction is matched FIRST but balance/allowance take precedence when
    an OZ-style message is unambiguous. NEVER raises."""
    if not reason or not isinstance(reason, str):
        return "unknown"
    r = reason.lower()
    # Caller-error classes first: these are precise, common, and must never be mistaken
    # for a restriction (a balance typo is not the issuer's fault).
    if any(p in r for p in BALANCE_PATTERNS):
        return "balance"
    if any(p in r for p in ALLOWANCE_PATTERNS):
        return "allowance"
    if "out of gas" in r or "gas required exceeds" in r:
        return "gas"
    if any(p in r for p in RESTRICTION_PATTERNS):
        return "restriction"
    return "other"


def summarize_reverts(reasons):
    """PURE: a list of decoded reason strings -> class counts. NEVER raises."""
    c = Counter(classify_revert(x) for x in (reasons or []))
    return {"total": sum(c.values()), "restriction": c["restriction"],
            "balance": c["balance"], "allowance": c["allowance"], "gas": c["gas"],
            "other": c["other"], "unknown": c["unknown"]}


# Minimum restriction-class reverts before the settlement-reliability axis will speak for
# an issuer. Below this the axis is DORMANT (evidence-insufficient) -- one revert is noise.
MIN_RESTRICTION_EVIDENCE = 3


def restriction_axis(landed, revert_summary, *, min_evidence=MIN_RESTRICTION_EVIDENCE):
    """PURE: fold a token/issuer's LANDED successes + its revert class-counts into the
    settlement-reliability axis. `restriction_revert_rate` = restriction reverts over
    (successes + restriction reverts) -- the denominator the backfill lacked. The axis is
    DORMANT until `restriction >= min_evidence` (evidence gate); dormant -> contributes
    nothing (rate reported for observability but `evidence_sufficient=False`). NEVER raises.
    """
    rs = revert_summary if isinstance(revert_summary, dict) else {}
    restr = rs.get("restriction") or 0
    landed = landed if isinstance(landed, int) and landed >= 0 else 0
    attempts = landed + restr
    rate = (restr / attempts) if attempts else None
    return {
        "restriction_reverts": restr,
        "landed": landed,
        "attempts": attempts,
        "restriction_revert_rate": rate,
        "evidence_sufficient": restr >= min_evidence,
        "dormant": restr < min_evidence,
    }


class RevertScanner:
    """Live keyless data tap: page a token's FAILED inbound txns and decode each revert
    reason. Two-step because Blockscout only populates the decoded reason on the per-tx
    detail endpoint, not the list. Transport injected for tests: `fetch_txns(token, params)
    -> (items, next_params)` and `fetch_tx(hash) -> detail_dict`. Fail-open."""

    def __init__(self, chain, timeout=8.0, bases=None, fetch_txns=None, fetch_tx=None):
        self.base = (bases or BLOCKSCOUT_BASES).get((chain or "").strip().lower())
        self.timeout = timeout
        self._fetch_txns = fetch_txns
        self._fetch_tx = fetch_tx

    # -- default live transport ------------------------------------------------
    def _live_txns(self, token, params):
        import http_util
        from urllib.parse import urlencode
        if not self.base:
            return ([], None)
        url = "%s/api/v2/addresses/%s/transactions?filter=to" % (self.base, token)
        if params:
            url += "&" + urlencode(params)
        d = http_util.get_json(url, timeout=self.timeout)
        if not isinstance(d, dict):
            return ([], None)
        return (d.get("items") or [], d.get("next_page_params"))

    def _live_tx(self, tx_hash):
        import http_util
        if not self.base:
            return {}
        return http_util.get_json("%s/api/v2/transactions/%s" % (self.base, tx_hash),
                                  timeout=self.timeout) or {}

    def scan_token(self, token, *, max_pages=6, max_details=50):
        """Return {failed, reasons[], summary}. Pages transactions, keeps status=='error'
        hashes, fetches each detail for its decoded reason, classifies. Fail-open: any
        transport error yields what was collected so far (or empties)."""
        fetch_txns = self._fetch_txns or self._live_txns
        fetch_tx = self._fetch_tx or self._live_tx
        failed_hashes, params, pages = [], None, 0
        try:
            while pages < max_pages:
                items, params = fetch_txns(token, params)
                for t in (items or []):
                    if isinstance(t, dict) and t.get("status") == "error":
                        h = t.get("hash")
                        if h:
                            failed_hashes.append(h)
                pages += 1
                if not params:
                    break
        except Exception:
            pass
        reasons = []
        for h in failed_hashes[:max_details]:
            try:
                detail = fetch_tx(h)
                reasons.append(extract_reason(
                    detail.get("revert_reason") if isinstance(detail, dict) else None))
            except Exception:
                reasons.append(None)
        return {"failed": len(failed_hashes), "reasons": reasons,
                "summary": summarize_reverts(reasons)}


def scan_corpus_issuers(events, scanner, *, max_tokens_per_issuer=25):
    """Aggregate restriction-revert class-counts PER ISSUER by scanning each issuer's
    tokens (from the RWA ledger events). Returns {issuer: summary}. Targeted (caps tokens
    per issuer) and fail-soft -- one bad token never aborts the run. Pure over an injected
    `scanner.scan_token`."""
    by_issuer = {}
    for e in events or []:
        if not isinstance(e, dict) or e.get("event", "buy") != "buy":
            continue
        iss, tok = e.get("issuer"), e.get("token")
        if iss and tok:
            by_issuer.setdefault(iss, set()).add(tok)
    out = {}
    for iss, toks in by_issuer.items():
        agg = Counter()
        for tok in list(toks)[:max_tokens_per_issuer]:
            try:
                s = scanner.scan_token(tok)["summary"]
            except Exception:
                continue
            for k, v in s.items():
                agg[k] += v
        out[iss] = dict(agg)
    return out


def main(argv=None):
    """CLI: scan the RWA corpus's tokens for restriction-class reverts and write a
    {issuer: revert_summary} JSON (the input build_issuer_grades folds as the dormant
    settlement-reliability axis). `python revert_scan.py rwa.jsonl --chain ethereum
    [--out reverts.json] [--max-tokens 25]`."""
    import argparse
    import json
    import sys
    from rwa_ledger import RwaLedger
    ap = argparse.ArgumentParser(description="Scan the RWA corpus for restriction reverts.")
    ap.add_argument("ledger", help="path to the rwa_ledger JSONL corpus")
    ap.add_argument("--chain", default="ethereum")
    ap.add_argument("--out", default=None, help="write {issuer: summary} JSON here")
    ap.add_argument("--max-tokens", type=int, default=25)
    args = ap.parse_args(argv)
    events = RwaLedger(args.ledger).load()
    summaries = scan_corpus_issuers(events, RevertScanner(args.chain),
                                    max_tokens_per_issuer=args.max_tokens)
    blob = json.dumps({"chain": args.chain, "issuers": summaries}, indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(blob + "\n")
    sys.stdout.write(blob + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
