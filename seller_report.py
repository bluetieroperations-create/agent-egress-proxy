#!/usr/bin/env python3
"""
seller_report.py -- "why agents are not paying you", per seller.

Every gate in this repo serves the BUYER: an agent about to pay, asking whether
it should. This is the first thing here that serves the SELLER, and it needs no
new data -- one payee address or host, the committed corpus, and one live probe
answer nine questions a seller cannot answer about themselves.

THE HEADLINE, and the reason this is worth anything: we run the seller through
the REAL engine (`decide_payment`), not a description of it. A seller learns the
verdict a buyer's agent actually gets today and the reasons behind it -- not our
opinion of their endpoint.

FOUR RULES, each learned from a specific mistake in this repo:

 1. NEVER REPORT OUR OWN STALE ARTIFACT AS THE SELLER'S BUG. `data/liveness.json`
    carries a `class` per host from the 2026-08-18 survey, taken BEFORE the
    `payment-required` carrier was implemented. It still says 86 hosts serve an
    unreadable 402; implementing that one carrier moved scoreable hosts from
    73/195 to 153/195, and the 2026-09-05 census puts non-answering hosts at 18
    of 195. A report built on that field would tell 68 sellers their challenge is
    unparseable because OUR parser was incomplete. So parseability is derived
    from a LIVE probe or reported as NOT CHECKED. This module never reads that
    field, and `test_seller_report` asserts it.

 2. SILENT IS NOT BROKEN. A host that does not answer and a healthy host produce
    the same absence of findings. `payee_syntax` learned this over three probes
    of the same seller. So a report leads with whether we reached them, and an
    unreachable host yields `unknown`, never a defect.

 3. A REPORT MUST NEVER BECOME AN INPUT TO THE GATE THAT SCORES THE SAME SELLER.
    This module imports the engine; the engine must never import this. Otherwise
    a seller could improve their verdict by influencing their own report, and the
    diagnostic becomes a laundering step. Structurally tested.

 4. EVERYTHING ECHOED IS UNTRUSTED. Host, payee, category and resource strings
    are all authored by the seller being reported on, and this report is rendered
    in a terminal and mailed to a third party. FIFTH instance of that class here
    (`payee_syntax`'s hint, `approvals`' `decided_by`, `billing_preflight`'s
    facilitator kinds, and `secret_scan`'s whole reason for existing).

Descriptive only: nothing here gates, scores, or changes a verdict.

CLI:
  python seller_report.py <payee-address|host> [--offline] [--json out.json]
"""

import json
import os
import sys
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

_HERE = os.path.dirname(os.path.abspath(__file__))
DIRECTORY_PATH = os.path.join(_HERE, "data", "directory.json")
COVERAGE_PATH = os.path.join(_HERE, "data", "asset_coverage.json")
CATEGORY_INDEX_PATH = os.path.join(_HERE, "data", "category_index.json")

# Severity of a finding, most severe first. `unknown` is deliberately NOT a
# severity: "we could not tell" is a statement about our evidence, not about the
# seller, and rendering it as a defect is rule 2 above.
BLOCKER = "blocker"   # an agent CANNOT pay you
WARNING = "warning"   # an agent's engine will hold or refuse you
INFO = "info"         # true and worth knowing; not a defect
UNKNOWN = "unknown"   # not checked, or not determinable from what we have

_RANK = {UNKNOWN: 0, INFO: 1, WARNING: 2, BLOCKER: 3}

MAX_ECHO = 120


def _safe(value, limit=MAX_ECHO):
    """Render seller-authored text so it cannot forge a line in this report."""
    text = value if isinstance(value, str) else str(value)
    if len(text) > limit:
        text = text[:limit] + "..."
    return repr(text)[1:-1]


def _dec(value):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() and d >= 0 else None


def finding(code, severity, title, detail, evidence=None, **extra):
    """One line of the report. `evidence` says WHERE it came from and WHEN.

    Required, not optional: this report tells a business their endpoint is
    broken. A claim whose source the reader cannot check is not worth making,
    and a claim from a dated artifact must carry the date so a stale input is
    visible as staleness rather than passing as a fact.
    """
    row = {"code": code, "severity": severity, "title": title, "detail": detail,
           "evidence": evidence or "not stated"}
    row.update(extra)
    return row


def worst(findings):
    return max((f["severity"] for f in findings),
               key=lambda s: _RANK.get(s, 0), default=UNKNOWN)


# ---------------------------------------------------------------------------
# Finding the seller in the corpus (pure)
# ---------------------------------------------------------------------------
def host_of(url):
    """Bare hostname of a resource URL, lowercased; None when unparseable."""
    from urllib.parse import urlsplit
    try:
        return (urlsplit(str(url)).hostname or "").lower() or None
    except Exception:
        return None


def hosts_of(row):
    seen = []
    for resource in (row.get("resources") or []):
        host = host_of(resource)
        if host and host not in seen:
            seen.append(host)
    return seen


