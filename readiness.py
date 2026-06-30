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


# ===========================================================================
# Self-owned readiness -- replicate the OBSERVABLE part of an endpoint-readiness
# check from public signals. No competitor dependency, no query-stream leak.
# Weights are MODELED ON Ontario's public x402-trust weights; the implementation
# is ours. Score is normalized to 0..100 over the signals we can observe.
# ===========================================================================
_READINESS_WEIGHTS = {
    "payment_challenge": 20,    # endpoint returns HTTP 402 with an x402 challenge
    "manifest_present": 15,     # /.well-known/x402 exists
    "manifest_well_formed": 15, # ...and parses with x402 fields
    "https": 10,                # endpoint is https
    "endpoint_reachable": 10,   # endpoint responds at all
    "openapi": 10,              # an OpenAPI schema is published
    "homepage_reachable": 10,   # the base origin responds
    "robots": 3,                # robots.txt present
    "schema_org": 2,            # homepage advertises schema.org metadata
}
_READINESS_MAX_BODY = 65536


def score_readiness(observed):
    """Pure: map observed endpoint signals -> {grade, score, source, signals}.

    `observed` is {signal_name: bool}; unknown/absent signals count as not
    satisfied. Score is 100 * (satisfied weight / total weight). Grade
    thresholds are OURS (documented), not claimed to match Ontario's exactly:
    >=80 ready, >=50 close, else needs_work. Output shape matches
    normalize_readiness() so it folds through apply_readiness() unchanged."""
    total = sum(_READINESS_WEIGHTS.values())
    got = sum(w for k, w in _READINESS_WEIGHTS.items() if observed.get(k))
    score = round(100 * got / total) if total else 0
    grade = "ready" if score >= 80 else ("close" if score >= 50 else "needs_work")
    return {
        "grade": grade,
        "score": score,
        "source": "local",
        "signals": {k: bool(observed.get(k)) for k in _READINESS_WEIGHTS},
    }


def _looks_x402(body):
    """True if a body looks like an x402 challenge / manifest (accepts/x402Version)."""
    try:
        d = json.loads(body)
    except Exception:
        return False
    return isinstance(d, dict) and bool(
        d.get("accepts") or d.get("x402Version") or d.get("resources"))


class LocalReadinessSource:
    """Self-owned endpoint-readiness scorer: fetch the endpoint + its well-known
    surfaces and score the observable signals ourselves. No third-party call.

    Fail-open: per-check failures count as "signal not satisfied"; if NOTHING
    could be reached at all (likely our own network), check() returns None
    ("unknown") rather than a false 'needs_work'. `fetch` is a test seam:
    callable(url) -> (status:int, headers:dict, body:bytes) or None/raises.
    """

    def __init__(self, timeout=2.5, fetch=None):
        self.timeout = timeout
        self._fetch = fetch

    def check(self, endpoint_url):
        if not endpoint_url or not isinstance(endpoint_url, str):
            return None
        observed = self._observe(endpoint_url)
        if observed is None:
            return None  # reached nothing -> unknown, not a false negative
        return score_readiness(observed)

    def _observe(self, endpoint_url):
        from urllib.parse import urlparse
        u = urlparse(endpoint_url)
        if not u.scheme or not u.netloc:
            return None
        base = "%s://%s" % (u.scheme, u.netloc)
        obs = {"https": u.scheme == "https"}
        reached = False

        # NOTE: we GET the endpoint and look for a 402 challenge. POST-only x402
        # endpoints won't surface their 402 on a GET -- manifest_well_formed below
        # still flags it as an x402 service, so the score degrades gracefully
        # rather than zeroing.
        r = self._get(endpoint_url)
        if r is not None:
            reached = True
            status, _h, body = r
            obs["endpoint_reachable"] = True
            if status == 402:
                obs["payment_challenge"] = _looks_x402(body)

        m = self._get(base + "/.well-known/x402")
        if m is not None:
            reached = True
            if m[0] == 200:
                obs["manifest_present"] = True
                obs["manifest_well_formed"] = _looks_x402(m[2])

        for path in ("/.well-known/openapi.json", "/openapi.json"):
            o = self._get(base + path)
            if o is not None:
                reached = True
                if o[0] == 200:
                    obs["openapi"] = True
                    break

        h = self._get(base + "/")
        if h is not None:
            reached = True
            if h[0] < 400:
                obs["homepage_reachable"] = True
                obs["schema_org"] = b"schema.org" in (h[2] or b"")

        rb = self._get(base + "/robots.txt")
        if rb is not None:
            reached = True
            if rb[0] == 200:
                obs["robots"] = True

        return obs if reached else None

    def _get(self, url):
        if self._fetch is not None:
            try:
                return self._fetch(url)
            except Exception:
                return None
        import urllib.error
        try:
            req = urllib.request.Request(
                url, headers={"user-agent": "Blackwall-readiness/1",
                              "accept": "application/json, */*"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return (r.status, dict(r.headers), r.read(_READINESS_MAX_BODY))
        except urllib.error.HTTPError as e:  # 402/404/etc. are signal, not failure
            try:
                body = e.read(_READINESS_MAX_BODY)
            except Exception:
                body = b""
            return (e.code, dict(e.headers or {}), body)
        except Exception:
            return None


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
                     "accept": "application/json",
                     "user-agent": "Mozilla/5.0 (compatible; Blackwall/0.1)"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())
