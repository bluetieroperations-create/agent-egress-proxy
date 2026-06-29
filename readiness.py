"""
readiness.py -- fold a third-party ENDPOINT-readiness signal into the verdict.

Blackwall scores a *counterparty address* (payment behavior). Ontario Protocol
scores an *endpoint URL* (is the service set up, discoverable, within budget) and
exposes it FREE at POST /api/agent/can-pay. Different identifiers, different
questions -- so we use Ontario's grade as a complementary INPUT, not a substitute.

Design rules (the important part):

  1. FAIL OPEN. Readiness is enrichment, not safety. If Ontario is unreachable,
     slow, or returns junk, we return None ("unknown") and the verdict proceeds on
     Blackwall's own signals. A third-party outage must never break the core
     verdict. (Contrast sanctions.py, which fails CLOSED -- that IS safety.)
  2. CONSERVATIVE-ONLY. Readiness may escalate GO -> HOLD when the endpoint is
     'needs_work'. It must NEVER upgrade a HOLD/STOP to GO. Monotonic toward
     caution.
  3. UNTRUSTED INPUT. The response is external and attacker-influenceable; every
     field is type-checked and nothing raises.

Stdlib only.
"""
import json
import urllib.request

# Ontario's verdict (allow/review/deny) maps onto readiness grades when an
# explicit grade is absent.
_DECISION_TO_GRADE = {"allow": "ready", "review": "close", "deny": "needs_work"}
_GRADES = ("ready", "close", "needs_work")


def normalize_readiness(resp):
    """Parse an Ontario can-pay / x402-readiness response (UNTRUSTED) into a
    normalized signal dict, or None when nothing usable is present.

    Accepts either shape:
      can-pay:   {"decision": "...", "report": {"grade": "...", "readiness_score": N}}
      readiness: {"grade": "...", "readiness_score": N}

    Returns {"grade", "score", "decision", "source"} or None. Never raises."""
    if not isinstance(resp, dict):
        return None
    report = resp.get("report")
    if not isinstance(report, dict):
        report = resp  # readiness-report shape carries grade at top level
    grade = report.get("grade") if isinstance(report.get("grade"), str) else None
    if grade not in _GRADES:
        # Fall back to the can-pay decision if there's no explicit grade.
        decision = resp.get("decision")
        grade = _DECISION_TO_GRADE.get(decision) if isinstance(decision, str) else None
    if grade not in _GRADES:
        return None
    score = report.get("readiness_score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        score = None
    decision = resp.get("decision")
    return {
        "grade": grade,
        "score": score,
        "decision": decision if isinstance(decision, str) else None,
        "source": "ontario",
    }


def apply_readiness(verdict, readiness):
    """Fold a normalized readiness signal into a verdict dict.

    CONSERVATIVE-ONLY: escalates GO -> HOLD when the endpoint grade is
    'needs_work'; never upgrades a HOLD/STOP to GO; a None/empty signal is a
    no-op. Returns a NEW dict (does not mutate the input).

    The endpoint readiness is reported under signals.endpoint_readiness and as a
    reason, kept clearly distinct from the address-based reputation signals."""
    if not readiness or not isinstance(readiness, dict):
        return verdict
    grade = readiness.get("grade")
    if grade not in _GRADES:
        return verdict

    v = dict(verdict)
    v["signals"] = dict(v.get("signals") or {})
    v["signals"]["endpoint_readiness"] = grade
    reasons = list(v.get("reasons") or [])

    if grade == "needs_work":
        reasons.append(
            "endpoint readiness is 'needs_work' (Ontario) -- service poorly "
            "configured or undiscoverable")
        if v.get("verdict") == "GO":
            v["verdict"] = "HOLD"
            reasons.append("escalated GO->HOLD on weak endpoint readiness")
    elif grade == "ready":
        reasons.append("endpoint readiness is 'ready' (Ontario)")
    else:  # close
        reasons.append("endpoint readiness is 'close' (Ontario)")

    v["reasons"] = reasons
    return v


class OntarioReadinessSource:
    """Query Ontario's FREE pre-payment readiness for an endpoint URL.

    Fail-open: any error/timeout/malformed response -> check() returns None.
    `transport` is an injection seam for tests: a callable(url, body) -> dict.
    """

    def __init__(self, base_url, timeout=2.5, verify_live=False, transport=None):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        self.verify_live = verify_live
        self._transport = transport

    def check(self, endpoint_url):
        """Return a normalized readiness signal for `endpoint_url`, or None.

        None means "unknown" -- the caller proceeds without the signal."""
        if not endpoint_url or not isinstance(endpoint_url, str):
            return None
        body = {"endpoint": endpoint_url, "verify_live": bool(self.verify_live)}
        try:
            resp = self._post("/api/agent/can-pay", body)
        except Exception:
            return None  # FAIL OPEN -- never let Ontario break our verdict
        return normalize_readiness(resp)

    def _post(self, path, body):
        if self._transport is not None:
            return self._transport(self.base_url + path, body)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data,
            headers={"content-type": "application/json",
                     "accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())