def resources_for_key(row, key):
    """The resources a report keyed on `key` may probe.

    A payee address covers the whole row, so every resource is fair game. A HOST
    does not: 58 of 266 corpus payees advertise more than one host, and
    `probe_resources` returns the FIRST ANSWERING resource, so a report asked
    about one host would otherwise be written from a SIBLING host's answer. On
    this corpus that is not a subtlety -- the payee behind `payanagent.com` also
    carries `api.anchor-x402.com`, and the one behind `x402.ottoai.services`
    also carries `api.aidress.ai`. Those are different businesses sharing a
    payment address, so a blocker found on one would be reported to the other as
    "an agent cannot pay you today".

    Third instance of the same cross-attribution class here (the
    two-businesses-in-one-report bug in `select_subject`, then the ledger
    recording against `hosts[0]`), this time at host granularity inside one row.
    NO FALLBACK to the siblings: a host whose own resources all fail IS
    unreachable, and answering with a neighbour's success would be the bug.
    """
    resources = list(row.get("resources") or [])
    needle = str(key or "").strip().lower()
    # A payee address needs no special case: it never equals a hostname, so
    # `scoped` is empty and the fallback hands back the whole row. An explicit
    # address branch was written first and removed -- it could not be killed by
    # any mutation, which is the tell that it was doing nothing.
    scoped = [r for r in resources if needle and host_of(r) == needle]
    return scoped or resources


def find_rows(rows, key):
    """Corpus rows matching `key`, which may be a payee address OR a host.

    Address match is case-insensitive because a live 402 returns EIP-55 while the
    crawl stores lowercase -- the join that silently missed 64 of 69 endpoints in
    `advertised_prices`. Host match is EXACT, not a substring: a substring would
    make "api.foo.com" match "api.foo.com.evil.net" and put one seller's findings
    in another seller's report.
    """
    if not key:
        return []
    needle = str(key).strip().lower()
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("payee") or "").strip().lower() == needle:
            out.append(row)
        elif needle in hosts_of(row):
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Live probe: reach + parseability (rule 1)
# ---------------------------------------------------------------------------
def assess_reach(probe, history=None):
    """Did the seller's endpoint answer us, and what happened the other times?

    Leads every report. A silent host and a healthy one produce the same absence
    of downstream findings, so saying which one we saw is what makes the rest of
    the report readable (rule 2).

    `history` is a `reachability_ledger` summary, and it is what stops a single
    timeout reading the same as a three-week silence. Without it this project
    probed one host four times and told a different story every time, because
    each probe overwrote the last -- so the honest answer to "is it down?" was
    always "we cannot tell from one look", and now it does not have to be.

    Still never a defect we ASSERT. Even a long silent run is reported as our
    observations, because today's six timeouts came through a proxy while a
    direct TLS handshake to the same host succeeded on the first try, and from
    here those two are indistinguishable.
    """
    import reachability_ledger as RL

    state = (history or {}).get("state")
    context = ""
    if history and state and state != "unobserved":
        context = " " + RL.describe(history)

    if probe is None:
        return finding("reach", UNKNOWN, "Reachability not checked",
                       "This report was produced without contacting your "
                       "endpoint." + context,
                       evidence="no probe performed", history=state)
    if probe.get("error"):
        title = "We could not reach your endpoint"
        detail = ("We got no usable answer (%s). This is a statement about our "
                  "probe, not a defect we observed in your service -- a host "
                  "that is merely quiet and a healthy one look identical from "
                  "here." % _safe(probe.get("error"), 80))
        if state == "silent_run":
            title = "We have not reached your endpoint in some time"
        elif state == "flapping":
            title = "Your endpoint answers intermittently"
        return finding("reach", UNKNOWN, title, detail + context,
                       evidence="live probe of %s" % _safe(probe.get("url"), 100),
                       history=state)
    title = "Your endpoint answered"
    if state == "flapping":
        title = "Your endpoint answered, but not always"
    return finding("reach", INFO, title,
                   ("HTTP %s." % _safe(probe.get("status"), 12)) + context,
                   evidence="live probe of %s" % _safe(probe.get("url"), 100),
                   status=probe.get("status"), history=state)


def assess_parseability(probe):
    """Can a buyer's agent PARSE your price?

    Derived LIVE, never from `data/liveness.json`'s `class` field -- see rule 1.
    An agent scores from the challenge's `accepts[]`; a 402 whose requirements
    it cannot read is unpayable however correct the rest of the service is.
    """
    if probe is None or probe.get("error"):
        return finding("challenge", UNKNOWN, "Payment challenge not read",
                       "We did not get a response to parse.",
                       evidence="no usable probe response")
    from x402_challenge import parse_challenge

    accepts, carrier = parse_challenge(probe.get("body") or "",
                                       probe.get("headers") or {})
    if not accepts:
        return finding("challenge", BLOCKER,
                       "An agent cannot read your payment requirements",
                       "Your endpoint answered, but we found no `accepts[]` in "
                       "the body, in `WWW-Authenticate`, or in a "
                       "`payment-required` header. An x402 client prices and "
                       "signs from that list, so there is nothing for it to pay.",
                       evidence="live parse of %s" % _safe(probe.get("url"), 100))
    return finding("challenge", INFO, "Your payment requirements are readable",
                   "Parsed %d option(s) from the %s carrier."
                   % (len(accepts), _safe(carrier, 40)),
                   evidence="live parse of %s" % _safe(probe.get("url"), 100),
                   carrier=carrier, options=len(accepts))


