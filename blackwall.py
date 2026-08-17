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
import threading
import time
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from addresses import addresses_equal, is_evm_address, normalize_address
from categories import classify_resource
from confidence import assess_confidence

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
# Stage 3 (docs/DATA_COMPLETENESS.md): sybil_ring GATES (HOLD) now that the coverage-
# convergence eval (coverage_eval.py) showed its false-flag rate on known-good payees
# has stabilized to ~0 on the shipped corpus. REVERSIBILITY LOCK: flip to False to
# demote it back to advisory-only instantly (no logic rewrite) if a live false positive
# appears. HOLD-only either way -- it can never STOP or clear a sanction.
SYBIL_RING_GATES = True
MIN_CLASS_OBSERVATIONS = 3        # same-resource observations needed to trust a per-class median
MIN_PEER_COUNTERPARTIES = 3       # distinct counterparties needed to define a peer-group market rate
PEER_HOLD_RATIO = 3.0             # priced >= Nx the peer market for its class => escalate (HOLD)
# Per-CATEGORY price baseline (category_pricing.py): a much BROADER, fuzzier cohort
# than a resource class, built from ON-CHAIN settled amounts. Advisory / HOLD-only /
# never STOP. The threshold is deliberately HIGH: an eval on real on-chain data found
# legit payees range up to ~20x their category median (premium tiers within a wide
# cohort -- finance spans cheap price-feeds to premium analytics), while genuine
# gouges are 1000x+. 50x clears every observed legit payee (0% false positives) yet
# still catches an order-of-magnitude+ gouge. Catches a COLD-START payee (own/peer
# median both blind) overcharging vs its whole category. See docs/CATEGORY.md.
CATEGORY_HOLD_RATIO = 50.0        # quoted >= 50x the category's on-chain median => HOLD
# Advertised-vs-settled divergence (price_integrity.py): a payee whose on-chain SETTLED
# median runs far above its most EXPENSIVE advertised (Bazaar) price -- lists cheap,
# collects more (bait-and-switch). Eval on the live corpus: 95% of payees settle <= 2.5x
# their max-advertised price. Gate at 10x (not 5x) for PRECISION: the 5-6x band is
# ambiguous with the temporal confound (advertised=now vs settled=historical -- a legit
# repricer looks the same), while >=10x is unambiguous (a single ~$0.001 listing settling
# ~$0.05-0.09). At 10x only ~5/292 payees gate. HOLD-only, never STOP, fail-open.
DIVERGENCE_HOLD_RATIO = 10.0      # settle >= 10x the most-expensive advertised price => HOLD
HOLD_AMOUNT_THRESHOLD = Decimal("10.00")  # amounts above this escalate (HOLD)
HOLD_ANOMALY = 0.30               # price-anomaly score at/above this cannot GO
STOP_ANOMALY_RATIO = 8.0          # charged >= 8x its own median => STOP (wildly off)
# "Going bad": recent outcomes turning toxic even while the lifetime dispute rate --
# drowned by past volume -- still looks clean. This is the compromised/rugging case a
# stateless engine and a volume-averaged rate both miss.
GOING_BAD_MIN_OUTCOMES = 4        # need >= this many RECENT outcomes to judge a trend
GOING_BAD_DISPUTE_RATE = 0.30     # recent dispute rate at/above this => escalate (HOLD)
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
                   resource=None, peer_median=None, payload_mismatch_reasons=None,
                   verified_floor=None, verified_grade=None,
                   payer_graph_signal=None, temporal_signal=None,
                   category=None, category_median=None, divergence_ratio=None,
                   enrichment=None, secret_findings=None):
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

    # Cross-counterparty guard (payer_graph.py / payer_reputation.py): the naive
    # Sybil gate above counts distinct payers but not WHO they are.
    #   * captive_sybil -- clears the distinct gate, yet every payer pays ONLY this
    #     payee (a wash farm). This BLOCKS the automatic GO (-> HOLD): it needs
    #     `established_payers == 0`, so it stays rare and doesn't fire on major payees.
    #   * sybil_ring    -- clears the distinct gate, yet NOT ONE payer is reputable
    #     (pays a trusted anchor) -- a closed, unvouched cluster. GATES (HOLD) now:
    #     the coverage-convergence eval (coverage_eval.py, docs/DATA_COMPLETENESS.md)
    #     showed its false-flag rate on known-good payees has stabilized to ~0 on the
    #     shipped corpus, so it no longer HOLDs real merchants. Behind SYBIL_RING_GATES
    #     (flip to False to demote to advisory instantly).
    # CONSERVATIVE: HOLD only, never a STOP; fail-open when no graph signal.
    _pgs = payer_graph_signal or {}
    captive_sybil = bool(_pgs.get("captive_sybil"))
    sybil_ring = bool(_pgs.get("sybil_ring"))
    ring_gate = sybil_ring and SYBIL_RING_GATES
    graph_sybil = captive_sybil or ring_gate  # both cross-counterparty Sybil patterns HOLD

    # Temporal axis (settlement_velocity.py). `stale` GATES: last_seen is robust
    # (the backfill always fetches the most recent settlements), so "no settlement in
    # STALE_DAYS" reliably flags a possibly-dead/abandoned endpoint -> HOLD (confirm
    # it still operates). `burst_sybil` is DIAGNOSTIC only -- backfill windowing makes
    # it flag high-volume payees -- so it is surfaced, never acted on. Fail-open.
    _tmp = temporal_signal or {}
    stale = bool(_tmp.get("stale"))

    # "Going bad" gate: a counterparty whose RECENT outcomes are disputing, even if
    # its volume-averaged lifetime dispute_rate still looks clean (1000 good then 4
    # of the last 10 disputed -> lifetime 0.4%, recent 40%). reputation_score is
    # dominated by lifetime volume and misses this; the recency window catches it.
    # HOLD (resolvable) -- escalate; never a STOP on the payer's self-reported outcome.
    recent_dr = record.get("recent_dispute_rate")
    recent_n = record.get("recent_outcomes") or 0
    going_bad = (recent_dr is not None
                 and recent_n >= GOING_BAD_MIN_OUTCOMES
                 and recent_dr >= GOING_BAD_DISPUTE_RATE)

    reasons = []

    # Seller-audit tier: a VALID Blackwall "verified merchant" attestation (issued
    # ONLY to a merchant that passed the audit -- seller_audit.py) grants a bounded,
    # EARNED trust floor and clears the thin-COUNT gate (the audit substitutes for
    # organic volume). It deliberately does NOT touch the Sybil / distinct-payer
    # gate (an audit can't certify distinct real payers) and NEVER overrides a STOP
    # (sanctions/known-bad/mismatch/gouge) or the anomaly/budget gates -- all of
    # which are evaluated from the LIVE record/price below, so a verified merchant
    # that later misbehaves is still caught (and its badge can be revoked).
    if verified_floor is not None:
        rep = max(rep, float(verified_floor))
        thin = False
        reasons.append(
            "counterparty is a Blackwall-verified merchant (grade %s) -- audit-earned "
            "trust floor %.2f applied; thin-history gate waived (Sybil/anomaly/budget/"
            "sanctions still enforced)" % (verified_grade, float(verified_floor)))

    # ---- STOP conditions (any one trips it) ----
    stop = False
    hard_stop = False  # sanctioned / known-bad / mismatch => trust score floored to 0
    # Payload simulation (Phase 1): the agent's ACTUAL signed x402 payment did not
    # match the payment it asked us to score (pays the wrong party / wrong amount /
    # wrong asset/chain). Non-negotiable -- you'd be signing something other than
    # what was judged. Leads the reasons because it is the sharpest attack. (See
    # payload_sim.py; reasons are pre-computed there so this stays pure.)
    for _m in (payload_mismatch_reasons or []):
        stop = hard_stop = True
        reasons.append(_m)
    # Leaked-secret guard (secret_scan.py): a private key / seed phrase / API credential
    # in a FREE-TEXT payment field would be published on-chain (public, irreversible) the
    # moment this is signed. HIGH severity -> hard STOP, non-negotiable. The reason names
    # only the TYPE and FIELD -- NEVER the secret value (secret_scan already redacts).
    _secret_high = [s for s in (secret_findings or []) if s.get("severity") == "high"]
    for _s in _secret_high:
        stop = hard_stop = True
        reasons.append(
            "payment payload contains a leaked secret (%s in field '%s') -- do NOT "
            "sign; it would be published on-chain and cannot be undone"
            % (_s.get("type", "credential"), _s.get("field", "payload"))
        )
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

    # Per-CATEGORY cross-check (category_pricing.py): is the QUOTED amount an
    # order-of-magnitude outlier vs what payees in this SERVICE CATEGORY actually
    # collect on-chain? This is the cold-start gouge signal -- a payee with no own
    # history (peer/own-median both blind) can still be caught paying 10x the
    # category norm. Reference = the quoted amount (what the agent is about to sign),
    # not a median. Advisory: HOLD only, never STOP; fail-open (no baseline -> None).
    category_ratio = peer_anomaly_ratio(amount_dec, category_median)
    category_hold = category_ratio is not None and category_ratio >= CATEGORY_HOLD_RATIO

    # Advertised-vs-settled price integrity (price_integrity.py): a PAYEE-level trust
    # signal precomputed as settled_median / max_advertised. A payee that lists cheap
    # but historically collects far more is a bait-and-switch risk. HOLD-only, fail-open.
    try:
        _div = float(divergence_ratio) if divergence_ratio is not None else None
    except (TypeError, ValueError):
        _div = None
    divergence_hold = _div is not None and _div >= DIVERGENCE_HOLD_RATIO

    # Free on-chain ENRICHMENT (blockscout.py): behavioral/crowd context. HARD BOUNDARY
    # -- it can ONLY push a would-be GO to HOLD (REVIEW); it must NEVER cause or clear a
    # STOP and never touches hard_stop or the sanctions decision (that stays with the
    # real compliance feeds). Structurally enforced: `enrichment_review` is added to the
    # GO conditions only, so it can turn GO->HOLD but cannot reach the STOP path.
    enrichment_review = bool((enrichment or {}).get("review"))

    # MEDIUM-severity secret/PII findings (SSN, bare hex, mnemonic-shape) -> HOLD (review):
    # lower stakes or higher false-positive risk than a HIGH credential, so escalate to a
    # human rather than block. HIGH findings already STOPped above.
    _secret_medium = sorted({s.get("type") for s in (secret_findings or [])
                             if s.get("severity") == "medium"})
    secret_hold = bool(_secret_medium)

    # ---- HOLD gates: single source of truth --------------------------------------
    # Each entry is (fired, reason-thunk). `go` is simply "no gate fired"; a fired gate's
    # reason is appended in order. The reason is a THUNK because several format signal
    # values that are only valid when the gate fired (e.g. peer_ratio is None unless
    # peer_hold). This unifies the old parallel go-conditions and reason blocks: adding a
    # HOLD signal is ONE entry here, so the two can never drift (a gate with no reason ->
    # an unexplained HOLD). STOP conditions are handled above; these only ever HOLD.
    def _thin_reason():
        extra = ("" if confirmed == settlements
                 else " (%d reported, only %d chain-confirmed)" % (settlements, confirmed))
        return ("counterparty thin on confirmed history (%d < %d) -- escalating%s"
                % (confirmed, THIN_HISTORY_SETTLEMENTS, extra))

    hold_gates = [
        (sybil_thin, lambda: "settlements from only %d distinct payer(s) (< %d) -- "
            "possible wash-trading; escalating" % (distinct_payers, MIN_DISTINCT_PAYERS)),
        (captive_sybil, lambda: "all %d payer(s) are captive (none pay any other known "
            "payee) -- cross-counterparty Sybil pattern; escalating"
            % (_pgs.get("distinct_payers") or 0)),
        (thin, _thin_reason),
        (rep < GO_REPUTATION_MIN, lambda: "trust score %.2f below the auto-approve floor "
            "%.2f" % (rep, GO_REPUTATION_MIN)),
        (a_score >= HOLD_ANOMALY, lambda: "price anomaly above the auto-approve ceiling"),
        (over_budget, lambda: "amount %s exceeds the auto-approve threshold %s -- hand to "
            "spending-cap layer" % (amount_dec, threshold)),
        (peer_hold, lambda: "priced %.1fx the peer-group market rate for this class -- "
            "above comparable counterparties" % peer_ratio),
        (category_hold, lambda: "quoted %.1fx the on-chain median for the '%s' category "
            "(%s) -- far above what comparable services collect; escalating"
            % (category_ratio, category or "?", category_median)),
        (divergence_hold, lambda: "counterparty settles %.1fx its most-expensive "
            "ADVERTISED price -- its public listing understates the real cost (possible "
            "bait-and-switch); escalating" % _div),
        # enrichment carries its OWN (pre-worded) reason list -> the thunk returns a list.
        (enrichment_review, lambda: list((enrichment or {}).get("reasons", []))),
        (stale, lambda: "no settlement in %s day(s) -- counterparty may be dormant or the "
            "endpoint abandoned; confirm it still operates" % (_tmp.get("recency_days"))),
        (going_bad, lambda: "recent dispute rate %.0f%% over the last %d outcome(s) -- "
            "counterparty trending bad despite a clean lifetime record; escalating"
            % (recent_dr * 100.0, recent_n)),
        (ring_gate and not captive_sybil, lambda: "none of the %d payer(s) pay a trusted "
            "anchor -- closed, unvouched cluster (possible sockpuppet ring); escalating"
            % (_pgs.get("distinct_payers") or 0)),
        (secret_hold, lambda: "payment payload contains possible sensitive data (%s) -- "
            "review before signing (would be published on-chain)"
            % ", ".join(_secret_medium)),
    ]

    # ---- verdict ----
    if stop:
        verdict = "STOP"
    elif not any(fired for fired, _reason in hold_gates):
        verdict = "GO"
    else:
        verdict = "HOLD"
        for fired, _reason in hold_gates:
            if fired:
                _r = _reason()
                reasons.extend(_r if isinstance(_r, list) else [_r])

    # When sybil_ring is DEMOTED to advisory (SYBIL_RING_GATES=False), surface it as a
    # non-blocking note instead of a gate, so a reviewer still sees the pattern.
    if sybil_ring and not ring_gate and not captive_sybil:
        reasons.append(
            "advisory: none of the %d payer(s) pay a trusted anchor (possible "
            "sockpuppet ring; not blocking -- sybil_ring gating disabled)"
            % (_pgs.get("distinct_payers") or 0)
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
        # recency-weighted dispute rate over the last window (None until the ledger
        # has dated outcomes) -- the "going bad" trend the lifetime rate hides.
        "recent_dispute_rate": (round(recent_dr, 3) if recent_dr is not None else None),
        "price_anomaly": round(a_score, 3),
        # whether the anomaly baseline is wash-trade-resistant (per-payer) or the
        # flat median -- so the caller knows how trustworthy the price norm is.
        "price_basis": price_basis,
        # how this counterparty's price compares to comparable counterparties for
        # its resource class (None = no peer market / no resource_class supplied).
        "peer_price_ratio": round(peer_ratio, 3) if peer_ratio is not None else None,
        # service category of the resource being paid for (derived from the resource
        # URL; None when no resource) + how the quoted amount compares to that
        # category's ON-CHAIN median (None = no category baseline). Advisory.
        "category": category,
        "category_price_ratio": (round(category_ratio, 3)
                                 if category_ratio is not None else None),
        # how far this payee's on-chain SETTLED median runs above its most-expensive
        # advertised price (None = not on the divergence watch-list). Bait-and-switch.
        "price_divergence_ratio": (round(_div, 3) if _div is not None else None),
        # free on-chain enrichment (Blockscout): behavioral/crowd context. DESCRIPTIVE +
        # REVIEW-only -- NOT a compliance signal. None when no enrichment source wired.
        "onchain_enrichment": enrichment,
        # leaked-secret/PII scan of the payment payload (secret_scan.py). REDACTED --
        # types + fields only, never the value. None when nothing was scanned/found.
        "secret_scan": ({"types": sorted({s.get("type") for s in secret_findings}),
                         "severity": ("high" if _secret_high else "medium")}
                        if secret_findings else None),
        # x402 settlement is on-chain and final.
        "reversibility": "irreversible",
        "blast_radius": "bounded" if not over_budget else "unbounded",
        # the merchant's Blackwall seller-audit grade, when it holds a valid badge
        # (None otherwise). Bounded/earned/revocable -- never a pay-to-whitelist.
        "seller_verified": verified_grade,
        # cross-counterparty corroboration from the payer graph + payer-reputation
        # layer (None when no graph supplied). established_payers = payers proven to
        # also pay OTHER known payees; reputable_payers = payers proven to pay a
        # trusted ANCHOR (present only when a reputation source is wired). Both are
        # hard-to-fake; captive_ratio = share paying only this payee.
        "cross_counterparty": ({
            "established_payers": _pgs.get("established_payers"),
            "captive_ratio": _pgs.get("captive_ratio"),
            "cross_score": _pgs.get("cross_score"),
            "reputable_payers": _pgs.get("reputable_payers"),
            "avg_payer_reputation": _pgs.get("avg_payer_reputation"),
            # closed unvouched cluster (no payer pays a trusted anchor). `sybil_ring` is
            # the pattern; `sybil_ring_gated` says whether it actually HOLD-gated this
            # verdict (True unless demoted via SYBIL_RING_GATES).
            "sybil_ring": sybil_ring,
            "sybil_ring_gated": ring_gate,
        } if payer_graph_signal else None),
        # temporal axis: `stale` gated the verdict above; recency/age/peak_day_share
        # are informational; `burst_sybil_advisory` is DIAGNOSTIC (backfill-windowed),
        # surfaced but never acted on. None when no timeline supplied.
        "temporal": ({
            "recency_days": _tmp.get("recency_days"),
            "age_days": _tmp.get("age_days"),
            "stale": stale,
            "peak_day_share": _tmp.get("peak_day_share"),
            "burst_sybil_advisory": bool(_tmp.get("burst_sybil")),
        } if temporal_signal else None),
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
        # How much EVIDENCE backs this verdict (level/score + backed_by/missing).
        # DESCRIPTIVE only -- it never changed the verdict above; it tells a caller
        # whether a GO rests on real history + corroboration or a thin cold start.
        "confidence": assess_confidence(record, signals),
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

    # payment_authorization = the agent's ACTUAL signed X-PAYMENT (base64) for the
    # payment being scored -- the one it's about to send the COUNTERPARTY. Optional.
    # NB: this is a request-BODY field, distinct from the transport X-PAYMENT header
    # (which pays Blackwall's own fee); payload_sim cross-checks it against the
    # claim. When present it must be a string (base64 header value).
    payment_authorization = payload.get("payment_authorization")
    if payment_authorization is not None and not isinstance(payment_authorization, str):
        return None, "payment_authorization must be a base64 X-PAYMENT string"

    # transaction = a raw contract-call payment the agent is about to sign
    # {to, data, value}; Phase-3 calldata screening flags drainer patterns. Optional.
    transaction = payload.get("transaction")
    if transaction is not None and not isinstance(transaction, dict):
        return None, "transaction must be an object {to, data, value}"

    # acquires = the tokenized RWA the agent is BUYING with this stablecoin payment
    # {token, chain?, standard?, amount?}. Optional; enables the pre-trade transfer-
    # restriction readiness check (rwa_readiness.py) -- "will the security transfer to
    # `payer` succeed, or revert on KYC/whitelist/freeze/pause?". `token` must name a
    # contract address; the rest is passed through opaque to the readiness source.
    acquires = payload.get("acquires")
    if acquires is not None:
        if not isinstance(acquires, dict):
            return None, "acquires must be an object {token, chain, standard, amount}"
        tok = acquires.get("token")
        if not isinstance(tok, str) or not tok:
            return None, "acquires.token must be a token contract address string"

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
        "payment_authorization": payment_authorization,
        "transaction": transaction,
        "acquires": acquires,
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
             hold_above=None, peer_index=None, seller_registry=None, now=None,
             graph_source=None, velocity_source=None, category_index=None,
             divergence_index=None, enrichment_source=None, verify_signer=True,
             rwa_source=None, stock_registry=None, pyth_source=None, rwa_ledger=None,
             balance_reader=None, backing_index=None, dex_source=None,
             holder_source=None, aave_source=None, issuer_trust_source=None,
             settlement_sim_source=None, auth_sim_source=None):
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

    # Payload simulation: cross-check what the agent is ACTUALLY about to sign
    # against the claim being scored. Opt-in + fail-open. Two forms:
    #   * Phase 1/2 (payment_authorization): the signed EIP-3009 transfer -- match
    #     recipient/amount/asset/chain and recover the signer.
    #   * Phase 3 (transaction): a raw contract-call payment -- flag drainer calldata
    #     (unlimited approval, setApprovalForAll, transfer to the wrong recipient).
    # Any mismatch is a hard STOP; warnings are advisory.
    sim_claim = {"counterparty": clean["counterparty"], "amount": clean["amount"],
                 "asset": clean["asset"], "chain": clean["chain"]}
    from payload_sim import check_payment_authorization
    from calldata import screen_transaction
    # Phase 1 (recipient/amount/asset/chain field match) is cheap (~us) and stays inline
    # -- its mismatches are the highest-value hard STOP. Phase 2 (signer recovery) is
    # ~30ms of pure-Python EC math (see BENCHMARKS.md); `verify_signer=False` DEFERS it to
    # a second stage (verify_signed_payment) so the fast verdict isn't blocked. When
    # deferred, signer_status="deferred" is surfaced so a GO is never mistaken for
    # signer-verified.
    _now = int(time.time()) if now is None else int(now)
    pay_check = check_payment_authorization(
        sim_claim, clean.get("payment_authorization"), decimals=6,
        now=_now, verify_signer=verify_signer)
    tx_check = screen_transaction(sim_claim, clean.get("transaction"))
    sim_mismatches = pay_check["mismatches"] + tx_check["mismatches"]
    sim_warnings = pay_check["warnings"] + tx_check["warnings"]
    signer_status = pay_check.get("signer_status", "not_applicable")

    # Seller-audit tier: if the counterparty holds a VALID Blackwall "verified
    # merchant" badge, apply its bounded, audit-earned trust floor (never a STOP
    # override -- see seller_audit.py / decide_payment).
    verified_floor = verified_grade = None
    if seller_registry is not None:
        cred = seller_registry.credential_for(clean["counterparty"],
                                              int(time.time()) if now is None else int(now))
        if cred:
            # A badge does NOT help a merchant whose LIVE disputes have since risen
            # past the audit bar (post-audit drift; re-audit + revocation are the
            # real control, but this is a cheap stale-badge guard at verdict time).
            import seller_audit
            live_dispute = float(record.get("dispute_rate") or 0.0)
            if live_dispute <= seller_audit.MAX_AUDIT_DISPUTE_RATE:
                verified_floor, verified_grade = cred["floor"], cred["grade"]

    # Cross-counterparty payer-graph signal (payer_graph.py). Fail-open: a missing
    # or raising graph source must never break the core verdict.
    payer_graph_signal = None
    if graph_source is not None:
        try:
            payer_graph_signal = graph_source.cross_signal(clean["counterparty"])
        except Exception:
            payer_graph_signal = None

    # Temporal signal (settlement_velocity.py). Fail-open, same contract.
    temporal_sig = None
    if velocity_source is not None:
        try:
            temporal_sig = velocity_source.temporal(clean["counterparty"])
        except Exception:
            temporal_sig = None

    # Free on-chain enrichment (blockscout.py). Fail-open; REVIEW-only (never STOP/clear).
    enrichment = None
    if enrichment_source is not None:
        try:
            enrichment = enrichment_source.enrich(clean["counterparty"])
        except Exception:
            enrichment = None

    # Per-category price baseline: classify the RESOURCE being paid for (from its
    # URL) and look up that category's on-chain median. Derived + advisory (HOLD-only,
    # fail-open) -- see category_pricing.py / docs/CATEGORY.md.
    category = classify_resource(clean.get("resource")) if clean.get("resource") else None
    category_median = (category_index.get(category)
                       if category_index and category else None)
    # Advertised-vs-settled divergence: a per-payee watch-list ratio (price_integrity).
    # Index keys are lowercased payees (build_divergence_index), so look up case-robust.
    divergence_ratio = (divergence_index.get(clean["counterparty"].lower())
                        if divergence_index else None)

    # Leaked-secret / PII scan of the RAW request body's free-text fields (secret_scan.py).
    # HIGH (credential) -> STOP; MEDIUM (PII) -> HOLD. Pure regex; wrapped defensively.
    try:
        import secret_scan
        secret_findings = secret_scan.scan_payload(payload)
    except Exception:
        secret_findings = None

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
        payload_mismatch_reasons=sim_mismatches,
        verified_floor=verified_floor, verified_grade=verified_grade,
        payer_graph_signal=payer_graph_signal,
        temporal_signal=temporal_sig,
        category=category, category_median=category_median,
        divergence_ratio=divergence_ratio,
        enrichment=enrichment,
        secret_findings=secret_findings,
    )
    # Surface advisory (non-blocking) simulation warnings, e.g. an expired auth or
    # a bounded approval.
    if sim_warnings:
        verdict["reasons"].extend(sim_warnings)

    # Make the Phase-2 signer state EXPLICIT in every response. When it was DEFERRED, a
    # non-STOP verdict is only PROVISIONAL w.r.t. signer authenticity -- say so, so a
    # caller runs verify_signed_payment() before relying on a GO. (Phase-1 field-match
    # hard-stops already applied inline regardless.)
    verdict["signals"]["payload_signer_status"] = signer_status
    if signer_status == "deferred" and verdict["verdict"] != "STOP":
        verdict["reasons"].append(
            "signer verification DEFERRED (fast path) -- this verdict does NOT confirm "
            "the payment's cryptographic signer; run the second-stage check "
            "(verify_signed_payment) before treating a GO as signer-verified")

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

    # PRE-SIGNATURE SETTLEMENT FEASIBILITY (settlement_sim.py) -- the CORE x402 path, not
    # just RWA buys. Simulate the actual stablecoin transfer on-chain before the agent
    # signs: if the PAYEE (or the PAYER) is frozen/blacklisted on the USDC contract, the
    # payment would REVERT -- and a blacklisted counterparty is itself a serious risk
    # signal (Circle freezes for cause). A CONTROL simulation separates payer-side from
    # payee-side blocks so a revert is never misattributed. HOLD-only, never STOP
    # (sanctions.py stays the compliance authority), fail-open, opt-in.
    if settlement_sim_source is not None:
        from settlement_sim import apply_settlement_sim
        try:
            _ss = settlement_sim_source.check(clean)
        except Exception:
            _ss = None
        verdict = apply_settlement_sim(verdict, _ss)

    # EIP-3009 AUTHORIZATION simulation (auth_sim.py). settlement_sim simulates a plain
    # `transfer`, but x402 settles via `transferWithAuthorization` -- so a REPLAYED nonce,
    # an out-of-window authorization, or a call that reverts for signature reasons are
    # invisible to it. This checks the ACTUAL signed authorization against the chain
    # (cheap `authorizationState` read first, full execution sim only if that is clean).
    # HOLD-only; payload_sim keeps the STOP authority for a mismatched/forged payload.
    if auth_sim_source is not None and clean.get("payment_authorization"):
        from auth_sim import apply_auth_sim
        try:
            from x402 import decode_payment_header
            _pay = decode_payment_header(clean["payment_authorization"])
            _as = auth_sim_source.check(clean, _pay)
        except Exception:
            _as = None
        verdict = apply_auth_sim(verdict, _as)

    # Tokenized-RWA transfer-restriction readiness (rwa_readiness.py). When the agent
    # is BUYING a tokenized RWA (request carries `acquires`), pre-check whether the
    # security can actually transfer to the receiving wallet (`payer`) or would revert
    # on KYC/whitelist/freeze/pause -- the asset-side question a stablecoin-only flow
    # never raises. FAIL OPEN at the consumer and CONSERVATIVE-ONLY: it can escalate
    # GO->HOLD on a blocked transfer but never upgrades, and never reaches STOP.
    # RWA-buy signals shared across the readiness / discovery / peg / ledger folds.
    rwa_signal = asset_record = peg = pre_balance = None
    # DEFINITIVE settlement: snapshot the payer's PRE-buy token balance now (opt-in,
    # only when accumulating), so the T+N outcome loop can prove the security arrived
    # (post > pre). One balanceOf on the RWA hot path when wired; fail-open.
    if (balance_reader is not None and rwa_ledger is not None
            and clean.get("acquires") and clean.get("payer")):
        _acq = clean["acquires"]
        try:
            pre_balance = balance_reader.balance_of(_acq.get("token"), _acq.get("chain"),
                                                    clean.get("payer"))
        except Exception:
            pre_balance = None
    if rwa_source is not None and clean.get("acquires"):
        from rwa_readiness import apply_rwa_readiness
        try:
            rwa_signal = rwa_source.check(clean["acquires"], clean.get("payer"),
                                          counterparty=clean.get("counterparty"))
        except Exception:
            rwa_signal = None
        verdict = apply_rwa_readiness(verdict, rwa_signal)

    # Tokenized-stock DISCOVERY enrichment (tokenized_stock_registry.py). When the
    # acquired token is a KNOWN tokenized security, annotate the verdict with its
    # issuer / underlying / ISIN (descriptive, like categories.py) and escalate
    # GO->HOLD if the underlying's trading is halted. Fail-open, conservative-only.
    if stock_registry is not None and clean.get("acquires"):
        from tokenized_stock_registry import apply_asset_registry
        acq = clean["acquires"]
        try:
            asset_record = stock_registry.lookup(acq.get("token"), acq.get("chain"))
        except Exception:
            asset_record = None
        verdict = apply_asset_registry(verdict, asset_record)

    # Peg / NAV divergence (pyth_price.py): is the agent paying near the REAL underlying
    # stock price, or a big premium (depeg / stale quote / bad route)? Needs the
    # recognized underlying symbol + a derivable unit price (acquires.unit_price or
    # amount/quantity). Keyless Pyth; HOLD-only on overpay; fail-open.
    if pyth_source is not None and asset_record and asset_record.get("underlying_symbol") \
            and clean.get("acquires"):
        from pyth_price import apply_peg, compute_peg
        try:
            peg = compute_peg(pyth_source, asset_record.get("underlying_symbol"),
                              clean["amount"], clean["acquires"])
        except Exception:
            peg = None
        verdict = apply_peg(verdict, peg)

    # Proof-of-reserves BACKING (backed_oracle.py): is the recognized tokenized security
    # actually collateralized (shares held vs tokens in circulation)? Keyless issuer PoR;
    # under-collateralized -> GO->HOLD. Conservative-only, fail-open.
    if backing_index is not None and asset_record and asset_record.get("symbol"):
        from backed_oracle import apply_backing, assess_backing
        try:
            _bk = assess_backing(backing_index.backing(asset_record.get("symbol")))
        except Exception:
            _bk = None
        verdict = apply_backing(verdict, _bk)

    # DEX market-vs-NAV peg (dex_price.py): the oracle-managed peg tracks the underlying
    # by construction, so it can't see the TOKEN trading off its NAV on an actual pool
    # (bait/thin liquidity / market depeg). Read the token's live Uniswap-v3 price and
    # compare to the underlying (Pyth). Off-peg -> HOLD. Opt-in, conservative, fail-open.
    if (dex_source is not None and pyth_source is not None and asset_record
            and asset_record.get("underlying_symbol") and clean.get("acquires")):
        from dex_price import (apply_market_peg, assess_execution,
                               assess_market_peg)
        _acq = clean["acquires"]
        try:
            _underp = pyth_source.price(asset_record.get("underlying_symbol"))
            # Prefer the EXECUTABLE price (QuoterV2, size-aware -- catches slippage at the
            # agent's actual trade size); fall back to the pool SPOT mid if no quoter.
            _exe = dex_source.executable_price(_acq.get("token"), _acq.get("chain"),
                                               clean["amount"])
            if _exe:
                _mp = assess_execution(_exe["effective_price"], _underp, _exe["spot_price"])
            else:
                _dexp = dex_source.price(_acq.get("token"), _acq.get("chain"),
                                         pool=_acq.get("dex_pool"))
                _mp = assess_market_peg(_dexp, _underp)
        except Exception:
            _mp = None
        verdict = apply_market_peg(verdict, _mp)

    # Holder-concentration rug-check (holder_concentration.py): a single non-contract
    # wallet holding a dominant share of the token supply -> dump/manipulation risk.
    # Keyless Blockscout; HOLD-only, fail-open, contract holders excluded (issuer custody).
    if holder_source is not None and clean.get("acquires"):
        from holder_concentration import apply_concentration
        _acq = clean["acquires"]
        try:
            _hc = holder_source.check(_acq.get("token"), _acq.get("chain"))
        except Exception:
            _hc = None
        # ADVISORY (gate=False): records the signal + feeds the aggregate, but doesn't
        # gate alone (noisier for RWAs). The aggregation layer weighs it collectively.
        verdict = apply_concentration(verdict, _hc, gate=False)

    # Aave reserve quality (aave_reserve.py) -- ADVISORY: a token Aave has FROZEN was
    # de-risked by a major protocol (feeds the aggregate); a listed reserve is a
    # positive note. Keyless eth_call; never gates alone, fail-open.
    if aave_source is not None and clean.get("acquires"):
        from aave_reserve import apply_aave
        _acq = clean["acquires"]
        try:
            _av = aave_source.check(_acq.get("token"), _acq.get("chain"))
        except Exception:
            _av = None
        verdict = apply_aave(verdict, _av)

    # Earned ISSUER-TRUST grade (issuer_trust_gate.py) -- the payoff of the accumulation
    # corpus: an issuer's LABELED settlement/outcome history graded high/medium/low. O(1)
    # lookup off a precomputed snapshot. DESCRIPTIVE by default (ISSUER_TRUST_GATES lock
    # off -> recorded, never gates); when calibrated + the lock flips, a LOW grade becomes
    # an advisory signal the aggregate weighs. Never STOP, fail-open.
    if issuer_trust_source is not None and asset_record and asset_record.get("issuer"):
        from issuer_trust_gate import apply_issuer_trust
        try:
            _grade = issuer_trust_source.grade(asset_record.get("issuer"))
        except Exception:
            _grade = None
        verdict = apply_issuer_trust(verdict, _grade)

    # AGGREGATION / confidence layer (rwa_aggregate.py): compose the ~8 RWA signals into
    # ONE weighted risk view (signals.rwa_risk: score/level/primary_concern/concerns) and
    # apply the controlled collective rule -- advisory signals agreeing past a threshold
    # escalate GO->HOLD once. Descriptive + conservative; the strong gates already decided.
    if clean.get("acquires"):
        from rwa_aggregate import aggregate, apply_aggregate
        verdict = apply_aggregate(verdict, aggregate(verdict.get("signals")))

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

    # ACCUMULATION (rwa_ledger.py): write down every RWA buy + its context (asset,
    # restriction grade, peg, verdict), keyed by the receipt_id join key so a later
    # OUTCOME can be linked back to it. The private corpus no competitor has.
    # Fail-soft: logging must never break a verdict.
    if rwa_ledger is not None and clean.get("acquires"):
        try:
            from rwa_ledger import build_rwa_event
            rwa_ledger.record(build_rwa_event(clean, verdict, asset_record,
                                              rwa_signal, peg, now=_now,
                                              receipt_id=verdict["receipt_id"],
                                              pre_balance=pre_balance))
        except Exception:
            pass

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


