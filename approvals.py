"""
approvals.py -- the human-in-the-loop half of a HOLD.

WHY THIS EXISTS. Every gate in this engine is HOLD-only by design: it refuses to
auto-approve and hands the question to a person. But the engine never had
anywhere to hand it TO. `forecast` returned "HOLD" and the caller was on its own,
so every integration -- LangChain, the wallet adapters, AgentCore -- punts the
same problem back to whoever embedded it. A HOLD nothing acts on is a HOLD that
gets configured away.

FOUND BY COMPETITIVE RE-VERIFICATION, not by imagination: TollWarden's live spec
(paysafe-agent.com/openapi.json, v1.5.0, re-pulled 2026-09-05) ships
`/v1/approvals/config` and `/v1/approvals/{id}` -- a flagged verdict pauses and a
human resumes it. That is the one thing in their product this engine had no
answer to, and it is a workflow gap rather than a detection gap, which is exactly
the kind a detection-focused project fails to notice about itself.

WHAT AN APPROVAL IS, precisely: a record that THE TOKEN HOLDER was asked about
ONE specific payment and said yes. It is NOT an upgrade of the verdict --
`redeem` returns "HOLD, approved", never GO.

AUDIT FINDING, AND THE MOST IMPORTANT LINE IN THIS FILE. An earlier version of
this docstring said "a record that a HUMAN was asked". THE ENGINE CANNOT KNOW
THAT. The capability token is returned to whoever opened the approval, and if
that is the agent, the agent can approve itself in the next call -- measured, it
takes 40ms. There is no human anywhere in "human-in-the-loop" unless the
INTEGRATOR puts one there.

So what this actually provides is narrower and worth stating plainly: a payment
cannot proceed on a HOLD without a SECOND, EXPLICIT, AUDITED act naming an
`actor`, bound to that exact payment, expiring, single-use. Whether a person
performs that act is the integrator's job -- give the token to your approval UI,
your Slack bot, your ops console; do NOT hand it to the agent that opened it.
That separation cannot be enforced from here, and pretending otherwise is worse
than saying so.

FIVE SECURITY PROPERTIES, each of which is a way this could be turned into a
bypass if it were skipped:

  1. STOP IS NEVER APPROVABLE. Only a HOLD can be opened for approval. A STOP is
     sanctions, a payload mismatch or a leaked credential; a human clicking
     "approve" must not be able to route around it, and the API must not offer
     the button.
  2. BOUND TO THE EXACT CLAIM. The record carries a digest of the payment. Get
     "$0.05 to X" approved and you cannot spend the approval on "$500 to Y" --
     without this the whole mechanism is a laundering step for any payment.
  3. SINGLE USE. Redeeming consumes it. One approval authorizes one payment, not
     a standing allowance.
  4. EXPIRES. Conditions move -- a payee can be sanctioned between the ask and
     the answer -- so an approval is only good for `DEFAULT_TTL` and the caller
     re-forecasts after that.
  5. OWNER-ONLY, via the same HMAC capability-token pattern as
     `sign_report_token`. Seeing an approval id in a log must not let a third
     party approve someone else's payment.

Pure + stdlib. Storage is INJECTED; `MemoryApprovalStore` is the reference. A
process restart loses pending approvals, which fails SAFE -- they simply cannot
be redeemed, and the caller re-forecasts.
"""
import hashlib
import hmac
import json
import os
import time
import uuid

PENDING = "pending"
APPROVED = "approved"
DECLINED = "declined"
CONSUMED = "consumed"
EXPIRED = "expired"

#: An approval is a snapshot of a judgement about conditions that move. Fifteen
#: minutes matches the AgentCore payment-session window, which is the shortest
#: real deadline any caller is working to.
DEFAULT_TTL = 900

#: Only these can ever be opened for approval. STOP is deliberately absent --
#: see property 1. Written as a frozenset so a future "just add STOP" edit is a
#: visible change to a named constant rather than a condition buried in a branch.
APPROVABLE = frozenset(("HOLD",))