# ---------------------------------------------------------------------------
# Identifier checks (pure, from committed artifacts)
# ---------------------------------------------------------------------------
def assess_payee_identifier(payee):
    """Is the address you advertise an address a payment can arrive at?

    Not hypothetical: `asset_coverage` found a live seller advertising a Solana
    `payTo` with `FACILITATOR_URL=https://...` glued onto it -- a missing newline
    in a `.env`. Money sent there is gone, and the engine could not tell it from
    a clean payee (measured: byte-identical verdicts).
    """
    from payee_syntax import assess_payee

    signal = assess_payee(payee)
    grade = signal.get("grade")
    if grade == "malformed":
        return finding("payee", BLOCKER, "Your payee address cannot receive money",
                       "It contains characters no address can hold on any chain "
                       "(%s). The usual cause is a missing newline in a `.env`, "
                       "which glues the next variable onto the address."
                       % _safe(signal.get("hint"), 80),
                       evidence="syntax check of the advertised payTo", grade=grade)
    if grade == "invalid_hex":
        return finding("payee", WARNING, "Your payee address looks malformed",
                       "It starts `0x` but is not a valid 20-byte EVM address.",
                       evidence="syntax check of the advertised payTo", grade=grade)
    return finding("payee", INFO, "Your payee address is well-formed",
                   "No impossible content.",
                   evidence="syntax check of the advertised payTo", grade=grade)


def assess_identifiers(coverage, hosts):
    """Broken asset identifiers and unscalable amounts attributed to your hosts.

    Two different defects with the same consequence. A malformed asset id (we
    found one truncated to 39 hex characters, still live) cannot be resolved at
    all. An asset we simply do not know the decimals for means a buyer's engine
    cannot convert your atomic amount into a price, so its spending cap and
    price-anomaly checks are switched off for every payment to you -- which
    reads as caution, not as approval.
    """
    hostset = {h for h in (hosts or []) if h}
    dated = "data/asset_coverage.json, generated %s" % _safe(
        (coverage or {}).get("generated_at") or "undated", 40)
    out = []

    mine = [m for m in (coverage or {}).get("malformed") or []
            if _mentions_host(m, hostset)]
    if mine:
        out.append(finding(
            "asset_id", BLOCKER, "You advertise an unusable asset identifier",
            "%d identifier(s) on your host(s) cannot be resolved on any chain: "
            "%s. A client cannot tell which token you want."
            % (len(mine), _safe("; ".join(_describe(m) for m in mine[:3]), 200)),
            evidence=dated, rows=len(mine)))

    unresolved = [u for u in (coverage or {}).get("unresolved") or []
                  if _mentions_host(u, hostset)]
    if unresolved:
        out.append(finding(
            "asset_scale", WARNING, "We cannot scale the amounts you quote",
            "%d (network, asset) pair(s) you use are not in our decimals table, "
            "so a buyer's engine cannot turn your atomic amount into a price. "
            "Its spending cap and price checks are inert for payments to you."
            % len(unresolved), evidence=dated, rows=len(unresolved)))

    if not out:
        # ABSENT ARTIFACT IS NOT A CLEAN BILL OF HEALTH. `load_json` fails soft to
        # {}, so a missing census produced "your identifiers resolve" -- and the
        # deploy image did not ship the file, which meant the host that carries
        # the one genuinely broken identifier in the corpus would have been told
        # it was fine. Same rule as reachability: missing evidence is a statement
        # about us, never about them.
        if not (coverage or {}).get("generated_at"):
            out.append(finding(
                "asset_id", UNKNOWN, "Asset identifiers not checked",
                "The ecosystem census was not available to this report.",
                evidence="no asset-coverage artifact loaded"))
        else:
            out.append(finding("asset_id", INFO, "Your asset identifiers resolve",
                               "Nothing on your host(s) is malformed or "
                               "unscalable.", evidence=dated))
    return out


def _mentions_host(entry, hostset):
    """True when a coverage row is attributable to one of these hosts.

    Conservative on purpose: coverage rows carry their originating host under
    more than one spelling across versions, so this looks at every string in the
    row rather than a fixed key. An entry we cannot attribute is DROPPED, never
    attributed by default -- a false attribution puts another seller's defect in
    this seller's report.
    """
    if not hostset:
        return False
    for value in _strings(entry):
        host = host_of(value) or value.strip().lower()
        if host in hostset:
            return True
    return False


