#!/usr/bin/env python3
"""
agentcore_guard.py -- gate an AWS Bedrock AgentCore `ProcessPayment` call with a
Blackwall verdict.

THE GAP THIS FILLS, stated from AWS's own documentation. AgentCore Payments is
GA and speaks x402 and MPP, with Coinbase and Stripe/Privy connectors. A payment
session constrains exactly two things:

    limits.maxSpendAmount   {value, currency}
    expiryTimeInMinutes

There is no payee allowlist, no counterparty screening, no merchant restriction
and no price-reasonableness check. `ProcessPayment` "validates the request,
checks the budget, signs the transaction" -- and the merchant's `payTo` is
forwarded VERBATIM into the signature.

So AgentCore enforces HOW MUCH an agent may spend and never evaluates WHO it
pays. Those are orthogonal questions and it only answers the first.

This guard answers the second, BEFORE the signature exists. It sits in front of
`ProcessPayment`: on GO it calls through, on HOLD it asks a human, and on STOP it
withholds the call entirely -- so no proof is ever generated for a payment we
refuse. Withholding is the only durable control here, because once AgentCore
returns `PROOF_GENERATED` the signed payload is in the agent's hands.

Two exposures in particular get past a spend cap, and both reach the engine here:

  * the PAYEE, which nothing in AgentCore looks at;
  * the `upto` scheme's `permit2AllowanceLimit`. `upto` settles through Permit2
    `transferFrom`, so the wallet grants an ERC-20 allowance -- and AWS documents
    granting an UNLIMITED one as a normal option. An allowance is not a spend, so
    a $1.00 session budget coexists with an approval over the whole balance.
    `upto_scheme.py` treats that as a hard STOP.

DESIGN, matching integrations/wallets/wallet_guard.py: a dependency-free core
(this file) plus thin framework shims. Nothing here imports boto3, Strands or
LangGraph -- you pass in the `process_fn` you already call, and this decides
WHETHER to call it. Stdlib only.

    guard = AgentCoreGuard(in_process_decider(), on_unreachable=FAIL_CLOSED)
    result = guard.process(process_payment_body, process_fn=my_process_payment)
    if result.processed:
        proof = result.response["paymentOutput"]
"""
from __future__ import annotations

import base64
import json
import re

OBSERVE = "observe"
ENFORCE = "enforce"

FAIL_CLOSED = "fail_closed"
FAIL_OPEN = "fail_open"

ALLOW = "allow"
CONFIRM = "confirm"
BLOCK = "block"

_REQUEST_PARAM = re.compile(r'request="([^"]+)"')

X402 = "CRYPTO_X402"
MPP = "MPP"


class Decision:
    """What the verdict means for this ProcessPayment call."""

    __slots__ = ("action", "verdict", "reasons", "claim", "error")

    def __init__(self, action, verdict=None, reasons=None, claim=None, error=None):
        self.action = action
        self.verdict = verdict
        self.reasons = list(reasons or [])
        self.claim = claim
        self.error = error

    def __repr__(self):
        return "Decision(action=%r, verdict=%r)" % (self.action, self.verdict)


class ProcessResult:
    """Whether ProcessPayment was actually invoked, and what came back."""

    __slots__ = ("processed", "response", "decision")

    def __init__(self, processed, response=None, decision=None):
        self.processed = processed
        self.response = response
        self.decision = decision

    def __repr__(self):
        return "ProcessResult(processed=%r, decision=%r)" % (
            self.processed, self.decision)


def _b64url(blob):
    """Decode base64url with or without padding. None on anything unreadable."""
    if not isinstance(blob, str) or not blob:
        return None
    text = blob.strip()
    for candidate in (text, text + "=" * (-len(text) % 4)):
        for decoder in (base64.urlsafe_b64decode, base64.b64decode):
            try:
                return decoder(candidate)
            except Exception:
                continue
    return None



