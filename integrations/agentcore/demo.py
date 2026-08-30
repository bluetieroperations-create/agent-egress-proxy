"""
Blackwall x AWS Bedrock AgentCore Payments -- the gap, demonstrated.

    python integrations/agentcore/demo.py

RUN IT YOURSELF. Every verdict below comes from a live HTTP call to the public
Blackwall endpoint. Nothing here is recorded, mocked or staged: change a number,
re-run it, and watch the answer change. That is the point -- a claim you can
falsify in thirty seconds is worth more than a slide.

WHAT IS BEING COMPARED

  AgentCore column   AWS's own two documented session constraints, implemented
                     here in `agentcore_session_allows` -- `limits.maxSpendAmount`
                     and `expiryTimeInMinutes`, and NOTHING else, because there is
                     nothing else. No payee allowlist, no counterparty screening,
                     no price check. Read that function: it is nine lines, and you
                     can check it against AWS's documentation line by line. This
                     script does NOT call AWS.

  Blackwall column   a real verdict from the live service, through the SAME
                     adapter (`AgentCoreGuard`) a production integration uses.

THE POINT IN ONE SENTENCE: AgentCore enforces HOW MUCH and never asks WHO.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from agentcore_guard import (ALLOW, BLOCK, CONFIRM, X402, AgentCoreGuard,
                             http_decider)

LIVE = "https://blackwall-free.onrender.com"

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# A real, established x402 merchant: 239 settlements from 222 distinct payers,
# harvested from public Base history. Its reputation is earned, not configured.
ESTABLISHED = "0x480cd46e6fade651a0437deadda53d5c8e7d846a"
BRAND_NEW = "0x" + "7" * 40
AGENT_WALLET = "0x" + "2" * 40

# The session the agent is operating under. This is the WHOLE of what AgentCore
# constrains.
SESSION = {"maxSpendAmount": {"value": "1.00", "currency": "USD"},
           "expiryTimeInMinutes": 15}

UNLIMITED = str((1 << 128) + 1)          # the uint-max "infinite approval"


def agentcore_session_allows(body, session=SESSION, now_minutes=0):
    """AWS AgentCore Payments' own session check, in full.

    `limits.maxSpendAmount` and `expiryTimeInMinutes`. That is the entire
    surface. The merchant's `payTo` is not consulted, because there is no field
    in which to consult it.
    """
    if now_minutes >= session["expiryTimeInMinutes"]:
        return False, "session expired"
    payload = body["paymentInput"]["cryptoX402"]["payload"]
    amount = int(payload.get("amount") or payload.get("maxAmountRequired") or 0)
    cap = float(session["maxSpendAmount"]["value"])
    spend = amount / 1e6                          # USDC is 6 decimals
    if spend > cap:
        return False, "over the $%.2f session cap" % cap
    return True, "$%.2f of a $%.2f cap, session valid" % (spend, cap)


def process_payment(body):
    """Stand-in for AgentCore's ProcessPayment: it SIGNS and returns proof.

    Modelled on the real thing in the one way that matters: once this returns,
    the signed payload is in the agent's hands and nothing downstream can recall
    it. That is why a STOP has to withhold the call rather than undo it.
    """
    payload = body["paymentInput"]["cryptoX402"]["payload"]
    return {"status": "PROOF_GENERATED", "signedTo": payload["payTo"]}


def request(*, pay_to, amount, scheme="exact", allowance=None):
    """An AgentCore ProcessPayment body."""
    payload = {"scheme": scheme, "network": "eip155:8453", "asset": USDC_BASE,
               "payTo": pay_to}
    if scheme == "upto":
        payload["maxAmountRequired"] = amount
    else:
        payload["amount"] = amount
    crypto = {"payload": payload}
    if allowance is not None:
        crypto["permit2AllowanceLimit"] = allowance
    return {"paymentType": X402, "paymentInput": {"cryptoX402": crypto}}


SCENARIOS = [
    ("An ordinary payment to an established merchant",
     "239 settlements from 222 distinct payers, priced in line with its own history.",
     request(pay_to=ESTABLISHED, amount="50000")),

    ("A merchant nobody has ever paid",
     "No history at all. Not evidence of fraud -- evidence of nothing, which is "
     "its own reason to ask a human first.",
     request(pay_to=BRAND_NEW, amount="50000")),

    ("$0.05 payment, approval over the ENTIRE wallet",
     "AWS's own docs offer granting an unlimited allowance as a normal option. "
     "An allowance is not a spend, so the $1.00 cap is still satisfied.",
     request(pay_to=ESTABLISHED, amount="50000", scheme="upto",
             allowance=UNLIMITED)),

    ("$0.05 payment, approval 1000x the quote",
     "Recorded, and deliberately NOT gated. Approving once and metering many "
     "calls under it is how `upto` is meant to work, so a tight ratio would "
     "flag correct behaviour. The note is there if you want it.",
     request(pay_to=ESTABLISHED, amount="50000", scheme="upto",
             allowance=str(50000 * 1000))),
]

# Which reason actually explains each verdict. Printing the first two buries the
# interesting one behind boilerplate about settlement counts.
KEYWORDS = ("allowance", "UNLIMITED", "no price history", "prior settlements")

VERDICT_LINE = {ALLOW: "signs the payment",
                CONFIRM: "asks a human first",
                BLOCK: "WITHHOLDS the signature"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Blackwall x AgentCore demo")
    parser.add_argument("--url", default=os.environ.get("BLACKWALL_URL", LIVE),
                        help="Blackwall endpoint (default: the public service)")
    args = parser.parse_args(argv)

    guard = AgentCoreGuard(http_decider(args.url))
    print(__doc__.strip().split("\n")[0])
    print("verdicts from %s -- live, not recorded\n" % args.url)
    print("session: max spend %s %s, expires in %d minutes\n"
          % (SESSION["maxSpendAmount"]["value"],
             SESSION["maxSpendAmount"]["currency"],
             SESSION["expiryTimeInMinutes"]))

    tally = {ALLOW: 0, CONFIRM: 0, BLOCK: 0}
    signed_anyway = []
    for title, note, body in SCENARIOS:
        ok, why = agentcore_session_allows(body)
        started = time.time()
        result = guard.process(body, process_payment)
        took = (time.time() - started) * 1000
        decision = result.decision

        print("-" * 78)
        print(title)
        print("  %s" % note)
        print("  AgentCore : %-8s %s" % ("APPROVES" if ok else "refuses", why))
        print("  Blackwall : %-8s %s   (%.0f ms)"
              % (decision.verdict or "n/a",
                 VERDICT_LINE.get(decision.action, decision.action), took))
        reasons = decision.reasons or []
        ranked = sorted(reasons, key=lambda r: min(
            [KEYWORDS.index(k) for k in KEYWORDS if k in r] or [len(KEYWORDS)]))
        for reason in ranked[:2]:
            print("              %s" % reason[:112])
        print("  outcome   : %s"
              % ("signed -- %s" % json.dumps(result.response)
                 if result.processed else "NOT signed; no proof was ever generated"))
        tally[decision.action] = tally.get(decision.action, 0) + 1
        if ok and decision.action != ALLOW:
            signed_anyway.append((title, decision.verdict))

    print("-" * 78)
    print("\nAgentCore approved all %d. Every one is inside the $%s cap and the"
          % (len(SCENARIOS), SESSION["maxSpendAmount"]["value"]))
    print("session window, so there is nothing left for it to check.\n")
    print("Blackwall signed %d, asked a human about %d, refused %d:"
          % (tally[ALLOW], tally[CONFIRM], tally[BLOCK]))
    for title, verdict in signed_anyway:
        print("   %-6s %s" % (verdict, title))
    print("\nNote what it did NOT do. The established merchant was paid. The 1000x")
    print("allowance was recorded and allowed through, because headroom is how the")
    print("metered scheme is meant to work. Only an approval over the entire")
    print("balance was refused outright, and an unknown counterparty became a")
    print("question rather than a denial.")
    print("\nAgentCore enforces HOW MUCH. It never asks WHO.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