def _strings(entry, depth=0):
    if depth > 4:
        return
    if isinstance(entry, str):
        yield entry
    elif isinstance(entry, dict):
        for v in entry.values():
            for s in _strings(v, depth + 1):
                yield s
    elif isinstance(entry, (list, tuple)):
        for v in entry:
            for s in _strings(v, depth + 1):
                yield s


def _describe(entry):
    if isinstance(entry, dict):
        for key in ("asset", "value", "payee", "identifier"):
            if entry.get(key):
                return str(entry[key])
    return str(entry)


# ---------------------------------------------------------------------------
# The headline: what verdict does a buyer's engine actually return? (pure)
# ---------------------------------------------------------------------------
def record_from_row(row):
    """The reputation record the engine would hold for this payee.

    Built from the SAME committed corpus a deployed Blackwall boots warm with,
    so the verdict below is the one a real buyer gets, not a simulation of it.
    """
    return {
        "settlement_count": row.get("settlement_count") or 0,
        "distinct_payers": row.get("distinct_payers"),
        "sanctioned": bool(row.get("sanctioned")),
        "dispute_rate": 0.0,
        "advertised_min_price": row.get("min_price"),
        "advertised_max_price": row.get("max_price"),
    }


def representative_amount(row):
    """The amount to score: the seller's own CHEAPEST advertised option.

    The corpus stores the min/max hull of a price list, not the list, so any
    single number is a choice. The cheapest is the honest one for a diagnostic:
    it is the option most buyers meet first, and it is the one least likely to
    trip an amount threshold -- so a HOLD at this amount is a HOLD that is about
    the seller's reputation rather than about the size of the payment.
    """
    lo = _dec(row.get("min_price"))
    return lo if lo is not None and lo > 0 else Decimal("0.01")


def assess_verdict(row, cross_signal=None, decide=None):
    """Run the seller through the REAL engine and report what a buyer gets."""
    if decide is None:
        from blackwall import decide_payment as decide

    amount = representative_amount(row)
    verdict = decide(str(amount), record_from_row(row), [],
                     counterparty=row.get("payee"),
                     payer_graph_signal=cross_signal)
    reasons = [_safe(r, 160) for r in (verdict.get("reasons") or [])]
    label = verdict.get("verdict")
    severity = {"GO": INFO, "HOLD": WARNING}.get(label, BLOCKER)
    return finding(
        "verdict", severity,
        "A buyer's agent gets %s on you, on reputation and price alone"
        % _safe(label, 12),
        "Scored at your own cheapest advertised option (%s). %s"
        % (amount, "; ".join(reasons) if reasons else "No reasons recorded."),
        # SCOPE, stated because the first live run produced a report reading
        # "you advertise an unusable asset identifier" and "a buyer's agent gets
        # GO" side by side. Both were true and together they read as a
        # contradiction. This verdict is computed from settlement history and
        # price only -- no asset, no chain, no settlement simulation -- so it
        # cannot see the broken identifier the line above it reports. Saying so
        # is the difference between a precise finding and a misleading all-clear.
        evidence="blackwall.decide_payment over data/directory.json -- history "
                 "and price only; the identifier and reachability findings above "
                 "are NOT inputs to it",
        verdict=label, amount=str(amount), reasons=reasons,
        scope="reputation_and_price")


# MEASURED across all 266 payees carrying a graph signal, 2026-09-06, from the
# committed seed corpus. Reproduce with:
#   gunzip -c data/reputation_seed.db.gz > /tmp/rep.db && python3 - <<'EOF'
#   import json
#   from reputation_store import ReputationStore
#   from payer_reputation import PayerReputationSource
#   src = PayerReputationSource.from_store(ReputationStore("/tmp/rep.db"))
#   e = sorted((src.cross_signal(r["payee"]) or {}).get("established_payers", 0)
#              for r in json.load(open("data/directory.json"))
#              if src.cross_signal(r["payee"]))
#   print(len(e), sum(1 for x in e if x == 0), e[len(e) // 2])
#   EOF
# Not asserted by a unit test: building the graph takes minutes, and a slow test
# is a test that gets skipped. It is dated instead, so a stale figure is visible
# as staleness rather than passing as a fact.
CORPUS_PAYEES_WITH_GRAPH = 266
CORPUS_ZERO_CORROBORATION = 11        # 4.1% -- the bottom of the market
CORPUS_MEDIAN_ESTABLISHED = 10
CORPUS_MEASURED = "2026-09-06"


