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
import base64
import hashlib
import hmac
import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from addresses import addresses_equal, is_evm_address, normalize_address

_AMOUNT_RE = re.compile(r"\A\d+(\.\d+)?\Z")  # plain decimal money string only


def _b64_header(obj):
    """Base64-encode a JSON dict for an x402 v2 HTTP transport header
    (PAYMENT-REQUIRED / PAYMENT-RESPONSE). Header-safe (base64 is ASCII, no
    newlines from compact separators)."""
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")

# ---------------------------------------------------------------------------
# Tunables -- the decision boundary's thresholds. Changing one of these should
# change a verdict; the tests pin the current values.
# ---------------------------------------------------------------------------
GO_REPUTATION_MIN = 0.70          # below this trust score, never an automatic GO
THIN_HISTORY_SETTLEMENTS = 20     # fewer prior settlements => "thin" => cannot GO
MIN_DISTINCT_PAYERS = 3           # confirmed settlements must span >= N payers (Sybil guard)
MIN_CLASS_OBSERVATIONS = 3        # same-resource observations needed to trust a per-class median
MIN_PEER_COUNTERPARTIES = 3       # distinct counterparties needed to define a peer-group market rate
PEER_HOLD_RATIO = 3.0             # priced >= Nx the peer market for its class => escalate (HOLD)
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
    if isinstance(raw, bool) or isinstance(raw, float):
        return None
    s = str(raw).strip()
    # Plain decimal only -- reject Decimal's permissive forms ("1_000", "1e3",
    # "+5", "Inf", "NaN") that would silently mean something other than the
    # literal digits for a money field.
    if not _AMOUNT_RE.match(s):
        return None
    try:
        amount = Decimal(s)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    return amount


def _median_of(values):
    """Median of a non-empty iterable of Decimals."""
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def robust_price_median(observations, min_payers=MIN_DISTINCT_PAYERS):
    """Wash-trade-resistant median price from payer-attributed observations.

    The flat median over all amounts is gameable: a counterparty can pay ITSELF
    many times to anchor its own 'normal' price, then overcharge a victim without
    tripping the anomaly check. This collapses each DISTINCT payer to ONE
    representative price (that payer's own median), then takes the median across
    payers -- so transaction COUNT from one actor counts once. Moving the result
    requires controlling a MAJORITY of distinct, chain-confirmed, funded payers,
    not just spamming settlements.

    `observations` is an iterable of {"payer", "amount"} dicts or (payer, amount)
    pairs. Entries with no payer or a non-positive/garbage amount are dropped.
    Returns (median_Decimal, n_distinct_payers), or (None, n) when fewer than
    `min_payers` distinct payers contribute -- too thin to trust over the flat
    median.

    Limitation (honest): this raises the cost of a wash-trade -- each fake payer
    must be a distinct, funded, chain-confirmed settler -- but does not eliminate
    it. At exactly `min_payers` an attacker controlling a majority of those few
    payers can still move the median; the defense strengthens as the distinct
    payer count grows. A peer-group cross-check (vs comparable services) is the
    complementary half and is NOT yet built."""
    by_payer = {}
    for obs in observations or []:
        if isinstance(obs, dict):
            payer, amt = obs.get("payer"), obs.get("amount")
        elif isinstance(obs, (list, tuple)) and len(obs) == 2:
            payer, amt = obs
        else:
            continue
        if not payer:
            continue
        try:
            d = Decimal(str(amt))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if not d.is_finite() or d <= 0:
            continue
        by_payer.setdefault(payer, []).append(d)
    n = len(by_payer)
    if n < min_payers:
        return None, n
    return _median_of([_median_of(v) for v in by_payer.values()]), n


def select_class_observations(observations, resource,
                              min_class_obs=MIN_CLASS_OBSERVATIONS):
    """Pure: choose which observations to price an amount against.

    If `resource` is given and at least `min_class_obs` observations share it,
    return just those (basis "per-class") so a payment is compared to LIKE-FOR-LIKE
    history -- a $5k invoice to a vendor whose $5k-invoice history is normal is not
    a gouge. Otherwise return ALL observations (basis "pooled") -- the conservative
    fallback: an unknown/new class still flags against the full history, so
    segmentation only ever RELAXES the anomaly when there's enough same-class
    evidence to justify it, never weakens the check for a novel payment.

    Returns (subset, basis) with basis in {"per-class", "pooled"}. Non-dict
    observations (legacy (payer, amount) tuples) have no class and never match.
    """
    obs = observations or []
    if resource is not None:
        same = [o for o in obs
                if isinstance(o, dict) and o.get("resource") == resource]
        if len(same) >= min_class_obs:
            return same, "per-class"
    return obs, "pooled"