def _mpp_currency(header):
    """The SYMBOL an MPP challenge names in `currency`, when it names no address.

    MPP often carries the token as a symbol ("USDC") rather than a contract
    address. `x402_challenge` deliberately refuses to put a symbol in the `asset`
    address slot -- correct, because every downstream address comparison would
    silently fail against it. But `forecast` requires an asset, so without this
    fallback EVERY MPP payment is unscoreable and falls to the availability
    policy. Fail-closed is safe and useless there: it blocks the legitimate ones
    too.

    A symbol is a supported claim shape in this codebase (resolve_decimals reads
    KNOWN_SYMBOL_DECIMALS; payload_sim's asset cross-check applies only when the
    claim names a contract address), so it belongs in the CLAIM. It is never
    written back into the parser's accepts entry, which keeps its strict address
    semantics.

    Returns None for an address (the entry already carries it) or anything
    unreadable. Never raises.
    """
    match = _REQUEST_PARAM.search(header if isinstance(header, str) else "")
    if not match:
        return None
    raw = _b64url(match.group(1))
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    currency = payload.get("currency") if isinstance(payload, dict) else None
    if not isinstance(currency, str) or not currency.strip():
        return None
    currency = currency.strip()
    # An address is already handled by the parser; only a symbol is new here.
    return None if currency.startswith("0x") else currency


def _first_accept(accepts):
    return accepts[0] if isinstance(accepts, list) and accepts \
        and isinstance(accepts[0], dict) else None


def claim_from_process_payment(body):
    """A ProcessPayment request body -> the claim to score, or None.

    The claim is built from `paymentInput`, which is the merchant's own payload --
    the same bytes AgentCore signs. Reading it from anywhere else would screen
    something other than what gets signed.

    Returns None rather than a partial claim when the body cannot be read: a
    half-built claim looks scoreable to a caller that only checks for non-None,
    and the availability policy should govern instead.
    """
    if not isinstance(body, dict):
        return None
    payment_input = body.get("paymentInput")
    if not isinstance(payment_input, dict):
        return None

    kind = body.get("paymentType")

    crypto = payment_input.get("cryptoX402")
    mpp_in = payment_input.get("mpp")
    if kind is None and isinstance(crypto, dict) and isinstance(mpp_in, dict):
        # AMBIGUOUS -- refuse rather than guess. AgentCore selects the protocol by
        # `paymentType`; with it absent and BOTH payloads present, which one AWS
        # signs is not specified by the API. Picking one risks scoring a different
        # payee from the one that gets signed, which is the single property this
        # guard exists to have. An attacker who can shape the body would put a
        # clean payee in the payload we score and a hostile one in the payload
        # that gets signed. None sends this to the availability policy, which is
        # the honest answer to "we cannot tell what will be signed".
        return None
    if isinstance(crypto, dict) and kind in (None, X402):
        payload = crypto.get("payload")
        if not isinstance(payload, dict):
            return None
        pay_to = payload.get("payTo")
        if not pay_to:
            return None
        # `exact` states `amount`; `upto` states `maxAmountRequired` -- the
        # CEILING, which is the exposure the wallet approves against.
        amount = payload.get("amount")
        if amount is None:
            amount = payload.get("maxAmountRequired")
        claim = {"counterparty": pay_to,
                 "amount": None if amount is None else str(amount),
                 "asset": payload.get("asset"),
                 "chain": payload.get("network"),
                 "scheme": payload.get("scheme"),
                 "accepts": [dict(payload)]}
        allowance = crypto.get("permit2AllowanceLimit")
        if allowance is not None:
            # Carried through under AWS's own spelling; upto_scheme reads it.
            claim["permit2AllowanceLimit"] = allowance
        return claim

    mpp = mpp_in
    if isinstance(mpp, dict) and kind in (None, MPP):
        headers = mpp.get("wwwAuthenticateHeaders")
        if not isinstance(headers, list) or not headers:
            return None
        # AgentCore fulfils exactly one challenge per call and requires exactly
        # one header, so the first is the one that will be paid.
        from x402_challenge import parse_challenge
        accepts, _carrier = parse_challenge(b"", {"WWW-Authenticate": headers[0]})
        entry = _first_accept(accepts)
        if not entry or not entry.get("payTo"):
            return None
        amount = entry.get("amount")
        if amount is None:
            amount = entry.get("maxAmountRequired")
        return {"counterparty": entry.get("payTo"),
                "amount": None if amount is None else str(amount),
                "asset": entry.get("asset") or _mpp_currency(headers[0]),
                "chain": entry.get("network"),
                "scheme": "mpp",
                "accepts": [entry]}

    return None


