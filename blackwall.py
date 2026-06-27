#!/usr/bin/env python3
"""
blackwall.py -- pre-signature payment-verdict service (x402 integration, step 1).

Blackwall sits in the PRE-SIGNATURE window of an x402 flow: the agent has
received a `402 Payment Required` and, before it signs the payment, asks
Blackwall whether to proceed. Blackwall returns a verdict -- GO / HOLD / STOP --
plus the signals that drove it. It NEVER touches funds and is NOT in the
settlement path: it returns a verdict, the agent decides. (Verdict, not
custody -- that is the clean regulatory posture; keep it that way.)

This file is build-order step 1 of the Blackwall x402 spec:

    Verdict endpoint with reputation + price-anomaly signals, returning
    STOP/HOLD/GO. Mock the reputation data source first.

DEFERRED (NOT in this file, by design):
  * step 2 -- wire the REAL counterparty-history data source (here it is mocked).
  * step 3 -- the x402 billing handshake (Blackwall charging per forecast). The
    server seam for it is marked `TODO(step 3)` in BlackwallServer.
  * step 4 -- the MCP server wrapper.

It binds 127.0.0.1 ONLY -- like the egress proxy beside it, it is not exposed.
Stdlib only, no deps. Python 3.8+.

The pure functions at the top are the DECISION boundary -- the analogue of the
egress proxy's parse/host_allowed/decide trio -- and are unit-tested TDD-first.
Mutating a threshold or dropping a STOP condition makes a named test FAIL.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import threading
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from addresses import addresses_equal, normalize_address

# ---------------------------------------------------------------------------
# Tunables -- the decision boundary's thresholds. Changing one of these should
# change a verdict; the tests pin the current values.
# ---------------------------------------------------------------------------
GO_REPUTATION_MIN = 0.70          # below this trust score, never an automatic GO
THIN_HISTORY_SETTLEMENTS = 20     # fewer prior settlements => "thin" => cannot GO
HOLD_AMOUNT_THRESHOLD = Decimal("10.00")  # amounts above this escalate (HOLD)
HOLD_ANOMALY = 0.30               # price-anomaly score at/above this cannot GO
STOP_ANOMALY_RATIO = 8.0          # charged >= 8x its own median => STOP (wildly off)
MAX_BODY_BYTES = 64 * 1024        # request-body cap (oversize guard)

# Receipts are signed so the agent can keep a tamper-evident audit trail. This
# is a DEV key; a real deployment supplies BLACKWALL_RECEIPT_KEY from a secret.
_DEV_RECEIPT_KEY = b"blackwall-dev-receipt-key-not-for-production"


# ===========================================================================
# DECISION-BOUNDARY PURE FUNCTIONS (unit-tested first)
# ===========================================================================
def parse_amount(raw):
    """
    Parse a payment amount (the `amount` field of the verdict request).

    Returns a positive Decimal on success, or None to REJECT.

    Rejects (None): missing, empty, non-numeric, NaN/Inf, zero, negative.
    Money is parsed via Decimal (never float) so `"0.09"` stays exact.
    """
    if raw is None:
        return None
    # Accept str / int / Decimal; reject float (binary-rounding) and junk.
    if isinstance(raw, float):
        return None
    try:
        amount = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not amount.is_finite():
        return None
    if amount <= 0:
        return None
    return amount


def price_anomaly_ratio(amount, price_history):
    """
    Ratio of `amount` to the counterparty's own median historical price for
    this resource class.

    Returns a float ratio (amount / median), or None when there is no usable
    history -- "is this endpoint charging me 8x what it charged everyone else"
    is a different question from "is the signature valid", and with no history
    it simply cannot be answered (so the caller treats anomaly as UNKNOWN, not
    as zero-and-fine).
    """
    if not price_history:
        return None
    prices = sorted(Decimal(str(p)) for p in price_history)
    n = len(prices)
    mid = n // 2
    if n % 2:
        median = prices[mid]
    else:
        median = (prices[mid - 1] + prices[mid]) / 2
    if median <= 0:
        return None
    return float(Decimal(str(amount)) / median)


def anomaly_score(ratio):
    """
    Map a price ratio to an anomaly score in 0..1 (0 = at/under the median and
    fine; 1 = at/over STOP_ANOMALY_RATIO, i.e. wildly overpriced).

    Cheaper-than-median (ratio <= 1) is never anomalous. Only OVERcharge counts
    -- the threat model is an endpoint price-gouging this agent, not giving it a
    discount. ratio of None (no history) -> 0.0; the UNKNOWN-ness is carried as
    a separate "thin history" signal, not smuggled into the anomaly number.
    """
    if ratio is None:
        return 0.0
    if ratio <= 1.0:
        return 0.0
    span = STOP_ANOMALY_RATIO - 1.0
    return min(1.0, (ratio - 1.0) / span)


def reputation_score(record):
    """
    Bayesian trust score in 0..1 from a counterparty's settlement history.

    Posterior mean of a Beta(good+1, bad+1) (Laplace prior), where
    good = settlements served without dispute and bad = disputed/underdelivered.
    A brand-new wallet with no history scores 0.5 (the prior), NOT 1.0 -- "no
    evidence" is not "trustworthy". Dispute rate pulls it down; volume pulls a
    clean record up toward 1 with diminishing returns. Age/velocity are surfaced
    as reasons and feed the separate thin-history gate, not folded in here.
    """
    settlements = record.get("settlement_count", 0) or 0
    dispute_rate = record.get("dispute_rate", 0.0) or 0.0
    dispute_rate = min(max(float(dispute_rate), 0.0), 1.0)
    good = settlements * (1.0 - dispute_rate)
    bad = settlements * dispute_rate
    return (good + 1.0) / (good + bad + 2.0)


def decide_payment(amount, record, price_history,
                   counterparty=None, expected_recipient=None):
    """
    The core verdict. Returns a dict:
        {verdict, score, reasons[], signals{...}}

    GO   -- reputable, in-budget, price within norms. The agent signs and pays.
    HOLD -- resolvable financial/high-impact: amount over threshold, thin
            counterparty, or anomalous price. Hand to the spending-cap /
            slow-escalation layer.
    STOP -- severe unambiguous harm: sanctioned or known-bad counterparty, price
            wildly off (>= STOP_ANOMALY_RATIO x its own median), or recipient
            mismatch vs the 402. Do NOT sign.

    Pure: no I/O. `record` is whatever the (mocked, for now) reputation source
    returned for `counterparty`.
    """
    settlements = record.get("settlement_count", 0) or 0
    dispute_rate = float(record.get("dispute_rate", 0.0) or 0.0)
    rep = reputation_score(record)
    ratio = price_anomaly_ratio(amount, price_history)
    a_score = anomaly_score(ratio)
    thin = settlements < THIN_HISTORY_SETTLEMENTS

    reasons = []

    # ---- STOP conditions (any one trips it) ----
    stop = False
    hard_stop = False  # sanctioned / known-bad / mismatch => trust score floored to 0
    if expected_recipient is not None and counterparty is not None \
            and not addresses_equal(counterparty, expected_recipient):
        # Case-INSENSITIVE: an EIP-55 checksummed recipient and its lowercase
        # form are the same address, not a mismatch.
        stop = hard_stop = True
        reasons.append(
            "recipient mismatch: paying %s but the 402 named %s"
            % (counterparty, expected_recipient)
        )
    if record.get("sanctioned"):
        stop = hard_stop = True
        reasons.append("counterparty is on a sanctions list")
    if record.get("known_bad"):
        stop = hard_stop = True
        reasons.append("counterparty is a known-bad address")
    if ratio is not None and ratio >= STOP_ANOMALY_RATIO:
        stop = True
        reasons.append(
            "quoted amount is %.1fx the counterparty's own median -- price wildly off"
            % ratio
        )

    # ---- reputation / price / amount signals (always reported) ----
    reasons.append(
        "counterparty has %d prior settlements, %.1f%% dispute rate"
        % (settlements, dispute_rate * 100.0)
    )
    if ratio is None:
        reasons.append(
            "no price history for this counterparty/resource -- price anomaly unknown"
        )
    elif ratio <= 1.1:
        reasons.append(
            "quoted amount within %.2fx of the counterparty's median for this resource class"
            % ratio
        )
    else:
        reasons.append(
            "quoted amount is %.2fx the counterparty's median for this resource class"
            % ratio
        )

    amount_dec = Decimal(str(amount))
    over_budget = amount_dec > HOLD_AMOUNT_THRESHOLD

    # ---- verdict ----
    if stop:
        verdict = "STOP"
    else:
        go = (rep >= GO_REPUTATION_MIN
              and not thin
              and a_score < HOLD_ANOMALY
              and not over_budget)
        if go:
            verdict = "GO"
        else:
            verdict = "HOLD"
            if thin:
                reasons.append(
                    "counterparty thin on history (%d < %d settlements) -- escalating"
                    % (settlements, THIN_HISTORY_SETTLEMENTS)
                )
            if rep < GO_REPUTATION_MIN:
                reasons.append(
                    "trust score %.2f below the auto-approve floor %.2f"
                    % (rep, GO_REPUTATION_MIN)
                )
            if a_score >= HOLD_ANOMALY:
                reasons.append("price anomaly above the auto-approve ceiling")
            if over_budget:
                reasons.append(
                    "amount %s exceeds the auto-approve threshold %s -- "
                    "hand to spending-cap layer"
                    % (amount_dec, HOLD_AMOUNT_THRESHOLD)
                )

    # Bayesian trust score for the response. Hard STOPs (sanction/known-bad/
    # mismatch) floor it to 0 -- there is no "trust" to report. Otherwise it is
    # reputation discounted by the price anomaly.
    if hard_stop:
        score = 0.0
    else:
        score = rep * (1.0 - a_score)

    signals = {
        "counterparty_reputation": round(rep, 3),
        "price_anomaly": round(a_score, 3),
        # x402 settlement is on-chain and final.
        "reversibility": "irreversible",
        "blast_radius": "bounded" if not over_budget else "unbounded",
    }

    return {
        "verdict": verdict,
        "score": round(score, 3),
        "reasons": reasons,
        "signals": signals,
    }


def sign_receipt(verdict_obj, key=None):
    """
    Deterministic, signed receipt id for the agent's audit trail.

    `bw_` + first 24 hex chars of HMAC-SHA256(key, canonical-json(verdict)).
    Same verdict + same key -> same id (verifiable), so the agent can prove
    later exactly what Blackwall returned.
    """
    if key is None:
        key = os.environ.get("BLACKWALL_RECEIPT_KEY", "").encode("utf-8") \
            or _DEV_RECEIPT_KEY
    canonical = json.dumps(verdict_obj, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    digest = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    return "bw_" + digest[:24]


# ===========================================================================
# Mock reputation data source (step 2 replaces this with a real source)
# ===========================================================================
class MockReputationSource:
    """
    Stand-in for the real counterparty-history source (HANDOFF open item: where
    from, and is it fast enough for a hot-path call?). Keyed by wallet address.

    A record carries: settlement_count, dispute_rate (0..1), age_days,
    velocity_per_day, sanctioned, known_bad, and price_history (the
    counterparty's own prior quotes for this resource class).

    An UNKNOWN counterparty gets a thin, no-history record -- which naturally
    routes to HOLD, never to a silent GO.
    """

    def __init__(self, seed=None):
        # A few illustrative counterparties so the three verdicts are reachable
        # end-to-end without a live data source.
        self._db = seed if seed is not None else {
            # Long, clean history + stable pricing -> GO-eligible.
            "0xKNOWNGOOD000000000000000000000000000001": {
                "settlement_count": 1240,
                "dispute_rate": 0.002,
                "age_days": 540,
                "velocity_per_day": 12.0,
                "sanctioned": False,
                "known_bad": False,
                "price_history": ["0.08", "0.09", "0.085", "0.09", "0.10",
                                  "0.088", "0.092"],
            },
            # Real but thin -> HOLD (graduates out over time; that engine is the
            # deferred step 6 retention story, not the MVP).
            "0xNEWBIE0000000000000000000000000000000002": {
                "settlement_count": 3,
                "dispute_rate": 0.0,
                "age_days": 6,
                "velocity_per_day": 0.5,
                "sanctioned": False,
                "known_bad": False,
                "price_history": ["0.09", "0.09"],
            },
            # OFAC-style hit -> STOP regardless of everything else.
            "0xSANCTIONED00000000000000000000000000003": {
                "settlement_count": 80,
                "dispute_rate": 0.01,
                "age_days": 200,
                "velocity_per_day": 4.0,
                "sanctioned": True,
                "known_bad": False,
                "price_history": ["0.09"],
            },
        }

    def lookup(self, counterparty):
        """Return the record for `counterparty`, or a thin unknown-wallet record."""
        rec = self._db.get(counterparty)
        if rec is not None:
            return dict(rec)
        return {
            "settlement_count": 0,
            "dispute_rate": 0.0,
            "age_days": 0,
            "velocity_per_day": 0.0,
            "sanctioned": False,
            "known_bad": False,
            "price_history": [],
        }


# ===========================================================================
# Request validation + forecast assembly
# ===========================================================================
REQUIRED_FIELDS = ("counterparty", "amount", "asset", "chain")


def validate_request(payload):
    """
    Validate a /v1/forecast-payment request body (already JSON-decoded).

    Returns (clean_dict, None) on success or (None, error_message) to reject
    with 400. Required: counterparty, amount (positive), asset, chain. `context`
    is optional and, when present, may carry `quoted_price_history` and
    `expected_recipient`.
    """
    if not isinstance(payload, dict):
        return None, "request body must be a JSON object"
    for field in REQUIRED_FIELDS:
        if field not in payload or payload[field] in (None, ""):
            return None, "missing required field: %s" % field
        if field != "amount" and not isinstance(payload[field], str):
            return None, "field %s must be a string" % field

    amount = parse_amount(payload.get("amount"))
    if amount is None:
        return None, "amount must be a positive decimal string (e.g. \"0.09\")"

    context = payload.get("context") or {}
    if not isinstance(context, dict):
        return None, "context must be an object"
    price_history = context.get("quoted_price_history") or []
    if not isinstance(price_history, list):
        return None, "context.quoted_price_history must be a list"
    expected_recipient = context.get("expected_recipient")
    if expected_recipient is not None and not isinstance(expected_recipient, str):
        return None, "context.expected_recipient must be a string"

    # payer = the agent's on-chain wallet that will sign/send the x402 payment.
    # Optional, but when supplied it must be a real EVM address: it binds the
    # later settlement confirmation to THIS agent (see settlement_watch), so a
    # malformed payer can never be matched on-chain. Normalized to lowercase.
    payer = payload.get("payer")
    if payer is not None:
        npayer = normalize_address(payer)
        if npayer is None:
            return None, "payer must be a valid EVM address (0x + 40 hex chars)"
        payer = npayer

    return {
        "agent_id": payload.get("agent_id"),
        "counterparty": payload["counterparty"],
        "resource": payload.get("resource"),
        "amount": amount,
        "asset": payload["asset"],
        "chain": payload["chain"],
        "price_history": price_history,
        "expected_recipient": expected_recipient,
        "payer": payer,
    }, None


def forecast(payload, reputation_source, ledger=None):
    """
    End-to-end: validate -> look up counterparty -> decide -> sign receipt.

    Returns (response_dict, None) or (None, error_message). The reputation
    lookup is the ONLY external dependency, and it is the (mocked) part that
    step 2 will replace with a real, latency-bounded source.

    If `ledger` is given, the verdict is recorded (the write half of the moat
    flywheel -- see ledger.py). The agent later closes the loop by reporting the
    outcome against the returned receipt_id.
    """
    clean, err = validate_request(payload)
    if err is not None:
        return None, err

    record = reputation_source.lookup(clean["counterparty"])
    # Prefer the counterparty's own recorded history; fall back to whatever the
    # agent supplied in context (its prior quotes for this resource).
    price_history = record.get("price_history") or clean["price_history"]

    verdict = decide_payment(
        amount=clean["amount"],
        record=record,
        price_history=price_history,
        counterparty=clean["counterparty"],
        expected_recipient=clean["expected_recipient"],
    )
    # The receipt is the ledger's join key, so it must be UNIQUE PER PAYMENT --
    # not just a hash of the verdict content (two counterparties with identical
    # stats produce identical verdicts and would otherwise collide). Sign over
    # the request identity plus a fresh nonce.
    receipt_payload = dict(verdict)
    receipt_payload.update({
        "counterparty": clean["counterparty"],
        "amount": str(clean["amount"]),
        "asset": clean["asset"],
        "chain": clean["chain"],
        "agent_id": clean.get("agent_id"),
        "nonce": os.urandom(12).hex(),
    })
    verdict["receipt_id"] = sign_receipt(receipt_payload)

    if ledger is not None:
        ledger.record_verdict(
            receipt_id=verdict["receipt_id"],
            counterparty=clean["counterparty"],
            amount=clean["amount"],
            verdict=verdict["verdict"],
            score=verdict["score"],
            agent_id=clean.get("agent_id"),
            resource=clean.get("resource"),
            asset=clean.get("asset"),
            chain=clean.get("chain"),
            payer=clean.get("payer"),
        )
    return verdict, None


# ===========================================================================
# HTTP server (POST /v1/forecast-payment) -- localhost only
# ===========================================================================
class _Handler(BaseHTTPRequestHandler):
    server_version = "Blackwall/0.1"

    # Injected by BlackwallServer.
    reputation_source = None
    ledger = None
    billing = None  # x402.BillingGate, or None to disable billing

    def _send_json(self, code, obj, extra_headers=None):
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "not found"})

    def _read_json_body(self):
        """Return (payload, None) or (None, error_string). Bounded + guarded."""
        raw_len = self.headers.get("Content-Length")
        try:
            length = int(raw_len) if raw_len is not None else 0
        except (TypeError, ValueError):
            return None, "invalid Content-Length header"
        if length <= 0:
            return None, "empty request body"
        if length > MAX_BODY_BYTES:
            return None, "request body too large"
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8")), None
        except (ValueError, UnicodeDecodeError):
            return None, "body is not valid JSON"

    def do_POST(self):
        if self.path == "/v1/forecast-payment":
            self._do_forecast()
        elif self.path == "/v1/report-outcome":
            self._do_report_outcome()
        elif self.path == "/v1/session":
            self._do_session()
        else:
            self._send_json(404, {"error": "not found"})

    def _do_forecast(self):
        payload, err = self._read_json_body()
        if err is not None:
            self._send_json(400, {"error": err})
            return

        # x402 billing (opt-in): on the first/unpaid call answer 402; the agent
        # retries with X-PAYMENT (or X-PAYMENT-SESSION). The verdict logic is
        # unchanged -- billing is a gate in front of it.
        remaining = None
        if self.billing is not None:
            resource = payload.get("resource") or self.path
            result = self.billing.check(
                resource,
                x_payment=self.headers.get("X-PAYMENT"),
                x_session=self.headers.get("X-PAYMENT-SESSION"))
            if not result.paid:
                self._send_json(result.status or 402, result.body)
                return
            remaining = result.session_remaining

        response, err = forecast(payload, self.reputation_source, self.ledger)
        if err is not None:
            self._send_json(400, {"error": err})
            return
        extra = {"X-PAYMENT-SESSION-REMAINING": str(remaining)} \
            if remaining is not None else None
        self._send_json(200, response, extra_headers=extra)

    def _do_session(self):
        # Fund-once, many-checks: a payment covering the session price returns a
        # reusable session token (x402 V2 session). Cuts per-call signing/latency.
        if self.billing is None:
            self._send_json(404, {"error": "billing not enabled"})
            return
        payload, _ = self._read_json_body()  # body optional here
        resource = (payload or {}).get("resource") if isinstance(payload, dict) else None
        result = self.billing.open_session(resource or self.path,
                                           self.headers.get("X-PAYMENT"))
        self._send_json(result.status or 402, result.body)

    def _do_report_outcome(self):
        # Close the moat flywheel: the agent reports what a prior verdict's
        # payment actually did, keyed by the receipt_id Blackwall signed.
        if self.ledger is None:
            self._send_json(503, {"error": "no ledger configured"})
            return
        payload, err = self._read_json_body()
        if err is not None:
            self._send_json(400, {"error": err})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "body must be a JSON object"})
            return
        receipt_id = payload.get("receipt_id")
        outcome = payload.get("outcome")
        if not receipt_id or not isinstance(receipt_id, str):
            self._send_json(400, {"error": "missing receipt_id"})
            return
        try:
            self.ledger.record_outcome(
                receipt_id=receipt_id,
                outcome=outcome,
                observed_amount=payload.get("observed_amount"),
                settlement_tx=payload.get("settlement_tx"),
            )
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return
        self._send_json(202, {"status": "recorded", "receipt_id": receipt_id})

    def log_message(self, fmt, *args):
        # One concise line to stdout, matching the proxy's house style.
        sys.stdout.write("blackwall: %s - %s\n" % (self.address_string(), fmt % args))
        sys.stdout.flush()


class BlackwallServer:
    """Localhost-only verdict server. Binds 127.0.0.1; not exposed."""

    def __init__(self, host="127.0.0.1", port=8402, reputation_source=None,
                 ledger=None, billing=None):
        self.host = host
        self.port = port
        self.reputation_source = reputation_source or MockReputationSource()
        self.ledger = ledger
        self.billing = billing
        self._httpd = None

    def serve_forever(self):
        handler = type("_BoundHandler", (_Handler,),
                       {"reputation_source": self.reputation_source,
                        "ledger": self.ledger,
                        "billing": self.billing})
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = self._httpd.server_address[1]
        sys.stdout.write(
            "blackwall verdict service on %s:%d  "
            "POST /v1/forecast-payment  POST /v1/report-outcome%s  "
            "(reputation source: MOCK, ledger: %s, billing: %s)\n"
            % (self.host, self.port,
               "  POST /v1/session" if self.billing else "",
               "on" if self.ledger else "off",
               "on" if self.billing else "off")
        )
        sys.stdout.flush()
        try:
            self._httpd.serve_forever()
        finally:
            self.shutdown()

    def shutdown(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None


# ===========================================================================
# CLI
# ===========================================================================
def main(argv=None):
    p = argparse.ArgumentParser(
        description="Blackwall pre-signature payment-verdict service (x402, step 1)."
    )
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("BLACKWALL_PORT", "8402")),
                   help="listen port (default 8402; bind is always 127.0.0.1)")
    p.add_argument("--ledger",
                   default=os.environ.get("BLACKWALL_LEDGER"),
                   help="path to the verdict->outcome ledger (JSONL); enables "
                        "POST /v1/report-outcome and self-learned reputation")
    p.add_argument("--pay-to", default=os.environ.get("BLACKWALL_PAY_TO"),
                   help="Blackwall's EVM wallet; enabling it turns ON x402 "
                        "billing (402 challenge + POST /v1/session)")
    p.add_argument("--price", default=os.environ.get("BLACKWALL_PRICE", "0.001"),
                   help="per-forecast price in USDC (default 0.001)")
    p.add_argument("--facilitator", default=os.environ.get("BLACKWALL_FACILITATOR"),
                   help="x402 facilitator base URL (verify/settle); default is "
                        "the built-in mock facilitator")
    args = p.parse_args(argv)

    led = None
    if args.ledger:
        from ledger import EventLedger
        led = EventLedger(args.ledger)

    billing = None
    if args.pay_to:
        from x402 import BillingConfig, BillingGate, HttpFacilitator
        facilitator = HttpFacilitator(args.facilitator) if args.facilitator else None
        billing = BillingGate(BillingConfig(price=args.price, pay_to=args.pay_to),
                              facilitator=facilitator)

    server = BlackwallServer(host="127.0.0.1", port=args.port, ledger=led,
                             billing=billing)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("\nblackwall: shutting down (Ctrl-C)\n")
        server.shutdown()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