def price_median_and_basis(price_history, observations, resource=None):
    """Pure: the median to price against, plus a label for how it was derived.

    Precedence: per-class payer-weighted -> pooled payer-weighted -> flat median
    of `price_history` -> None. `basis` is "per-class" | "payer-weighted" |
    "flat". Single source of truth for both the ratio and the reported basis."""
    if observations:
        sel, sel_basis = select_class_observations(observations, resource)
        m, _n = robust_price_median(sel)
        if m is not None:
            return m, ("per-class" if sel_basis == "per-class" else "payer-weighted")
        # a per-class subset too sparse in DISTINCT payers -> fall back to pooled
        if sel_basis == "per-class":
            m, _n = robust_price_median(observations)
            if m is not None:
                return m, "payer-weighted"
    if price_history:
        return _median_of(Decimal(str(p)) for p in price_history), "flat"
    return None, "flat"


def price_anomaly_ratio(amount, price_history, observations=None, resource=None):
    """
    Ratio of `amount` to the counterparty's own median historical price for
    this resource class.

    Returns a float ratio (amount / median), or None when there is no usable
    history -- with no history the "is this 8x what it charged everyone else"
    question simply cannot be answered (caller treats anomaly as UNKNOWN, not
    zero-and-fine).

    When payer-attributed `observations` are supplied they drive a WASH-TRADE-
    RESISTANT median (one price per payer; see robust_price_median). When a
    `resource` is given and enough observations share it, the median is computed
    PER-CLASS (like-for-like); otherwise it pools all observations, then falls
    back to the flat median of `price_history` (unchanged legacy behavior)."""
    median, _basis = price_median_and_basis(price_history, observations, resource)
    if median is None or median <= 0:
        return None
    return float(Decimal(str(amount)) / median)


def peer_group_median(counterparty_medians, min_counterparties=MIN_PEER_COUNTERPARTIES):
    """Pure: the peer-group market median for a class = the median of each
    counterparty's OWN median price. Median-of-medians so one high-volume (or
    Sybil) counterparty can't drag the market rate. Requires >= min_counterparties
    distinct peers; too few -> None (not enough market to compare against)."""
    vals = []
    for m in counterparty_medians or []:
        try:
            d = Decimal(str(m))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if d.is_finite() and d > 0:
            vals.append(d)
    if len(vals) < min_counterparties:
        return None
    return _median_of(vals)


def build_peer_class_index(class_observations,
                           min_counterparties=MIN_PEER_COUNTERPARTIES):
    """Pure: build {resource_class: peer_median} from CROSS-counterparty
    observations. Each obs is a dict {counterparty, resource_class, amount}. Per
    class: take each counterparty's median amount, then the peer-group median
    across DISTINCT counterparties (>= min_counterparties, else the class is
    omitted -- not enough peers to define a market rate). Values are strings."""
    by_class = {}  # rc -> counterparty -> [Decimal amounts]
    for o in class_observations or []:
        if not isinstance(o, dict):
            continue
        rc, cp, amt = o.get("resource_class"), o.get("counterparty"), o.get("amount")
        if not rc or not cp:
            continue
        try:
            d = Decimal(str(amt))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if not d.is_finite() or d <= 0:
            continue
        by_class.setdefault(rc, {}).setdefault(cp, []).append(d)
    index = {}
    for rc, cps in by_class.items():
        pm = peer_group_median([_median_of(v) for v in cps.values()],
                               min_counterparties)
        if pm is not None:
            index[rc] = str(pm)
    return index