#: The claim fields that MUST match at redemption. Anything that changes what
#: money moves, to whom, on what chain. Deliberately NOT the whole claim: a
#: caller may legitimately re-send with extra context (a resource URL, price
#: history) and that must not invalidate a human's answer.
BOUND_FIELDS = ("counterparty", "amount", "asset", "chain", "payer")


def claim_digest(claim):
    """Stable digest of the payment an approval is bound to.

    Only `BOUND_FIELDS`, canonically ordered, so the digest is reproducible
    across callers and JSON encoders. `None` and a missing key hash alike, which
    is what a caller omitting an optional field expects.
    """
    if not isinstance(claim, dict):
        claim = {}
    parts = []
    for field in BOUND_FIELDS:
        value = claim.get(field)
        parts.append("" if value is None else str(value).strip().lower())
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _key():
    """HMAC key. Reuses BLACKWALL_RECEIPT_KEY so an operator has one secret to
    manage; domain separation below keeps an approval token from ever being
    mistaken for a report token."""
    return (os.environ.get("BLACKWALL_RECEIPT_KEY") or "dev-insecure-key").encode("utf-8")


def sign_approval_token(approval_id, key=None):
    """Capability token authorizing DECISIONS on `approval_id`.

    Domain-separated with an "approve:" prefix -- a report token and an approval
    token must never be interchangeable, or the right to report an outcome
    silently becomes the right to authorize a payment.
    """
    key = key or _key()
    return hmac.new(key, ("approve:" + str(approval_id)).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def verify_approval_token(approval_id, token, key=None):
    """Constant-time check that `token` authorizes deciding `approval_id`."""
    if not token or not isinstance(token, str):
        return False
    return hmac.compare_digest(token, sign_approval_token(approval_id, key))


def open_approval(verdict, claim, now=None, ttl=DEFAULT_TTL, approval_id=None):
    """Build a PENDING record for a HOLD, or return None.

    None means "not approvable" and is the answer for GO (nothing to ask), for
    STOP (property 1) and for anything unrecognized. PURE: generates no side
    effects and does not store.
    """
    if not isinstance(verdict, dict):
        return None
    if verdict.get("verdict") not in APPROVABLE:
        return None
    # hard_stop is belt-and-braces: a verdict labelled HOLD that also carries a
    # hard stop is incoherent, and the safe reading of an incoherent verdict is
    # the more restrictive one.
    if verdict.get("hard_stop"):
        return None
    started = int(time.time() if now is None else now)
    try:
        window = max(1, int(ttl))
    except (TypeError, ValueError):
        window = DEFAULT_TTL
    return {
        "approval_id": approval_id or uuid.uuid4().hex,
        "state": PENDING,
        "verdict": verdict.get("verdict"),
        "receipt_id": verdict.get("receipt_id"),
        "digest": claim_digest(claim),
        "reasons": list(verdict.get("reasons") or [])[:12],
        "created_at": started,
        "expires_at": started + window,
        "decided_at": None,
        "decided_by": None,
    }


def is_expired(record, now=None):
    """True once the window has passed. Compared with >=, so an approval is dead
    ON its expiry second rather than one second after it."""
    if not isinstance(record, dict):
        return True
    return int(time.time() if now is None else now) >= int(record.get("expires_at") or 0)


def decide(record, approve, now=None, actor=None):
    """PURE: record a human's answer. Returns a NEW record, never mutates.

    Terminal states are final -- a declined or consumed approval cannot be
    flipped to approved by a second call, so a retry loop cannot grind an
    approval out of a refusal.
    """
    if not isinstance(record, dict):
        return record
    if record.get("state") != PENDING:
        return dict(record)
    out = dict(record)
    if is_expired(record, now):
        out["state"] = EXPIRED
        return out
    out["state"] = APPROVED if approve else DECLINED
    out["decided_at"] = int(time.time() if now is None else now)
    out["decided_by"] = str(actor)[:64] if actor else None
    return out


def redeem(record, claim, now=None):
    """PURE: may this payment proceed on this approval? -> (ok, reason, record).

    The returned record is CONSUMED on success (property 3). On failure the
    record is unchanged, so a wrong claim does not burn a human's answer.
    """
    if not isinstance(record, dict):
        return False, "no such approval", record
    if record.get("state") == CONSUMED:
        return False, "approval already used", dict(record)
    if record.get("state") == DECLINED:
        return False, "this payment was declined", dict(record)
    if record.get("state") != APPROVED:
        return False, "approval is still pending", dict(record)
    if is_expired(record, now):
        out = dict(record)
        out["state"] = EXPIRED
        return False, "approval expired -- re-check before paying", out
    if claim_digest(claim) != record.get("digest"):
        # The load-bearing one. An approval for one payment is worthless for
        # another, which is what stops it becoming a laundering step.
        return False, "approval was for a different payment", dict(record)
    out = dict(record)
    out["state"] = CONSUMED
    return True, "HOLD, approved by the approval-token holder", out


def public_view(record):
    """What is safe to return to a poller: never the token, never the digest.

    The digest is withheld because it is an offline-guessable commitment to the
    payment -- BOUND_FIELDS is a short, low-entropy tuple, so publishing the
    digest would let anyone holding an approval id confirm amounts and payees by
    brute force.
    """
    if not isinstance(record, dict):
        return {}
    return {k: record.get(k) for k in
            ("approval_id", "state", "verdict", "receipt_id", "reasons",
             "created_at", "expires_at", "decided_at")}


class ApprovalStoreFull(Exception):
    """Raised rather than evicting a live pending approval. See put()."""


class MemoryApprovalStore:
    """Reference store. Bounded, so an unauthenticated opener cannot grow it
    without limit; oldest pending records are dropped first, which fails SAFE
    (a dropped approval cannot be redeemed)."""

    def __init__(self, limit=10000):
        self._rows = {}
        self._limit = max(1, int(limit))

    def put(self, record):
        """AUDIT FINDING (fixed): this evicted the OLDEST row, pending or not.
        Opening an approval is the cheapest write in the API, so filling the
        store flushed a legitimate PENDING approval out of it -- measured, six
        opens against a limit of three erased the victim. That is a denial of
        the feature by anyone who can call it.

        Terminal rows (consumed / declined / expired) are spent and free to
        drop. A pending one is a live question someone is waiting on, so when
        only pending rows remain the store REFUSES the new record instead. A
        refused open is a visible error the caller retries; a silently evicted
        approval is a payment that mysteriously cannot proceed.
        """
        if record["approval_id"] not in self._rows and len(self._rows) >= self._limit:
            spent = [r for r in self._rows.values() if r.get("state") != PENDING]
            if not spent:
                raise ApprovalStoreFull(
                    "approval store is full of pending requests -- decide or "
                    "expire some before opening more")
            oldest = min(spent, key=lambda r: r.get("created_at", 0))
            self._rows.pop(oldest["approval_id"], None)
        self._rows[record["approval_id"]] = dict(record)
        return record

    def get(self, approval_id):
        row = self._rows.get(approval_id)
        return dict(row) if row else None

    def __len__(self):
        return len(self._rows)


def sweep(store, now=None):
    """Mark passed-deadline pending records EXPIRED. Advisory tidying only --
    `redeem` and `decide` both check expiry themselves, so a store that is never
    swept is still correct, just untidy."""
    swept = 0
    for approval_id in list(getattr(store, "_rows", {})):
        row = store.get(approval_id)
        if row and row.get("state") == PENDING and is_expired(row, now):
            row["state"] = EXPIRED
            store.put(row)
            swept += 1
    return swept