def verify_signed_payment(payload, now=None):
    """SECOND-STAGE signer verification (the ~30ms path deferred by
    `forecast(..., verify_signer=False)`). Recovers the EIP-3009 signer from the request
    body's `payment_authorization` and confirms it matches the claim. Returns
        {ok, signer_status, mismatches[], warnings[]}
    where `ok` is False iff the signer is forged / mismatched -- a caller that got a fast
    GO MUST NOT submit the payment when `ok` is False. Pure w.r.t. the verdict engine
    (no reputation lookup); NEVER raises. Idempotent -- safe to call once per payment.

    `ok` means "not PROVEN forged" -- it is True for signer_status 'confirmed' AND for
    'unverified' (e.g. an unknown asset with no trusted EIP-712 domain, where recovery
    isn't possible; this matches the inline behavior, which warns rather than STOPs). A
    caller wanting STRICT proof-of-signer should require signer_status == 'confirmed',
    not merely ok == True.

    Usage:
        v, err = forecast(body, src, verify_signer=False)      # fast, ~90us
        if not err and v["verdict"] != "STOP":
            r = verify_signed_payment(body)                    # ~30ms, only if worth it
            if not r["ok"]:                                    # forged signer -> do NOT submit
                ...treat as STOP...
    """
    from payload_sim import check_payment_authorization
    body = payload if isinstance(payload, dict) else {}
    claim = {"counterparty": body.get("counterparty"), "amount": body.get("amount"),
             "asset": body.get("asset"), "chain": body.get("chain")}
    _now = int(time.time()) if now is None else int(now)
    res = check_payment_authorization(claim, body.get("payment_authorization"),
                                      decimals=6, now=_now, verify_signer=True)
    return {"ok": not res["mismatches"], "signer_status": res.get("signer_status"),
            "mismatches": res["mismatches"], "warnings": res["warnings"]}


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
class _Stats:
    """Thread-safe integer counters exposed at GET /stats (requests, verdicts,
    rate_limited). No PII -- just aggregate counts for basic observability."""

    def __init__(self):
        self._lock = threading.Lock()
        self._c = {}

    def incr(self, key, n=1):
        with self._lock:
            self._c[key] = self._c.get(key, 0) + n

    def snapshot(self):
        with self._lock:
            return dict(self._c)