def peer_anomaly_ratio(reference_price, peer_median):
    """Pure: ratio of a counterparty's reference price (its own class median, or
    the quoted amount at cold-start) to the peer-group market median for that
    class. None when there is no peer market to compare against."""
    if peer_median is None:
        return None
    try:
        pm = Decimal(str(peer_median))
        ref = Decimal(str(reference_price))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not pm.is_finite() or pm <= 0 or not ref.is_finite() or ref <= 0:
        return None
    return float(ref / pm)


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
                   counterparty=None, expected_recipient=None, hold_above=None,
                   resource=None, peer_median=None):
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
    # Payer-attributed price observations (chain-confirmed) let us use a
    # wash-trade-resistant median; without them we fall back to the flat median.
    observations = record.get("price_observations")
    ratio = price_anomaly_ratio(amount, price_history, observations=observations,
                                resource=resource)
    a_score = anomaly_score(ratio)
    # Single source of truth for the median AND the basis label (per-class ->
    # payer-weighted -> flat), so the reported signal matches what was scored.
    _median, price_basis = price_median_and_basis(price_history, observations,
                                                  resource)
    # The thin-history gate counts only CONFIRMED settlements -- on-chain
    # (reputation store) or chain-watch-confirmed (ledger). A source that does
    # not distinguish (None) vouches for all of its count (seed/on-chain). The
    # ledger sets this to its chain-confirmed subset, so unauthenticated
    # self-reported "settled" can inflate settlement_count but NOT graduate a
    # counterparty out of HOLD.
    confirmed = record.get("confirmed_settlement_count")
    if confirmed is None:
        confirmed = settlements
    thin = confirmed < THIN_HISTORY_SETTLEMENTS

    # Sybil / wash-trade guard: a counterparty that received its confirmed
    # settlements from too few DISTINCT payers is suspicious -- one actor paying
    # itself can manufacture settlement COUNT but not many distinct funded
    # payers. Gate GO on payer diversity. A source that doesn't track it (None)
    # vouches for all (seed/mock); the ledger and store provide it. (No amount
    # floor: legit x402 payments are sub-cent micropayments.)
    distinct_payers = record.get("distinct_payers")
    sybil_thin = distinct_payers is not None \
        and distinct_payers < MIN_DISTINCT_PAYERS

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
    # Amounts above this escalate to HOLD (hand to a spending-cap/human layer).
    # Configurable per deployment: agent micropayments want the low default;
    # treasury/AP raises it so in-line vendor payments can auto-release while
    # price-anomaly still catches gouges.
    threshold = (HOLD_AMOUNT_THRESHOLD if hold_above is None
                 else Decimal(str(hold_above)))
    over_budget = amount_dec > threshold

    # Peer-group cross-check: is this counterparty priced far above COMPARABLE
    # counterparties for the same resource class? Reference = its own class median
    # (structural: "this vendor is expensive"), or the quoted amount at cold-start
    # (no own history). Being above the market is EXPENSIVE, not fraud -- so it
    # blocks an automatic GO (escalates to HOLD) but never STOPs on its own.
    reference_price = _median if _median is not None else amount_dec
    peer_ratio = peer_anomaly_ratio(reference_price, peer_median)
    peer_hold = peer_ratio is not None and peer_ratio >= PEER_HOLD_RATIO

    # ---- verdict ----
    if stop:
        verdict = "STOP"
    else:
        go = (rep >= GO_REPUTATION_MIN
              and not thin
              and not sybil_thin
              and a_score < HOLD_ANOMALY
              and not over_budget
              and not peer_hold)
        if go:
            verdict = "GO"
        else:
            verdict = "HOLD"
            if sybil_thin:
                reasons.append(
                    "settlements from only %d distinct payer(s) (< %d) -- possible "
                    "wash-trading; escalating" % (distinct_payers, MIN_DISTINCT_PAYERS)
                )
            if thin:
                extra = ("" if confirmed == settlements
                         else " (%d reported, only %d chain-confirmed)"
                         % (settlements, confirmed))
                reasons.append(
                    "counterparty thin on confirmed history (%d < %d) -- escalating%s"
                    % (confirmed, THIN_HISTORY_SETTLEMENTS, extra)
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
                    % (amount_dec, threshold)
                )
            if peer_hold:
                reasons.append(
                    "priced %.1fx the peer-group market rate for this class -- "
                    "above comparable counterparties" % peer_ratio
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
        # whether the anomaly baseline is wash-trade-resistant (per-payer) or the
        # flat median -- so the caller knows how trustworthy the price norm is.
        "price_basis": price_basis,
        # how this counterparty's price compares to comparable counterparties for
        # its resource class (None = no peer market / no resource_class supplied).
        "peer_price_ratio": round(peer_ratio, 3) if peer_ratio is not None else None,
        # x402 settlement is on-chain and final.
        "reversibility": "irreversible",
        "blast_radius": "bounded" if not over_budget else "unbounded",
    }

    return {
        "verdict": verdict,
        # hard_stop = a non-negotiable block (sanctioned / known-bad / recipient
        # mismatch) vs. a judgment STOP (price gouge). Lets a consumer map STOP ->
        # a hard block vs. a human-overridable deny without sniffing reason strings.
        # (See docs/RECONCILIATION.md.) Only ever True when verdict == "STOP".
        "hard_stop": hard_stop,
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


def _receipt_key():
    return os.environ.get("BLACKWALL_RECEIPT_KEY", "").encode("utf-8") \
        or _DEV_RECEIPT_KEY


def sign_report_token(receipt_id, key=None):
    """
    Capability token authorizing an OUTCOME report for `receipt_id`.

    Returned to the caller alongside the verdict and required to POST an outcome.
    Only Blackwall (holding the key) can compute it, so a party that merely knows
    a receipt_id -- e.g. saw it in a log -- cannot forge a report. Domain-
    separated from the receipt id with a "report:" prefix.
    """
    key = key or _receipt_key()
    return hmac.new(key, ("report:" + str(receipt_id)).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def verify_report_token(receipt_id, token, key=None):
    """Constant-time check that `token` authorizes reporting on `receipt_id`."""
    if not token or not isinstance(token, str):
        return False
    return hmac.compare_digest(token, sign_report_token(receipt_id, key))


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

    # Canonicalize counterparty: when it's an EVM address, lowercase it so the
    # reputation key and the recipient-mismatch check (case-insensitive) agree,
    # and reputation doesn't split across mixed-case spellings of one address.
    counterparty = payload["counterparty"]
    if is_evm_address(counterparty):
        counterparty = counterparty.lower()

    resource_class = payload.get("resource_class")
    if resource_class is not None and not isinstance(resource_class, str):
        return None, "resource_class must be a string"

    return {
        "agent_id": payload.get("agent_id"),
        "counterparty": counterparty,
        "resource": payload.get("resource"),
        "resource_class": resource_class,
        "amount": amount,
        "asset": payload["asset"],
        "chain": payload["chain"],
        "price_history": price_history,
        "expected_recipient": expected_recipient,
        "payer": payer,
    }, None


def normalize_network(network):
    """Canonicalize a network identifier: strip + lowercase. So 'Base-Sepolia',
    ' base-sepolia\\n' (env trailing newline), 'BASE-SEPOLIA' all collapse to
    'base-sepolia' rather than silently falling through to the mainnet branch."""
    return (network or "").strip().lower()


def default_billing_asset(network, explicit, base_usdc, sepolia_usdc):
    """Pick the USDC contract to advertise in the 402 challenge.

    An explicit --asset/BLACKWALL_ASSET always wins. Otherwise the asset follows
    the NORMALIZED network: Base-Sepolia USDC for the testnet dry-run, Base
    mainnet USDC everywhere else (so an operator can't accidentally advertise
    mainnet USDC on a base-sepolia deploy just by forgetting the asset flag -- or
    by a case/whitespace slip in the network string)."""
    if explicit:
        return explicit
    return sepolia_usdc if normalize_network(network) == "base-sepolia" else base_usdc


def forecast(payload, reputation_source, ledger=None, readiness_source=None,
             hold_above=None, peer_index=None):
    """
    End-to-end: validate -> look up counterparty -> decide -> sign receipt.

    Returns (response_dict, None) or (None, error_message). The reputation
    lookup is the ONLY external dependency, and it is the (mocked) part that
    step 2 will replace with a real, latency-bounded source.

    If `ledger` is given, the verdict is recorded (the write half of the moat
    flywheel -- see ledger.py). The agent later closes the loop by reporting the
    outcome against the returned receipt_id.

    If `readiness_source` is given AND the request carries a `resource` (the
    endpoint URL the agent is paying), the verdict is enriched with a
    third-party ENDPOINT-readiness grade (see readiness.py). It is fail-open and
    conservative-only: it can escalate GO->HOLD on a poorly-configured endpoint
    but never upgrades, and a readiness outage never blocks the core verdict.
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
        hold_above=hold_above,
        resource=clean.get("resource"),
        peer_median=(peer_index.get(clean["resource_class"])
                     if peer_index and clean.get("resource_class") else None),
    )

    # Endpoint-readiness enrichment (address-based verdict + URL-based readiness).
    # FAIL OPEN here, at the consumer: a misbehaving/raising readiness source --
    # custom, or a malformed injected transport -- must never break the core
    # verdict. (The shipped sources are internally fail-open too; this enforces
    # the contract regardless of the source.)
    if readiness_source is not None and clean.get("resource"):
        from readiness import apply_readiness
        try:
            readiness = readiness_source.check(clean["resource"])
        except Exception:
            readiness = None
        verdict = apply_readiness(verdict, readiness)
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
        # The caller gets a capability token to later report this payment's
        # outcome -- only the party that received the verdict can report on it.
        verdict["report_token"] = sign_report_token(verdict["receipt_id"])
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


def sanctions_enabled(sanctions_list):
    """Whether sanctions screening is REAL and should be advertised.

    Screening only counts if the list actually loaded addresses. A missing or
    empty file must NOT enable the wrapper -- otherwise the discovery descriptor
    advertises `screening: ["sanctions-ofac"]` while screening zero addresses,
    which is an integrity lie (claims a check it doesn't perform).
    """
    return sanctions_list is not None and len(sanctions_list) > 0


def is_compliance_free(reputation_source, payload):
    """A sanctioned counterparty is a hard STOP served FREE -- never behind the
    paywall.

    Rationale: Blackwall bills itself as a SUPERSET of the free KYT baseline
    (which declines sanctioned addresses for free). Charging to deliver an OFAC
    STOP would make it WORSE than free on the compliance axis. So the sanctions
    screen runs BEFORE billing and, on a hit, the verdict is returned free.

    Only the cheap set-membership lookup runs here -- NOT the billed reputation
    path (no on-chain ingestion) -- so this doesn't open a pay-bypass for the
    expensive verdict. Price/reputation STOPs (the judgment you pay for) are
    unaffected. Fail-safe: any error -> not free (fall back to normal billing).
    """
    sanctions = getattr(reputation_source, "sanctions", None)
    if sanctions is None or not isinstance(payload, dict):
        return False
    cp = payload.get("counterparty")
    try:
        return bool(cp) and sanctions.is_sanctioned(cp)
    except Exception:
        return False


# ===========================================================================
# HTTP server (POST /v1/forecast-payment) -- localhost only
# ===========================================================================
class _Handler(BaseHTTPRequestHandler):
    server_version = "Blackwall/0.1"

    # Injected by BlackwallServer.
    reputation_source = None
    ledger = None
    billing = None  # x402.BillingGate, or None to disable billing
    readiness_source = None  # readiness.OntarioReadinessSource, or None
    hold_above = None  # amount above which the verdict escalates to HOLD (None = default)
    peer_index = None  # {resource_class: peer_median} for the peer-group check, or None
    openapi_server_url = None  # public origin for the openapi.json servers[] (or None)

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
        elif self.path in ("/.well-known/x402", "/v1/discovery"):
            self._send_json(200, self._descriptor())
        elif self.path == "/openapi.json":
            # x402scan probes the origin root for this discovery document.
            self._send_json(200, self._openapi())
        else:
            self._send_json(404, {"error": "not found"})

    def do_HEAD(self):
        # Health checkers/crawlers (UptimeRobot, etc.) often probe with HEAD;
        # the stdlib default returns 501, which reads as "service down". Mirror
        # do_GET's routing but send headers only, no body.
        code = 200 if self.path in ("/healthz", "/.well-known/x402",
                                    "/v1/discovery", "/openapi.json") else 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def _descriptor(self):
        from discovery import build_descriptor
        from sanctions import SanctionsScreeningSource
        rs = self.reputation_source
        # Advertise sanctions screening iff it is REAL *right now* -- wrapped AND
        # the list currently non-empty. Checked per request, so a background
        # refresh that populates an initially-empty list flips screening ON with
        # no restart, and an empty wrapper never claims a no-op check.
        screening = (isinstance(rs, SanctionsScreeningSource)
                     and sanctions_enabled(rs.sanctions))
        readiness = self.readiness_source is not None
        if self.billing is not None:
            cfg = self.billing.cfg
            return build_descriptor(
                pay_to=cfg.pay_to,
                # atomic units + asset CONTRACT, mirroring the authoritative 402
                # (spec 5.1.2: accepts[].amount is atomic) so a consumer reading
                # the descriptor isn't off by 10^decimals.
                price=str(cfg.price_atomic),
                asset=cfg.asset, network=cfg.network,
                mcp=True, sanctions_screening=screening,
                endpoint_readiness=readiness)
        return build_descriptor(mcp=True, sanctions_screening=screening,
                                endpoint_readiness=readiness)

    def _openapi(self):
        """The x402scan OpenAPI discovery document for this origin.

        `priced` mirrors whether billing is on so we never advertise a free
        oracle as paid (or vice-versa). The public origin URL comes from the
        BLACKWALL_ORIGIN env when set (a hosted deploy behind a proxy can't infer
        its own public URL); otherwise it is derived from the Host header, and
        omitted (relative paths) as a last resort -- x402scan resolves relative
        paths against the probed origin, so discovery still works."""
        from discovery import build_openapi
        server_url = self.openapi_server_url
        if not server_url:
            host = self.headers.get("Host")
            if host:
                # We serve plain HTTP locally, but a hosted deploy is fronted by
                # HTTPS (Render/Fly terminate TLS). Advertise https for any
                # non-localhost host so the discovered URL is reachable.
                scheme = "http" if host.split(":")[0] in (
                    "127.0.0.1", "localhost", "::1") else "https"
                server_url = "%s://%s" % (scheme, host)
        priced = self.billing is not None
        kwargs = {}
        if priced:
            # Reflect the live value-aligned band into the advertised price.
            pricing = getattr(self.billing, "pricing", None)
            if pricing is not None:
                # Advertise the real FEE band [min_fee, max_fee] -- NOT
                # free_below (that is an amount-at-risk threshold, not a fee, and
                # would produce an inverted min>max band).
                kwargs["min_fee"] = str(pricing.min_fee)
                kwargs["max_fee"] = str(pricing.max_fee)
        return build_openapi(server_url=server_url, priced=priced, **kwargs)

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

        # Compliance floor is FREE: a sanctioned counterparty is a hard STOP and
        # we never charge to deliver it (keeps Blackwall a strict SUPERSET of the
        # free KYT baseline). Cheap set lookup -- runs before billing, no
        # reputation ingestion, so it doesn't bypass the paid verdict path.
        free_stop = is_compliance_free(self.reputation_source, payload)

        # x402 billing (opt-in): on the first/unpaid call answer 402; the agent
        # retries with X-PAYMENT (or X-PAYMENT-SESSION). The verdict logic is
        # unchanged -- billing is a gate in front of it.
        remaining = None
        settlement_tx = None
        if self.billing is not None and not free_stop:
            resource = payload.get("resource") or self.path
            result = self.billing.check(
                resource,
                # v2 canonical header is PAYMENT-SIGNATURE; keep X-PAYMENT for
                # v1/CDP-client compatibility. Prefer PAYMENT-SIGNATURE if both.
                x_payment=(self.headers.get("PAYMENT-SIGNATURE")
                           or self.headers.get("X-PAYMENT")),
                x_session=self.headers.get("X-PAYMENT-SESSION"),
                # value-aligned pricing: the fee tracks the payment being forecast.
                amount_at_risk=(payload.get("amount") if isinstance(payload, dict)
                                else None))
            if not result.paid:
                extra = None
                # v2 HTTP transport: mirror the PaymentRequired body into the
                # base64 PAYMENT-REQUIRED header (canonical location). The JSON
                # body is kept too (x402scan's probe parses either; legacy
                # clients read the body). Only for real 402s carrying accepts.
                if (result.status or 402) == 402 and isinstance(result.body, dict) \
                        and "accepts" in result.body:
                    extra = {"PAYMENT-REQUIRED": _b64_header(result.body)}
                self._send_json(result.status or 402, result.body, extra_headers=extra)
                return
            remaining = result.session_remaining
            # A per-call settlement produced an on-chain tx hash; carry it back in
            # the v2 PAYMENT-RESPONSE header (base64 SettlementResponse) so the
            # paying client learns its settlement. Session/free serves carry no
            # per-call settlement, so no header is emitted for them.
            settlement_tx = result.settlement

        response, err = forecast(payload, self.reputation_source, self.ledger,
                                 readiness_source=self.readiness_source,
                                 hold_above=self.hold_above,
                                 peer_index=self.peer_index)
        if err is not None:
            self._send_json(400, {"error": err})
            return
        extra = {}
        if remaining is not None:
            extra["X-PAYMENT-SESSION-REMAINING"] = str(remaining)
        if settlement_tx:
            # v2 SettlementResponse: {success, transaction, network}. network is
            # advertised in CAIP-2 (the same value the 402 challenge carried).
            from x402 import to_caip2
            settle_resp = {"success": True, "transaction": settlement_tx,
                           "network": to_caip2(self.billing.cfg.network)}
            extra["PAYMENT-RESPONSE"] = _b64_header(settle_resp)
        self._send_json(200, response, extra_headers=extra or None)

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
        # Authenticate the reporter: only the party that received the verdict
        # holds a valid report_token for this receipt. Stops a third party who
        # merely knows a receipt_id from poisoning a counterparty's reputation.
        if not verify_report_token(receipt_id, payload.get("report_token")):
            self._send_json(403, {"error": "invalid or missing report_token"})
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
                 ledger=None, billing=None, readiness_source=None,
                 hold_above=None, peer_index=None, openapi_server_url=None):
        self.host = host
        self.port = port
        self._source_kind = "MOCK" if reputation_source is None \
            else type(reputation_source).__name__
        self.reputation_source = reputation_source or MockReputationSource()
        self.ledger = ledger
        self.billing = billing
        self.readiness_source = readiness_source
        self.hold_above = hold_above
        self.peer_index = peer_index
        self.openapi_server_url = openapi_server_url
        self._httpd = None

    def serve_forever(self):
        handler = type("_BoundHandler", (_Handler,),
                       {"reputation_source": self.reputation_source,
                        "ledger": self.ledger,
                        "billing": self.billing,
                        "readiness_source": self.readiness_source,
                        "hold_above": self.hold_above,
                        "peer_index": self.peer_index,
                        "openapi_server_url": self.openapi_server_url})
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = self._httpd.server_address[1]
        sys.stdout.write(
            "blackwall verdict service on %s:%d  "
            "POST /v1/forecast-payment  POST /v1/report-outcome%s  "
            "(reputation source: %s, ledger: %s, billing: %s)\n"
            % (self.host, self.port,
               "  POST /v1/session" if self.billing else "",
               self._source_kind,
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
    p.add_argument("--host", default=os.environ.get("BLACKWALL_HOST", "127.0.0.1"),
                   help="bind address (default 127.0.0.1, localhost-only). Use "
                        "0.0.0.0 ONLY for a hosted/public deploy -- see DEPLOY.md")
    p.add_argument("--port", type=int,
                   # Honor the standard PORT env (Render/Cloud Run/Heroku set it
                   # and route to it) as a fallback, so the service binds the port
                   # the platform expects without per-host config.
                   default=int(os.environ.get("BLACKWALL_PORT")
                               or os.environ.get("PORT") or "8402"),
                   help="listen port (default: $BLACKWALL_PORT, else $PORT, else 8402)")
    p.add_argument("--ledger",
                   default=os.environ.get("BLACKWALL_LEDGER"),
                   help="path to the verdict->outcome ledger (JSONL); enables "
                        "POST /v1/report-outcome and self-learned reputation")
    p.add_argument("--pay-to", default=os.environ.get("BLACKWALL_PAY_TO"),
                   help="Blackwall's EVM wallet; enabling it turns ON x402 "
                        "billing (402 challenge + POST /v1/session)")
    p.add_argument("--price", default=os.environ.get("BLACKWALL_PRICE", "0.001"),
                   help="flat per-forecast price in USDC (default 0.001)")
    p.add_argument("--value-pricing", action="store_true",
                   default=bool(os.environ.get("BLACKWALL_VALUE_PRICING")),
                   help="value-aligned pricing: free under BLACKWALL_FREE_BELOW "
                        "(default $1), else BLACKWALL_PRICE_BPS of the amount "
                        "(default 10bps) capped at BLACKWALL_MAX_FEE (default $0.10)")
    p.add_argument("--facilitator", default=os.environ.get("BLACKWALL_FACILITATOR"),
                   help="x402 facilitator base URL (verify/settle); default is "
                        "the built-in mock facilitator")
    p.add_argument("--network", default=os.environ.get("BLACKWALL_NETWORK", "base"),
                   help="x402 settlement network advertised in the 402 challenge "
                        "(default 'base'; use 'base-sepolia' for the testnet dry-run)")
    p.add_argument("--origin", default=os.environ.get("BLACKWALL_ORIGIN"),
                   help="public origin URL (e.g. https://agent-egress-proxy."
                        "onrender.com) advertised in GET /openapi.json for "
                        "x402scan discovery; defaults to the request Host header")
    p.add_argument("--asset", default=os.environ.get("BLACKWALL_ASSET"),
                   help="USDC contract advertised in the 402 challenge; defaults to "
                        "Base mainnet USDC, or Base-Sepolia USDC when --network is "
                        "base-sepolia")
    p.add_argument("--store", default=os.environ.get("BLACKWALL_STORE"),
                   help="SQLite reputation store path; uses REAL on-chain "
                        "reputation instead of the mock source")
    p.add_argument("--ingest", action="store_true",
                   default=bool(os.environ.get("BLACKWALL_INGEST")),
                   help="with --store, self-populate from chain on first sight "
                        "of a counterparty (first call slow, then cached)")
    p.add_argument("--sanctions", default=os.environ.get("BLACKWALL_SANCTIONS"),
                   help="path to an OFAC sanctioned-address file; STOPs sanctioned "
                        "counterparties (the free-baseline check, in one call)")
    p.add_argument("--sanctions-refresh", action="store_true",
                   default=bool(os.environ.get("BLACKWALL_SANCTIONS_REFRESH")),
                   help="on startup, best-effort refresh the sanctions list from "
                        "the published OFAC URL and merge into the baked-in "
                        "snapshot (fail-open: keeps the snapshot if the fetch "
                        "fails). Set on long-running deploys to stay current "
                        "without re-baking the image.")
    p.add_argument("--hold-above", default=os.environ.get("BLACKWALL_HOLD_ABOVE"),
                   help="amount (in the asset's units) above which a verdict "
                        "escalates to HOLD/REVIEW regardless of reputation "
                        "(default $10). Raise for treasury/AP so in-line vendor "
                        "payments can auto-release while price-anomaly still "
                        "catches gouges; price/sanctions checks are unaffected.")
    p.add_argument("--readiness", default=os.environ.get("BLACKWALL_READINESS"),
                   help="base URL of an EXTERNAL endpoint-readiness oracle (e.g. "
                        "Ontario https://ontarioprotocol.com); folds its grade into "
                        "the verdict when a request carries a `resource` URL "
                        "(fail-open, conservative-only). NOTE: this calls a third "
                        "party per request and reveals your query stream -- prefer "
                        "--readiness-local")
    p.add_argument("--readiness-local", action="store_true",
                   default=bool(os.environ.get("BLACKWALL_READINESS_LOCAL")),
                   help="SELF-OWNED endpoint-readiness: score the `resource` URL "
                        "from public signals we observe ourselves (no third-party "
                        "call, no query-stream leak). Takes precedence over "
                        "--readiness.")
    args = p.parse_args(argv)
    # Canonicalize the network so a case/whitespace slip ("Base-Sepolia", an env
    # var with a trailing newline) doesn't advertise the wrong chain/asset.
    args.network = normalize_network(args.network) or "base"

    led = None
    if args.ledger:
        from ledger import EventLedger
        led = EventLedger(args.ledger)

    reputation_source = None
    if args.store:
        from reputation_store import production_source
        reputation_source = production_source(args.store, ledger=led,
                                              ingest=args.ingest)

    # Sanctions screening on top of whatever reputation source -- this is what
    # makes Blackwall a SUPERSET of the free facilitator KYT baseline.
    if args.sanctions:
        from sanctions import (SanctionsList, SanctionsScreeningSource,
                               start_background_refresh)
        sl = SanctionsList.from_file(args.sanctions)
        # Wrap ALWAYS. The descriptor advertises screening dynamically -- only
        # while the list is non-empty (see _descriptor) -- so wrapping an empty
        # list is honest, and it lets a background refresh flip screening ON with
        # no restart.
        base = reputation_source or MockReputationSource()
        reputation_source = SanctionsScreeningSource(base, sl)
        if args.sanctions_refresh:
            # BACKGROUND refresh: the socket binds immediately and serves the
            # baked-in snapshot; the live OFAC list merges into `sl` when the fetch
            # returns. A slow / hung / drip-feeding upstream can NEVER block boot
            # or the deploy healthcheck (a sync refresh could -- socket timeouts
            # bound per-read inactivity, not total transfer time).
            start_background_refresh(sl, log=lambda m: sys.stdout.write(m + "\n"))
        if sanctions_enabled(sl):
            sys.stdout.write("blackwall: sanctions screening ON (%d addresses)\n"
                             % len(sl))
        elif args.sanctions_refresh:
            sys.stdout.write("blackwall: sanctions screening PENDING background "
                             "refresh (baked-in snapshot empty)\n")
        else:
            sys.stdout.write(
                "blackwall: WARNING sanctions file %r empty or missing and no "
                "refresh -- screening advertised OFF (no addresses)\n"
                % args.sanctions)

    billing = None
    if args.pay_to:
        from x402 import (BASE_SEPOLIA_USDC, BASE_USDC, BillingConfig,
                          BillingGate, HttpFacilitator, PricingPolicy)
        facilitator = HttpFacilitator(args.facilitator) if args.facilitator else None
        pricing = None
        if args.value_pricing:
            pricing = PricingPolicy(
                free_below=os.environ.get("BLACKWALL_FREE_BELOW", "1.00"),
                bps=os.environ.get("BLACKWALL_PRICE_BPS", "10"),
                min_fee=os.environ.get("BLACKWALL_MIN_FEE", "0.001"),
                max_fee=os.environ.get("BLACKWALL_MAX_FEE", "0.10"))
        # Fail loud (don't silently advertise mainnet USDC) if the network isn't
        # one we know an asset for and the operator didn't pin --asset.
        if args.network not in ("base", "base-sepolia") and not args.asset:
            sys.stderr.write(
                "blackwall: WARNING -- unrecognized --network %r; defaulting the "
                "billing asset to Base mainnet USDC. Pass --asset explicitly if "
                "that's wrong.\n" % args.network)
            sys.stderr.flush()
        billing = BillingGate(
            BillingConfig(price=args.price, pay_to=args.pay_to,
                          network=args.network,
                          asset=default_billing_asset(args.network, args.asset,
                                                       BASE_USDC, BASE_SEPOLIA_USDC)),
            facilitator=facilitator, pricing=pricing)

    # Public bind is a deliberate posture change (the service is localhost-only
    # by default, like the egress proxy). Warn loudly if exposing it with no
    # billing -- that publishes a free verdict oracle.
    if args.host not in ("127.0.0.1", "::1", "localhost") and billing is None:
        sys.stderr.write(
            "blackwall: WARNING -- binding %s (PUBLIC) with billing OFF: anyone "
            "can call /v1/forecast-payment for free. Set --pay-to to bill, or "
            "front it with auth. See DEPLOY.md.\n" % args.host)
        sys.stderr.flush()

    readiness_source = None
    if args.readiness_local:
        from readiness import LocalReadinessSource
        readiness_source = LocalReadinessSource()
        sys.stdout.write("blackwall: endpoint-readiness enrichment ON "
                         "(self-owned -- no third-party call)\n")
    elif args.readiness:
        from readiness import OntarioReadinessSource
        readiness_source = OntarioReadinessSource(args.readiness)
        sys.stdout.write("blackwall: endpoint-readiness enrichment ON "
                         "(external: %s -- reveals query stream)\n" % args.readiness)

    hold_above = None
    if args.hold_above:
        try:
            hold_above = str(Decimal(str(args.hold_above)))  # validate; parsed again downstream
            sys.stdout.write(
                "blackwall: HOLD escalation threshold set to %s "
                "(amounts above this REVIEW/HOLD)\n" % hold_above)
        except (InvalidOperation, ValueError):
            sys.stderr.write(
                "blackwall: WARNING invalid --hold-above %r; using default\n"
                % args.hold_above)
            sys.stderr.flush()

    # Public origin for the openapi.json servers[] block. A hosted deploy behind
    # a TLS-terminating proxy can't infer its own https URL, so it's set via
    # BLACKWALL_ORIGIN (or --origin). When unset the handler falls back to the
    # Host header, so discovery still works without config.
    origin = args.origin or os.environ.get("BLACKWALL_ORIGIN") or None
    server = BlackwallServer(host=args.host, port=args.port, ledger=led,
                             billing=billing, reputation_source=reputation_source,
                             readiness_source=readiness_source,
                             hold_above=hold_above,
                             openapi_server_url=origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("\nblackwall: shutting down (Ctrl-C)\n")
        server.shutdown()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