def assess_demand_authenticity(row, cross_signal, store_error=None):
    """Are your payers real, or are they only paying you?

    THE FINDING A SELLER CANNOT GET ANYWHERE ELSE, and the reason is structural
    rather than a matter of sample size: the evidence lives in the OTHER payees.
    A receipt-window analysis can see that 200 addresses paid you; only a
    CROSS-PAYEE graph can see that not one of them ever paid anybody else.

    THREE TIERS, drawn on measurement rather than taste, because this is the
    finding most likely to insult a legitimate business:

      * the engine's own flags (`sybil_ring` / `captive_sybil`) fire on 4 of 266
        payees (1.5%). Those thresholds are calibrated and graduated, so they get
        the strongest wording.
      * zero corroborated payers is 11 of 266 (4.1%) while the MEDIAN endpoint
        has 10, so it is genuinely the bottom of the market and warrants a
        warning -- but a softer one, quoting the median so a seller can place
        themselves rather than just feel accused.
      * anything above zero is reported with the same median for context.

    An earlier revision rendered "0 of your payers also pay other known x402
    endpoints" under the heading "your payers are corroborated elsewhere",
    marked ok. It reached that branch because the engine's flags need a minimum
    payer count to fire at all, so a payee with ONE payer tripped neither -- and
    the report congratulated a seller on evidence it had just said was absent.
    """
    if cross_signal is None:
        if store_error:
            return finding("demand", UNKNOWN, "Demand authenticity not assessed",
                           "A payer graph was supplied but could not be read "
                           "(%s)." % _safe(store_error, 160),
                           evidence="payer graph failed to load")
        return finding("demand", UNKNOWN, "Demand authenticity not assessed",
                       "The cross-payee payer graph was not available for this "
                       "report.", evidence="no payer graph supplied")

    established = cross_signal.get("established_payers") or 0
    payers = cross_signal.get("distinct_payers")
    flags = [k for k in ("sybil_ring", "captive_sybil") if cross_signal.get(k)]
    context = ("The median x402 endpoint we track has %d such payers (measured "
               "across %d endpoints, %s)."
               % (CORPUS_MEDIAN_ESTABLISHED, CORPUS_PAYEES_WITH_GRAPH,
                  CORPUS_MEASURED))
    evidence = "cross-payee payer graph over the ingested corpus"

    if flags:
        return finding(
            "demand", WARNING, "Your payers do not appear anywhere else",
            "You show %s distinct payer(s) and %d of them also pay other known "
            "x402 endpoints (%s). %s A buyer's engine holds on this pattern. If "
            "your traffic is genuinely new it resolves itself as your payers "
            "spend elsewhere."
            % (payers, established, ", ".join(flags), context),
            evidence=evidence, established_payers=established, flags=flags)
    if established == 0:
        return finding(
            "demand", WARNING, "None of your payers pays anyone else we know",
            "Across your %s distinct payer(s), not one also pays another known "
            "x402 endpoint. %s Only %d of %d endpoints are in that position. "
            "This is what a buyer cannot check from your receipts alone, and it "
            "is equally consistent with a genuinely new audience -- but it is "
            "the bottom of the market today."
            % (payers, context, CORPUS_ZERO_CORROBORATION,
               CORPUS_PAYEES_WITH_GRAPH),
            evidence=evidence, established_payers=established)
    return finding(
        "demand", INFO, "Your payers are corroborated elsewhere",
        "%d of your %s payer(s) also pay other known x402 endpoints, which is "
        "the hard-to-fake half of a reputation. %s"
        % (established, payers, context),
        evidence=evidence, established_payers=established)