class _Handler(BaseHTTPRequestHandler):
    server_version = "Blackwall/0.1"

    # Injected by BlackwallServer.
    reputation_source = None
    ledger = None
    billing = None  # x402.BillingGate, or None to disable billing
    readiness_source = None  # readiness.OntarioReadinessSource, or None
    hold_above = None  # amount above which the verdict escalates to HOLD (None = default)
    peer_index = None  # {resource_class: peer_median} for the peer-group check, or None
    category_index = None  # {category: on-chain median} for the category-price check, or None
    divergence_index = None  # {payee: settled/advertised ratio} bait-and-switch watch-list, or None
    enrichment_source = None  # blockscout.BlockscoutEnrichmentSource (REVIEW-only), or None
    rwa_source = None  # rwa_readiness.RwaReadinessSource (RWA transfer-restriction), or None
    stock_registry = None  # tokenized_stock_registry.TokenizedStockRegistry (discovery), or None
    pyth_source = None  # pyth_price.PythPriceSource (peg/NAV divergence), or None
    rwa_ledger = None  # rwa_ledger.RwaLedger (accumulation corpus), or None
    balance_reader = None  # rwa_balance.BalanceReader (pre-buy snapshot), or None
    backing_index = None  # backed_oracle.BackedOracleIndex (proof-of-reserves), or None
    dex_source = None  # dex_price.DexPriceSource (market-vs-NAV peg), or None
    holder_source = None  # holder_concentration.HolderConcentrationSource (rug-check), or None
    aave_source = None  # aave_reserve.AaveReserveSource (advisory quality), or None
    issuer_trust_source = None  # issuer_trust_gate.IssuerTrustSource (earned grade), or None
    settlement_sim_source = None  # settlement_sim.SettlementSimSource (pre-sig feasibility)
    auth_sim_source = None  # auth_sim.AuthorizationSimSource (EIP-3009 replay/window)
    graph_source = None  # payer_reputation.PayerReputationSource (Sybil corroboration)
    velocity_source = None  # settlement_velocity.VelocitySource (stale gate)
    openapi_server_url = None  # public origin for the openapi.json servers[] (or None)
    verdict_anchor = None  # verdict_anchor.VerdictAnchor, or None (opt-in audit trail)
    rate_limiter = None  # ratelimit.RateLimiter for POSTs, or None (limiting off)
    stats = None  # _Stats counter (always set by the server)

    def _client_key(self):
        from ratelimit import client_ip_from
        return client_ip_from(self.headers.get("X-Forwarded-For"),
                              self.client_address[0] if self.client_address else None)

    def _rate_ok(self):
        """True if this POST is within the per-client rate limit (or limiting is off).
        On deny, emits 429 + Retry-After and records the block."""
        if self.rate_limiter is None:
            return True
        allowed, retry = self.rate_limiter.allow(self._client_key(), time.monotonic())
        if not allowed:
            if self.stats is not None:
                self.stats.incr("rate_limited")
            self._send_json(429, {"error": "rate limit exceeded; slow down"},
                            extra_headers={"Retry-After": str(int(retry) + 1)})
        return allowed

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
        elif self.path == "/stats":
            # Lightweight observability: request + verdict counters. No PII (IPs are
            # only kept in-memory for rate-limiting, never logged here).
            self._send_json(200, self.stats.snapshot() if self.stats else {})
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
        # Per-client rate limit on the (expensive) POST paths only -- GET health/
        # discovery are cheap and must stay unthrottled for health checkers.
        if self.path in ("/v1/forecast-payment", "/v1/report-outcome", "/v1/session"):
            if self.stats is not None:
                self.stats.incr("requests")
            if not self._rate_ok():
                return
        try:
            if self.path == "/v1/forecast-payment":
                self._do_forecast()
            elif self.path == "/v1/verify-signer":
                self._do_verify_signer()
            elif self.path == "/v1/report-outcome":
                self._do_report_outcome()
            elif self.path == "/v1/session":
                self._do_session()
            else:
                self._send_json(404, {"error": "not found"})
        except Exception as e:
            # Unexpected crash in a handler (e.g. a raising reputation store / ledger).
            # Fail CLOSED + gracefully: a 503 is never a GO, so no bad payment is
            # approved, and the client gets a clean response instead of a dropped
            # connection or a leaked traceback. The operator sees it in stderr + /stats.
            if self.stats is not None:
                self.stats.incr("errors")
            sys.stderr.write("blackwall: 503 unhandled %s on %s: %s\n"
                             % (type(e).__name__, self.path, e))
            sys.stderr.flush()
            try:
                self._send_json(503, {"error": "verdict engine temporarily unavailable"})
            except Exception:
                pass   # response may already be partly written; don't cascade

    def _do_verify_signer(self):
        """Stage-2 signer verification for a request that got a fast (deferred) verdict.
        Runs the ~30ms EIP-3009 signer recovery and returns {ok, signer_status,
        mismatches, warnings}. `ok:false` means the signer is forged/mismatched -- do NOT
        submit the payment. Rate-limited via do_POST like every other route."""
        payload, err = self._read_json_body()
        if err is not None:
            self._send_json(400, {"error": err})
            return
        self._send_json(200, verify_signed_payment(payload))

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

        # Two-stage opt-in: a request may set `defer_signer: true` to get the FAST verdict
        # (Phase-1 field hard-stops inline, ~sub-ms) and skip the ~30ms Phase-2 signer
        # recovery, then complete it via POST /v1/verify-signer. Default keeps Phase 2
        # inline, so deployed behavior is unchanged unless a caller opts in.
        # STRICT `is True`: a malformed flag (e.g. the STRING "false", which is truthy)
        # must NOT silently reduce verification -- fail toward MORE checking, not less.
        _verify_signer = not (isinstance(payload, dict)
                              and payload.get("defer_signer") is True)
        response, err = forecast(payload, self.reputation_source, self.ledger,
                                 readiness_source=self.readiness_source,
                                 hold_above=self.hold_above,
                                 peer_index=self.peer_index,
                                 graph_source=self.graph_source,
                                 velocity_source=self.velocity_source,
                                 category_index=self.category_index,
                                 divergence_index=self.divergence_index,
                                 enrichment_source=self.enrichment_source,
                                 rwa_source=self.rwa_source,
                                 stock_registry=self.stock_registry,
                                 pyth_source=self.pyth_source,
                                 rwa_ledger=self.rwa_ledger,
                                 balance_reader=self.balance_reader,
                                 backing_index=self.backing_index,
                                 dex_source=self.dex_source,
                                 holder_source=self.holder_source,
                                 aave_source=self.aave_source,
                                 issuer_trust_source=self.issuer_trust_source,
                                 settlement_sim_source=self.settlement_sim_source,
                                 auth_sim_source=self.auth_sim_source,
                                 verify_signer=_verify_signer)
        if err is not None:
            self._send_json(400, {"error": err})
            return
        if self.stats is not None:
            self.stats.incr("verdict_" + str(response.get("verdict", "?")).lower())
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
        # OPT-IN audit trail: anchor the verdict digest to Traceipt. Done AFTER the
        # response is written and is itself non-blocking (fire-and-forget on a
        # daemon thread) + fail-open, so it can never delay or break the verdict.
        if self.verdict_anchor is not None:
            self.verdict_anchor.submit(response)

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
                 hold_above=None, peer_index=None, openapi_server_url=None,
                 graph_source=None, velocity_source=None, verdict_anchor=None,
                 category_index=None, divergence_index=None, rate_limiter=None,
                 enrichment_source=None, rwa_source=None, stock_registry=None,
                 pyth_source=None, rwa_ledger=None, balance_reader=None,
                 backing_index=None, dex_source=None, holder_source=None,
                 aave_source=None, issuer_trust_source=None,
                 settlement_sim_source=None, auth_sim_source=None):
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
        self.graph_source = graph_source
        self.velocity_source = velocity_source
        self.openapi_server_url = openapi_server_url
        self.verdict_anchor = verdict_anchor
        self.category_index = category_index
        self.divergence_index = divergence_index
        self.rate_limiter = rate_limiter
        self.enrichment_source = enrichment_source
        self.rwa_source = rwa_source
        self.stock_registry = stock_registry
        self.pyth_source = pyth_source
        self.rwa_ledger = rwa_ledger
        self.balance_reader = balance_reader
        self.backing_index = backing_index
        self.dex_source = dex_source
        self.holder_source = holder_source
        self.aave_source = aave_source
        self.issuer_trust_source = issuer_trust_source
        self.settlement_sim_source = settlement_sim_source
        self.auth_sim_source = auth_sim_source
        self.stats = _Stats()
        self._httpd = None

    def serve_forever(self):
        handler = type("_BoundHandler", (_Handler,),
                       {"reputation_source": self.reputation_source,
                        "ledger": self.ledger,
                        "billing": self.billing,
                        "readiness_source": self.readiness_source,
                        "hold_above": self.hold_above,
                        "peer_index": self.peer_index,
                        "graph_source": self.graph_source,
                        "velocity_source": self.velocity_source,
                        "verdict_anchor": self.verdict_anchor,
                        "category_index": self.category_index,
                        "divergence_index": self.divergence_index,
                        "rate_limiter": self.rate_limiter,
                        "enrichment_source": self.enrichment_source,
                        "rwa_source": self.rwa_source,
                        "stock_registry": self.stock_registry,
                        "pyth_source": self.pyth_source,
                        "rwa_ledger": self.rwa_ledger,
                        "balance_reader": self.balance_reader,
                        "backing_index": self.backing_index,
                        "dex_source": self.dex_source,
                        "holder_source": self.holder_source,
                        "aave_source": self.aave_source,
                        "issuer_trust_source": self.issuer_trust_source,
                        "settlement_sim_source": self.settlement_sim_source,
                        "auth_sim_source": self.auth_sim_source,
                        "stats": self.stats,
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
def _env_flag(name):
    """A BLACKWALL_* boolean env var. Truthy ONLY for 1/true/yes/on
    (case-insensitive); "0"/"false"/"no"/""/unset -> False. `bool(os.environ.get())`
    is a trap -- bool("0") is True -- so a flag set to "0" could never be disabled."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


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
                   default=_env_flag("BLACKWALL_VALUE_PRICING"),
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
                   default=_env_flag("BLACKWALL_INGEST"),
                   help="with --store, self-populate from chain on first sight "
                        "of a counterparty (first call slow, then cached)")
    p.add_argument("--sanctions", default=os.environ.get("BLACKWALL_SANCTIONS"),
                   help="path to an OFAC sanctioned-address file; STOPs sanctioned "
                        "counterparties (the free-baseline check, in one call)")
    p.add_argument("--sanctions-refresh", action="store_true",
                   default=_env_flag("BLACKWALL_SANCTIONS_REFRESH"),
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
                   default=_env_flag("BLACKWALL_READINESS_LOCAL"),
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
    graph_source = velocity_source = None
    if args.store:
        from reputation_store import production_source, ReputationStore
        reputation_source = production_source(args.store, ledger=led,
                                              ingest=args.ingest)
        # Full signal stack off the same store: cross-counterparty Sybil
        # corroboration (payer graph + reputation) and the temporal `stale` gate.
        from payer_reputation import PayerReputationSource
        from settlement_velocity import VelocitySource
        graph_source = PayerReputationSource.from_store(ReputationStore(args.store))
        velocity_source = VelocitySource(ReputationStore(args.store))

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
                          BillingGate, PricingPolicy, choose_facilitator)
        # CDP facilitator (authenticated) when CDP creds are present -- this is
        # the path that gets settlements catalogued by Coinbase Bazaar. Community
        # facilitators (keyless) settle on-chain but never list. The selection
        # (and the guard against misrouting a CDP token to a community URL) lives
        # in choose_facilitator so it can be unit-tested.
        facilitator, fac_note = choose_facilitator(
            args.facilitator, os.environ.get("CDP_API_KEY_ID"),
            os.environ.get("CDP_API_KEY_SECRET"))
        sys.stderr.write("blackwall: %s\n" % fac_note)
        sys.stderr.flush()
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

    # OPT-IN per-category price baseline: a precomputed {category: on-chain median}
    # JSON (build it with `python category_pricing.py --store ... --out ...`). Loaded
    # from BLACKWALL_CATEGORY_INDEX; fail-open -- a missing/garbled file just disables
    # the category signal, never blocks boot. Shared loader with the MCP server so the
    # two can't drift. See docs/CATEGORY.md.
    from category_pricing import load_category_index, load_index_json
    category_index, _cat_err = load_category_index(
        os.environ.get("BLACKWALL_CATEGORY_INDEX"))
    if category_index:
        sys.stdout.write("blackwall: category price baselines ON (%d categories)\n"
                         % len(category_index))
    elif _cat_err:
        sys.stderr.write("blackwall: WARNING category index unusable (%s) -- "
                         "category price signal OFF\n" % _cat_err)
        sys.stderr.flush()

    # Advertised-vs-settled price-divergence watch-list (price_integrity.py); fail-open.
    divergence_index, _div_err = load_index_json(
        os.environ.get("BLACKWALL_DIVERGENCE_INDEX"))
    if divergence_index:
        sys.stdout.write("blackwall: price-divergence watch-list ON (%d payees)\n"
                         % len(divergence_index))
    elif _div_err:
        sys.stderr.write("blackwall: WARNING divergence index unusable (%s) -- "
                         "price-integrity signal OFF\n" % _div_err)
        sys.stderr.flush()

    # OPT-IN per-client rate limit for the public POST paths. BLACKWALL_RATE_LIMIT =
    # max requests per BLACKWALL_RATE_WINDOW seconds (default 60) per client IP; unset
    # or <=0 disables it. BLACKWALL_RATE_BURST caps the instantaneous burst (default =
    # the limit). Fail-open: a bad value just leaves limiting off.
    rate_limiter = None
    try:
        _rl = int(os.environ.get("BLACKWALL_RATE_LIMIT", "0"))
    except ValueError:
        _rl = 0
    if _rl > 0:
        from ratelimit import RateLimiter
        try:
            _win = float(os.environ.get("BLACKWALL_RATE_WINDOW", "60"))
            _burst = os.environ.get("BLACKWALL_RATE_BURST")
            rate_limiter = RateLimiter(_rl, _win,
                                       burst=int(_burst) if _burst else None)
            sys.stdout.write("blackwall: rate limit ON (%d req / %gs per client, "
                             "burst %g)\n" % (_rl, _win, rate_limiter.capacity))
            sys.stdout.flush()
        except (ValueError, TypeError) as e:
            sys.stderr.write("blackwall: WARNING bad rate-limit config (%s) -- OFF\n" % e)
            sys.stderr.flush()

    # OPT-IN (BLACKWALL_ONCHAIN_ENRICH=1): free Blockscout enrichment. Adds a LIVE call
    # per verdict (latency + keyless rate limits), so it's off by default; fail-open and
    # REVIEW-only (never a sanctions clear/hit). See blockscout.py.
    enrichment_source = None
    if _env_flag("BLACKWALL_ONCHAIN_ENRICH"):
        from blockscout import BlockscoutEnrichmentSource, BLOCKSCOUT_BASE
        _bs = os.environ.get("BLACKWALL_BLOCKSCOUT_URL", BLOCKSCOUT_BASE)
        enrichment_source = BlockscoutEnrichmentSource(base_url=_bs)
        sys.stdout.write("blackwall: on-chain enrichment ON (Blockscout %s -- REVIEW-only "
                         "behavioral context; live call per verdict)\n" % _bs)
        sys.stdout.flush()

    # OPT-IN (BLACKWALL_RWA_READINESS=1): pre-trade transfer-restriction readiness for
    # agents BUYING tokenized RWAs with stablecoins. Only fires when a request carries
    # `acquires`; adds a few eth_call view reads per such verdict against
    # BLACKWALL_RWA_RPC_URL. Fail-open + conservative (GO->HOLD on a blocked transfer,
    # never STOP). See docs/TOKENIZED_RWA.md.
    rwa_source = None
    if _env_flag("BLACKWALL_RWA_READINESS"):
        from rwa_readiness import CombinedRwaReadinessSource, RwaReadinessSource
        _rpc = os.environ.get("BLACKWALL_RWA_RPC_URL", "")
        _sol_rpc = os.environ.get("BLACKWALL_RWA_SOLANA_RPC_URL", "")
        _sources = [RwaReadinessSource(rpc_url=_rpc)]
        if _sol_rpc:
            from solana_rwa import SolanaRwaReadinessSource
            _sources.append(SolanaRwaReadinessSource(rpc_url=_sol_rpc))
        # ERC-8226 (RAMS) agent-authorization axis -- DORMANT-BUT-READY: idle until a
        # request advertises `acquires.mandate_registry` (or BLACKWALL_RAMS_REGISTRY),
        # then it reads canExecute on the same EVM RPC. Wired now so it activates the day
        # an asset ships a RAMS hook, with zero code change. See docs/TOKENIZED_RWA.md.
        if _rpc:
            from rams_readiness import RamsReadinessSource
            _sources.append(RamsReadinessSource(
                rpc_url=_rpc,
                default_registry=os.environ.get("BLACKWALL_RAMS_REGISTRY") or None))
        rwa_source = CombinedRwaReadinessSource(_sources)
        sys.stdout.write("blackwall: RWA transfer-restriction readiness ON (EVM %s%s -- "
                         "pre-trade check when a request carries `acquires`; fail-open, "
                         "HOLD-only)\n" % (_rpc or "NO RPC url -> fail-open unknown",
                         "; Solana " + _sol_rpc if _sol_rpc else ""))
        sys.stdout.flush()

    # OPT-IN (BLACKWALL_STOCK_REGISTRY=1): tokenized-stock discovery enrichment. Fetches
    # the keyless Backed/xStocks feed at startup (one network call) into an in-memory
    # registry; recognizes a known tokenized security in `acquires` and annotates the
    # verdict (issuer/underlying/ISIN + trading_halted HOLD). Fail-soft (empty on error).
    stock_registry = None
    if _env_flag("BLACKWALL_STOCK_REGISTRY"):
        from tokenized_stock_registry import build_registry
        stock_registry = build_registry()
        sys.stdout.write("blackwall: tokenized-stock discovery ON (%d known deployments "
                         "from the keyless Backed feed; descriptive + trading-halt HOLD)\n"
                         % len(stock_registry))
        sys.stdout.flush()

    # With the registry on, also load Backed's keyless oracle + proof-of-reserves index:
    # an authoritative Pyth feed map (hardens the peg gate) + a BACKING gate
    # (under-collateralized -> HOLD). Fail-soft.
    backing_index = None
    if stock_registry is not None:
        from backed_oracle import load_backed_oracle_index
        backing_index = load_backed_oracle_index()
        sys.stdout.write("blackwall: Backed oracle+PoR ON (%d reserves, %d authoritative "
                         "Pyth feeds; under-collateralized HOLD)\n"
                         % (len(backing_index.reserves), len(backing_index.feed_map())))
        sys.stdout.flush()

    # OPT-IN (BLACKWALL_PYTH=1, needs the stock registry): peg/NAV divergence via the
    # keyless Pyth oracle -- flags a tokenized stock priced far over its real underlying.
    # Seed the price source with the issuer-authoritative feed map so it resolves the
    # exact Pyth feed instead of heuristically searching by ticker.
    pyth_source = None
    if _env_flag("BLACKWALL_PYTH") and stock_registry is not None:
        from pyth_price import PythPriceSource
        _fm = backing_index.feed_map() if backing_index is not None else None
        pyth_source = PythPriceSource(feed_map=_fm)
        sys.stdout.write("blackwall: peg/NAV divergence ON (keyless Pyth; overpay-vs-"
                         "underlying HOLD when acquires carries unit_price/quantity)\n")
        sys.stdout.flush()

    # OPT-IN (BLACKWALL_DEX=1, needs pyth + an EVM RPC): DEX market-vs-NAV peg. Reads the
    # token's live Uniswap-v3 price and flags a material deviation from the underlying
    # (depeg / thin liquidity / bait pool) the oracle-managed peg can't see.
    dex_source = None
    if _env_flag("BLACKWALL_DEX") and pyth_source is not None:
        _dex_rpc = os.environ.get("BLACKWALL_RWA_RPC_URL", "")
        if _dex_rpc:
            from dex_price import MIN_QUOTE_LIQUIDITY, DexPriceSource
            try:
                _min_liq = int(os.environ.get("BLACKWALL_DEX_MIN_LIQ", MIN_QUOTE_LIQUIDITY))
            except ValueError:
                _min_liq = MIN_QUOTE_LIQUIDITY
            dex_source = DexPriceSource(rpc_url=_dex_rpc, min_liquidity=_min_liq)
            sys.stdout.write("blackwall: DEX market-peg ON (Uniswap-v3 deepest pool >= "
                             "%d USDC atomic; QuoterV2 executable price vs underlying NAV; "
                             "off-peg/high-slippage HOLD)\n" % _min_liq)
            sys.stdout.flush()

    # OPT-IN (BLACKWALL_HOLDER_CONCENTRATION=1): holder-concentration rug-check. Keyless
    # Blockscout; a dominant NON-CONTRACT holder -> HOLD. Fires on any RWA/token buy.
    holder_source = None
    if _env_flag("BLACKWALL_HOLDER_CONCENTRATION"):
        from holder_concentration import HolderConcentrationSource
        holder_source = HolderConcentrationSource()
        sys.stdout.write("blackwall: holder-concentration rug-check ON (keyless "
                         "Blockscout; dominant non-contract holder -> HOLD)\n")
        sys.stdout.flush()

    # OPT-IN (BLACKWALL_AAVE=1, needs an EVM RPC): Aave reserve quality (advisory). A
    # token Aave has frozen -> risk note (feeds the aggregate); listed -> positive note.
    aave_source = None
    if _env_flag("BLACKWALL_AAVE"):
        _aave_rpc = os.environ.get("BLACKWALL_RWA_RPC_URL", "")
        if _aave_rpc:
            from aave_reserve import AaveReserveSource
            aave_source = AaveReserveSource(rpc_url=_aave_rpc)
            sys.stdout.write("blackwall: Aave reserve quality ON (advisory; frozen "
                             "reserve -> risk note, weighed collectively)\n")
            sys.stdout.flush()

    # OPT-IN (BLACKWALL_RWA_LEDGER=<path>): the ACCUMULATION corpus -- append every RWA
    # buy + its asset/restriction/peg context to build the private history no one else has.
    rwa_ledger = None
    _rwa_led_path = os.environ.get("BLACKWALL_RWA_LEDGER", "")
    if _rwa_led_path:
        from rwa_ledger import RwaLedger
        rwa_ledger = RwaLedger(_rwa_led_path)
        sys.stdout.write("blackwall: RWA accumulation corpus ON -> %s (per-asset / "
                         "per-issuer history)\n" % _rwa_led_path)
        sys.stdout.flush()

    # DEFINITIVE settlement snapshot: when the corpus is on AND an RPC is configured,
    # snapshot the payer's pre-buy token balance so the outcome loop can prove arrival
    # (post > pre). One balanceOf on the RWA hot path; opt-in via the same RPC vars.
    balance_reader = None
    if rwa_ledger is not None:
        _bal_evm = os.environ.get("BLACKWALL_RWA_RPC_URL", "")
        _bal_sol = os.environ.get("BLACKWALL_RWA_SOLANA_RPC_URL", "")
        if _bal_evm or _bal_sol:
            from rwa_balance import BalanceReader
            balance_reader = BalanceReader(evm_rpc_url=_bal_evm, solana_rpc_url=_bal_sol)
            sys.stdout.write("blackwall: definitive settlement snapshot ON (pre-buy "
                             "balance recorded for the T+N delta)\n")
            sys.stdout.flush()

    # EARNED issuer-trust grade (issuer_trust_gate.py): when the accumulation corpus is
    # on, precompute the per-issuer grade snapshot ONCE at startup so every RWA verdict
    # carries the issuer's LABELED settlement/outcome grade (O(1) lookup on the hot path).
    # DESCRIPTIVE by default (ISSUER_TRUST_GATES lock off); flips to an advisory signal
    # only once calibrated. Fail-open -- a broken corpus yields an empty snapshot.
    issuer_trust_source = None
    if rwa_ledger is not None:
        from issuer_trust_gate import IssuerTrustSource
        issuer_trust_source = IssuerTrustSource.from_ledger(rwa_ledger)
        sys.stdout.write("blackwall: issuer-trust grade ON (%d issuer(s) graded from the "
                         "corpus; descriptive until ISSUER_TRUST_GATES is calibrated)\n"
                         % len(issuer_trust_source))
        sys.stdout.flush()

    # OPT-IN (BLACKWALL_SETTLEMENT_SIM=1 + an RPC): PRE-SIGNATURE settlement feasibility
    # for every x402 payment -- simulate the USDC transfer and catch a frozen/blacklisted
    # payee or payer BEFORE the agent signs. One eth_call on the hot path (a second only
    # when the first reverts, to attribute the block); HOLD-only, never STOP, fail-open.
    settlement_sim_source = None
    auth_sim_source = None
    if _env_flag("BLACKWALL_SETTLEMENT_SIM"):
        _sim_rpcs = {}
        for _chain, _var in (("base", "BLACKWALL_BASE_RPC_URL"),
                             ("ethereum", "BLACKWALL_RWA_RPC_URL")):
            _u = os.environ.get(_var, "")
            if _u:
                _sim_rpcs[_chain] = _u
        if _sim_rpcs:
            from settlement_sim import SettlementSimSource
            settlement_sim_source = SettlementSimSource(rpc_by_chain=_sim_rpcs)
            from auth_sim import AuthorizationSimSource
            auth_sim_source = AuthorizationSimSource(rpc_by_chain=_sim_rpcs)
            sys.stdout.write("blackwall: settlement simulation ON for %s (pre-signature "
                             "revert check; HOLD-only, fail-open)\n"
                             % ",".join(sorted(_sim_rpcs)))
            sys.stdout.flush()

    # OPT-IN (BLACKWALL_ANCHOR=1): anchor every verdict's digest to Traceipt for a
    # tamper-evident audit trail. Non-blocking + fail-open; costs ~0.002 USDC/verdict
    # and needs a funded SIGNER_PRIVATE_KEY (else it 402s and fails open). OFF by
    # default -- see docs/TRACEIPT_INTEGRATION.md and the reliability caveat there.
    from verdict_anchor import from_env as _anchor_from_env
    anchor = _anchor_from_env(_env_flag)
    if anchor is not None:
        sys.stdout.write(
            "blackwall: verdict anchoring ON -> %s (opt-in audit trail; "
            "non-blocking, fail-open, ~0.002 USDC/verdict%s)\n"
            % (anchor.base_url,
               "" if anchor.pay else "; NO signer -> will 402/fail-open unsigned"))
        sys.stdout.flush()

    server = BlackwallServer(host=args.host, port=args.port, ledger=led,
                             billing=billing, reputation_source=reputation_source,
                             readiness_source=readiness_source,
                             hold_above=hold_above,
                             graph_source=graph_source,
                             velocity_source=velocity_source,
                             verdict_anchor=anchor,
                             category_index=category_index,
                             divergence_index=divergence_index,
                             rate_limiter=rate_limiter,
                             enrichment_source=enrichment_source,
                             rwa_source=rwa_source,
                             stock_registry=stock_registry,
                             pyth_source=pyth_source,
                             rwa_ledger=rwa_ledger,
                             balance_reader=balance_reader,
                             backing_index=backing_index,
                             dex_source=dex_source,
                             holder_source=holder_source,
                             aave_source=aave_source,
                             issuer_trust_source=issuer_trust_source,
                             settlement_sim_source=settlement_sim_source,
                             auth_sim_source=auth_sim_source,
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