def in_process_decider(reputation_source=None, **forecast_kwargs):
    """Decider backed by the in-process engine (no HTTP, no key)."""
    def decide(claim):
        from blackwall import forecast
        verdict, err = forecast(claim, reputation_source, **forecast_kwargs)
        if err:
            raise RuntimeError("forecast rejected the claim: %s" % err)
        return verdict
    return decide


def http_decider(base_url, *, path="/v1/forecast-payment", headers=None,
                 timeout=10):
    """Decider backed by a Blackwall HTTP endpoint."""
    def decide(claim):
        import urllib.request
        req = urllib.request.Request(
            base_url.rstrip("/") + path,
            data=json.dumps(claim).encode("utf-8"),
            headers=dict({"Content-Type": "application/json"}, **(headers or {})),
            method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    return decide


class AgentCoreGuard:
    """Decide whether an AgentCore ProcessPayment call may proceed.

    `mode`           ENFORCE (default) withholds; OBSERVE always calls through
                     but still reports the verdict, so you can measure before arming.
    `on_unreachable` what to do when the verdict cannot be obtained -- FAIL_CLOSED
                     (default) or FAIL_OPEN. This governs BOTH an unreachable
                     decider and a body we cannot parse: an unreadable request is
                     an unknown, not an approval.
    """

    def __init__(self, decider, *, mode=ENFORCE, on_unreachable=FAIL_CLOSED):
        self.decider = decider
        self.mode = mode
        self.on_unreachable = on_unreachable

    # -- decision ---------------------------------------------------------
    def decide(self, body):
        claim = claim_from_process_payment(body)
        if claim is None:
            return self._unavailable("could not read a payment claim from the "
                                     "ProcessPayment body")
        try:
            verdict = self.decider(claim) or {}
        except Exception as exc:
            return self._unavailable("%s: %s" % (type(exc).__name__, exc),
                                     claim=claim)
        name = str(verdict.get("verdict", "")).upper()
        reasons = verdict.get("reasons") or []
        if name == "GO":
            return Decision(ALLOW, name, reasons, claim)
        if name == "STOP":
            return Decision(BLOCK, name, reasons, claim)
        if name == "HOLD":
            return Decision(CONFIRM, name, reasons, claim)
        # An unrecognised verdict is not an approval.
        return self._unavailable("unrecognised verdict %r" % (name,), claim=claim)

    def _unavailable(self, error, claim=None):
        action = BLOCK if self.on_unreachable == FAIL_CLOSED else ALLOW
        return Decision(action, None, [error], claim, error=error)

    # -- gate -------------------------------------------------------------
    def process(self, body, process_fn, *, approved=False):
        """Run `process_fn(body)` only if the verdict permits it.

        `approved=True` records that a human confirmed a HOLD. It does NOT
        override a STOP: a HOLD is a question, a STOP is an answer.
        """
        decision = self.decide(body)
        may = decision.action == ALLOW or (
            decision.action == CONFIRM and approved)
        if self.mode == OBSERVE:
            may = True
        if not may:
            return ProcessResult(False, None, decision)
        return ProcessResult(True, process_fn(body), decision)