def assess_price_position(row, category_median=None):
    """Which of your price options will a buyer's engine hold?

    NOT "is your listing too expensive" -- that framing produced a false
    accusation on the first live run. Bitrefill's dearest advertised option is
    $1000 against a $0.25 commerce median, and the report called it "4000x your
    category" as though the listing were a gouge. It is a GIFT CARD. The price
    is correct; a $1000 purchase simply is a large payment.

    The hull hazard again, exactly as `advertised_prices.py` documents it:
    [min, max] is the HULL of a price list, not the list, and its top is
    routinely a legitimate outlier. Compounding it, the engine's category gate
    compares the AMOUNT BEING PAID, not the advertised maximum -- so a report
    that judges the listing is stricter than the engine and wrong about it.

    So this states the engine's actual consequence: above `median x
    CATEGORY_HOLD_RATIO` a payment gets held, and here is where your options sit
    relative to that line. A seller whose CHEAPEST option is already above it has
    something to fix; a seller with one expensive product does not.
    """
    from blackwall import CATEGORY_HOLD_RATIO

    category = row.get("category")
    lo, hi = _dec(row.get("min_price")), _dec(row.get("max_price"))
    median = _dec(category_median)
    if median is None or median <= 0 or lo is None:
        return finding("price", UNKNOWN, "Price position not assessed",
                       "No usable settled median for your category.",
                       evidence="category price index not supplied",
                       category=category)

    line = median * Decimal(str(CATEGORY_HOLD_RATIO))
    detail_tail = ("A buyer's engine holds any payment at or above %s (%.0fx the "
                   "settled median of %s for %s)."
                   % (line, CATEGORY_HOLD_RATIO, median, _safe(category, 40)))
    if lo >= line:
        return finding(
            "price", WARNING, "Even your cheapest option gets held on price",
            "Your cheapest advertised option is %s. %s Every payment to you "
            "crosses that line." % (lo, detail_tail),
            evidence="category settled median", category=category,
            hold_line=str(line))
    if hi is not None and hi >= line:
        return finding(
            "price", INFO, "Your dearest options get held on price",
            "Your options run %s to %s. %s Your cheaper options clear it; the "
            "dearest do not, which is ordinary for a catalogue that includes "
            "large purchases." % (lo, hi, detail_tail),
            evidence="category settled median", category=category,
            hold_line=str(line))
    return finding("price", INFO, "Your prices clear the category check",
                   "Your options run %s to %s. %s" % (lo, hi, detail_tail),
                   evidence="category settled median", category=category,
                   hold_line=str(line))


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def select_subject(matches):
    """THE ONE PLACE a report's subject payee is chosen.

    A host can carry several payees -- blockrun.ai carries three -- so "the
    seller at this host" is a CHOICE, and the busiest payee is the one a buyer
    is most likely to meet. It exists as a named function because the selection
    was briefly made in TWO places with DIFFERENT rules: `main` probed and built
    the payer graph from `matches[0]` while `build_report` reported on
    `max(settlement_count)`. Measured on blockrun.ai, that put 0x6e00...'s payer
    graph ("26 payers corroborated") into 0xe903...'s report ("1 distinct payer,
    possible wash-trading") -- two different businesses in one document, and the
    numbers contradicted each other on the page.

    So `build_report` now selects ONCE and does its own probing and lookups from
    that row; callers pass FUNCTIONS, not pre-resolved results, and cannot
    resolve them against a different payee.
    """
    return max(matches, key=lambda r: r.get("settlement_count") or 0)


def build_report(key, rows, coverage=None, probe_fn=None, cross_fn=None,
                 category_index=None, decide=None, source="seller_report"):
    matches = find_rows(rows, key)
    if not matches:
        return {"key": key, "found": False, "severity": UNKNOWN, "findings": [
            finding("corpus", UNKNOWN, "We have no record of you",
                    "Nothing in our crawl of the live x402 ecosystem advertises "
                    "this payee or host. That most often means an agent cannot "
                    "discover you at all -- but it is equally consistent with us "
                    "never having crawled you, so it is not a defect we observed.",
                    evidence="data/directory.json")]}

    row = select_subject(matches)
    hosts = hosts_of(row)
    payee = row.get("payee")

    probe = probe_fn(resources_for_key(row, key)) if probe_fn else None
    history = None
    # THE HOST WE ACTUALLY PROBED, not hosts[0]. 58 of 266 corpus payees
    # advertise more than one host, and `probe_resources` returns the FIRST
    # ANSWERING resource -- so on 24 of them the probe can land on a different
    # host than the first one listed. Recording that against hosts[0] writes
    # false evidence in BOTH directions: a silent host gets credited with a
    # sibling's success, and a host nobody tried gets charged with a failure.
    # That is the exact cross-attribution this ledger exists to prevent, and the
    # same class as the two-businesses-in-one-report bug above.
    probed_host = host_of((probe or {}).get("url")) if probe else None
    subject_host = probed_host or (hosts[0] if hosts else None)
    if subject_host:
        # Fail-soft in both directions: a report must never break because a log
        # file is unwritable, and never be blocked because one is unreadable.
        import reachability_ledger as RL
        try:
            if probe is not None:
                RL.observe(subject_host, probe, source=source)
            history = RL.summarize(RL.load(host=subject_host))
        except Exception:
            history = None
    cross, store_error = (None, None)
    if cross_fn:
        cross, store_error = cross_fn(payee)
    median = None
    if isinstance(category_index, dict):
        median = category_index.get(row.get("category"))

    findings = [assess_reach(probe, history), assess_parseability(probe),
                assess_payee_identifier(payee)]
    findings.extend(assess_identifiers(coverage, hosts))
    findings.append(assess_verdict(row, cross, decide=decide))
    findings.append(assess_demand_authenticity(row, cross,
                                               store_error=store_error))
    findings.append(assess_price_position(row, median))
    if len(matches) > 1:
        # A seller on a shared host must know WHICH payee this report is about,
        # or they will read someone else's numbers as their own.
        findings.append(finding(
            "shared_host", INFO, "Your host advertises more than one payee",
            "%d payees advertise on these host(s); this report is about the "
            "busiest (%s). Ask for a report by ADDRESS to see another."
            % (len(matches), _safe(payee, 60)),
            evidence="data/directory.json", payees=len(matches)))
    return {"key": key, "found": True, "payee": payee,
            "hosts": hosts, "category": row.get("category"),
            "settlements": row.get("settlement_count"),
            "distinct_payers": row.get("distinct_payers"),
            "severity": worst(findings), "findings": findings}


_MARK = {BLOCKER: "BLOCKER", WARNING: "WARNING", INFO: "ok     ",
         UNKNOWN: "unknown"}

_HEADLINE = {
    BLOCKER: "An agent cannot pay you today.",
    WARNING: "An agent can pay you, but a buyer's engine will not clear you.",
    INFO: "Nothing is blocking a payment to you.",
    UNKNOWN: "We could not establish enough to say.",
}


def format_report(report):
    lines = ["x402 seller diagnostic -- %s" % _safe(report.get("key"), 80), ""]
    if report.get("found"):
        lines.append("payee %s   hosts %s"
                     % (_safe(report.get("payee"), 60),
                        _safe(", ".join(report.get("hosts") or []) or "none", 100)))
        lines.append("%s settlements from %s distinct payers, category %s"
                     % (report.get("settlements"), report.get("distinct_payers"),
                        _safe(report.get("category") or "unclassified", 40)))
        lines.append("")
    lines.append(_HEADLINE.get(report["severity"], ""))
    lines.append("")
    for f in report["findings"]:
        lines.append("  [%s] %s" % (_MARK.get(f["severity"], "?"), f["title"]))
        lines.append("           %s" % f["detail"])
        lines.append("           evidence: %s" % f["evidence"])
    return "\n".join(lines)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Probe safety: where the URL we fetch actually comes from
# ---------------------------------------------------------------------------
# A resource URL is NOT our data. `discovery_crawl` harvests it from a stranger's
# own x402 advertisement, so it is attacker-authored content that we store and
# later FETCH. Today's corpus is clean -- measured 2026-09-06: 3827 resources,
# all https, no IP literals, no private hosts -- but it is refreshed by crawling
# third parties, and nothing stops the next crawl from picking up
# `https://169.254.169.254/x402` or a hostname that resolves there. The report
# then echoes the status and the error string, which is a usable oracle for
# mapping whatever network this process runs in.
#
# So the probe validates its own target. Latent rather than live is the right
# time to fix this: the fix is cheap, and the corpus refreshes on a schedule.
#
# RESIDUAL GAP, stated rather than implied: this resolves and then lets urllib
# resolve again, so a name that answers differently on the second lookup (DNS
# rebinding) is not covered. Closing that needs connecting to the pinned address
# with the Host header preserved, which is a bigger change than this warrants
# while every corpus host is a public CDN name.
ALLOWED_SCHEMES = ("https", "http")


def _resolve(hostname):
    import socket
    return [info[4][0] for info in socket.getaddrinfo(hostname, None)]


def pinned_address(url, resolve=None):
    """(ok, reason, ip) -- validate the URL AND return the address to connect to.

    Returning the address is what closes the rebinding window. `safe_probe_url`
    checked the name and then let urllib resolve it a SECOND time, so a name that
    answered public on the check and private on the connect walked straight
    through -- the classic DNS-rebinding TOCTOU, and a live one here because the
    names come from strangers' advertisements that we store and later fetch.
    Handing the caller the exact address we approved removes the second lookup.

    Refuses anything whose host resolves to an address that is not on the public
    internet -- loopback, private, link-local (the cloud metadata range),
    reserved or multicast -- plus non-HTTP schemes and embedded credentials.
    """
    import ipaddress
    from urllib.parse import urlsplit

    if resolve is None:
        resolve = _resolve
    try:
        parts = urlsplit(str(url))
    except Exception:
        return False, "unparseable url", None
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return False, "scheme %s is not http(s)" % _safe(parts.scheme, 20), None
    # `user@host` forms let a crafted URL disagree with what a human reads.
    if "@" in (parts.netloc or ""):
        return False, "url carries embedded credentials", None
    host = parts.hostname
    if not host:
        return False, "url has no host", None
    try:
        addresses = resolve(host)
    except Exception as e:
        return False, "host does not resolve (%s)" % _safe(e, 60), None
    if not addresses:
        return False, "host does not resolve", None
    chosen = None
    for address in addresses:
        try:
            ip = ipaddress.ip_address(str(address).split("%")[0])
        except ValueError:
            return False, "unreadable address for host", None
        # EVERY resolved address must be public: a name answering with one
        # public and one private address would otherwise pass and then connect
        # to whichever the OS picked.
        if not ip.is_global or ip.is_multicast:
            return False, "host resolves to a non-public address", None
        if chosen is None:
            chosen = str(ip)
    return True, "ok", chosen


def safe_probe_url(url, resolve=None):
    """(ok, reason). The boolean half of `pinned_address`, kept for callers and
    tests that only ask whether a URL may be fetched at all."""
    ok, reason, _ = pinned_address(url, resolve=resolve)
    return ok, reason



def _fetch_pinned(url, ip, timeout=12.0):
    """GET `url` by connecting to `ip`, the address we already validated.

    The name is still what gets presented -- SNI, certificate verification and
    the `Host` header all use the hostname -- so this is not certificate
    pinning and it does not weaken TLS. The only thing pinned is WHICH ADDRESS
    the socket goes to, which is precisely the value a second DNS lookup could
    have changed underneath us.

    A 402 is the SUCCESS case here; every status is returned rather than raised.
    """
    import http.client
    import ssl
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    secure = parts.scheme.lower() == "https"
    port = parts.port or (443 if secure else 80)
    path = urlunsplit(("", "", parts.path or "/", parts.query, "")) or "/"

    if secure:
        context = ssl.create_default_context()
        conn = http.client.HTTPSConnection(ip, port, timeout=timeout,
                                           context=context)
        # SNI + hostname verification must use the NAME, not the address we dial.
        conn._pinned_server_hostname = parts.hostname
        _patch_sni(conn, parts.hostname)
    else:
        conn = http.client.HTTPConnection(ip, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Host": parts.netloc,
                                           "accept": "application/json"})
        response = conn.getresponse()
        return response.status, response.read(1 << 20), dict(response.getheaders())
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _patch_sni(conn, hostname):
    """Make an HTTPSConnection dialling an IP present `hostname` for TLS."""
    import socket as _socket

    def connect():
        sock = _socket.create_connection((conn.host, conn.port), conn.timeout)
        conn.sock = conn._context.wrap_socket(sock, server_hostname=hostname)

    conn.connect = connect


def probe_endpoint(url, timeout=12.0, fetch=None):
    """One GET, returning body+headers whatever the status.

    A 402 is the SUCCESS case here -- it is the document we came to read -- so
    the HTTPError branch is the main path, not the error path.
    """
    if fetch is not None:
        return fetch(url)
    ok, reason, ip = pinned_address(url)
    if not ok:
        # Not an error to report as the seller's: it is us declining to fetch a
        # URL we harvested. Surfaced as a probe error so it reads as "we did not
        # look", which is exactly what happened.
        return {"url": url, "error": "not probed: %s" % reason}
    try:
        status, body, headers = _fetch_pinned(url, ip, timeout)
        return {"url": url, "status": status, "body": body, "headers": headers}
    except Exception as e:
        return {"url": url, "error": "%s: %s" % (type(e).__name__, e)}


MAX_PROBES = 3


def probe_resources(resources, fetch=None, limit=MAX_PROBES):
    """Probe up to `limit` of a seller's resources; the first ANSWER wins."""
    first = None
    for resource in list(resources or [])[:limit]:
        result = probe_endpoint(resource, fetch=fetch)
        if not result.get("error"):
            return result
        if first is None:
            first = result
    return first


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(description="Why agents are not paying you.")
    p.add_argument("key", help="payee address or host")
    p.add_argument("--offline", action="store_true",
                   help="skip the live probe (reach + parseability -> unknown)")
    p.add_argument("--store", help="reputation store, for the demand-authenticity "
                                   "finding (the cross-payee payer graph)")
    p.add_argument("--json", metavar="PATH", help="also write the report as JSON")
    args = p.parse_args(argv)

    rows = load_json(DIRECTORY_PATH, [])
    coverage = load_json(COVERAGE_PATH, {})
    index = load_json(CATEGORY_INDEX_PATH, {})

    def probe_fn(resources):
        # Try SEVERAL resources before concluding a host is unreachable. A
        # seller with eight endpoints and one retired path is not unreachable,
        # and reporting them as such is asserting a defect we did not observe.
        return None if args.offline else probe_resources(resources)

    def cross_fn(payee):
        """(signal, error). Fail-soft but LOUD -- see the note below."""
        if not args.store:
            return None, None
        try:
            # `from_store`, NOT the constructor: PayerReputationSource takes
            # EDGES. Passing the store raised a TypeError that the fail-soft
            # here turned into a benign-looking "not assessed", so the one
            # finding a seller cannot get anywhere else was unreachable through
            # the CLI while every test passed. The wired-and-inert pattern
            # again; the tests now drive this exact path against a real store.
            from payer_reputation import PayerReputationSource
            from reputation_store import ReputationStore
            source = PayerReputationSource.from_store(ReputationStore(args.store))
            return source.cross_signal(payee), None
        except Exception as e:
            detail = "%s: %s" % (type(e).__name__, e)
            sys.stderr.write("seller_report: payer graph unavailable (%s)\n"
                             % _safe(detail, 160))
            return None, detail

    report = build_report(args.key, rows, coverage=coverage, probe_fn=probe_fn,
                          cross_fn=cross_fn, category_index=index)
    sys.stdout.write(format_report(report) + "\n")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, sort_keys=True)
    return {BLOCKER: 2, WARNING: 1}.get(report["severity"], 0)


if __name__ == "__main__":
    sys.exit(main())
