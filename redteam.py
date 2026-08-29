#!/usr/bin/env python3
"""
redteam.py -- adversarial coverage scorecard for the verdict engine.

Runs a battery of attacks and legit controls through blackwall.decide_payment and
tabulates the outcome, HONESTLY -- including the documented gaps where an attack
gets through (large Sybil farms above the graph ceiling; the advisory-only signals).
The point is an auditable "what we catch and what we don't", not a green checkmark.

Dispositions:
  CAUGHT       -- an attack was blocked (HOLD/STOP) as intended.
  KNOWN GAP    -- an attack got GO; a documented limitation, not a surprise.
  CLEAN        -- a legit payment correctly GOes.
  FALSE POSITIVE -- a legit payment was wrongly blocked (a real bug if it appears).

Two scenario families:
  SCENARIOS      -- driven through `decide_payment` (the reputation/price/Sybil core).
  SIM_SCENARIOS  -- driven through `forecast` with INJECTED simulation sources, because
                    the settlement / authorization / RWA-readiness gates fold there, not
                    in decide_payment. These cover the newest and least battle-tested
                    code, and -- just as importantly -- assert the properties that keep
                    them from over-blocking: sender-side reverts are NOT blamed on the
                    receiver, an underfunded payer does NOT gate, and an unreachable RPC
                    fails OPEN.

`run()` is importable (used by test_redteam to guard that the CAUGHT set stays caught
and no FALSE POSITIVE appears); `main()` prints the table / writes JSON.
"""
from __future__ import annotations

import blackwall as bw

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
LEGIT = "0x" + "1" * 40
ATTACKER = "0x" + "2" * 40
STABLE = ["0.09", "0.09", "0.088", "0.092"]     # median 0.09
# a reputable, in-budget, fair-priced baseline record.
GOOD = {"settlement_count": 500, "dispute_rate": 0.0, "distinct_payers": 30}

# (name, category, expect: "block"|"stop"|"allow", known_gap: bool, decide_payment kwargs)
#   block -- must not GO (HOLD or STOP both acceptable)
#   stop  -- must be STOP specifically; a HOLD is a MISS. Use when the SEVERITY is
#            the property under test, so the scenario can actually fail.
#   allow -- must GO
SCENARIOS = [
    # --- attacks that MUST be caught ---
    ("sanctioned counterparty", "sanctions", "block", False,
     dict(amount="0.09", record=dict(GOOD, sanctioned=True), price_history=STABLE,
          counterparty=LEGIT)),
    ("known-bad counterparty", "known-bad", "block", False,
     dict(amount="0.09", record=dict(GOOD, known_bad=True), price_history=STABLE,
          counterparty=LEGIT)),
    ("recipient mismatch vs 402", "payload-sim", "block", False,
     dict(amount="0.09", record=GOOD, price_history=STABLE,
          counterparty=ATTACKER, expected_recipient=LEGIT)),
    ("price gouge (>=8x median)", "price", "block", False,
     dict(amount="0.72", record=GOOD, price_history=STABLE, counterparty=LEGIT)),
    # The price STOP is corroborated before it stands (blackwall.price_stop_is_
    # corroborated), so a payee can try to MANUFACTURE the corroboration: self-pay
    # three times at the inflated price (~$15, recovered on the first victim) to
    # forge an "established tier", then quote it. The tier arm is payer-weighted
    # precisely to stop this -- a wash farm controls settlement COUNT cheaply, but
    # not independent counterparties.
    # Expect "stop", NOT "block": a price gouge always scores anomaly 1.0 and so
    # always HOLDs, which means a "block" expectation would pass even with
    # MIN_TIER_PAYERS removed. Mutation-verified -- setting MIN_TIER_PAYERS = 0
    # turns this into MISS (BUG) and fails the run.
    ("wash-forged price tier", "price", "stop", False,
     dict(amount="0.72", counterparty=LEGIT,
          record=dict(GOOD, price_observations=(
              [{"payer": "0x%040x" % i, "amount": "0.09"} for i in range(30)]
              + [{"payer": ATTACKER, "amount": "0.72"}] * 3)),
          price_history=STABLE + ["0.72"] * 3)),
    ("price anomaly (moderate)", "price", "block", False,
     dict(amount="0.30", record=GOOD, price_history=STABLE, counterparty=LEGIT)),
    ("thin history (<20 settlements)", "thin", "block", False,
     dict(amount="0.09", record={"settlement_count": 5, "dispute_rate": 0.0},
          price_history=STABLE, counterparty=LEGIT)),
    ("Sybil: <3 distinct payers", "sybil", "block", False,
     dict(amount="0.09", record={"settlement_count": 500, "distinct_payers": 2,
                                 "dispute_rate": 0.0},
          price_history=STABLE, counterparty=LEGIT)),
    ("captive Sybil (graph)", "sybil-graph", "block", False,
     dict(amount="0.09", record=GOOD, price_history=STABLE, counterparty=LEGIT,
          payer_graph_signal={"captive_sybil": True, "distinct_payers": 4})),
    ("going bad (recent disputes)", "outcome", "block", False,
     dict(amount="0.09", record=dict(GOOD, recent_dispute_rate=0.5, recent_outcomes=10),
          price_history=STABLE, counterparty=LEGIT)),
    ("stale / dormant endpoint", "temporal", "block", False,
     dict(amount="0.09", record=GOOD, price_history=STABLE, counterparty=LEGIT,
          temporal_signal={"stale": True, "recency_days": 200})),
    ("over budget (amount > $10)", "amount", "block", False,
     dict(amount="25.00", record=GOOD, price_history=["25", "25", "24"],
          counterparty=LEGIT)),
    ("cold-start unknown", "cold-start", "block", False,
     dict(amount="0.09", record={}, price_history=STABLE, counterparty=LEGIT)),
    # self-reported wash: claims 999 settlements but 0 are chain-CONFIRMED -> the
    # thin gate counts only confirmed, so it can't talk its way out of HOLD.
    ("self-reported wash (0 confirmed)", "thin", "block", False,
     dict(amount="0.09",
          record={"settlement_count": 999, "confirmed_settlement_count": 0,
                  "distinct_payers": 0, "dispute_rate": 0.0},
          price_history=STABLE, counterparty=LEGIT)),
    # cold-start gouge caught by the CATEGORY baseline: reputable, no own price
    # history, but quoting 60x the on-chain median for its service category.
    ("category price gouge (cold-start)", "category", "block", False,
     dict(amount="0.30", record=GOOD, price_history=[], counterparty=LEGIT,
          category="finance", category_median="0.005")),   # 0.30 / 0.005 = 60x
    # bait-and-switch: lists cheap on the Bazaar, historically settles 9x its most
    # expensive listing -> the public price understates the real cost.
    ("advertised-vs-settled bait-and-switch", "price-integrity", "block", False,
     dict(amount="0.09", record=GOOD, price_history=STABLE, counterparty=LEGIT,
          divergence_ratio="12.0")),
    # free on-chain enrichment: Blockscout flags the address as scam-associated ->
    # REVIEW (HOLD). Never a STOP (crowd tag, not a compliance decision).
    ("on-chain scam tag (Blockscout REVIEW)", "onchain-enrich", "block", False,
     dict(amount="0.09", record=GOOD, price_history=STABLE, counterparty=LEGIT,
          enrichment={"review": True, "is_scam": True, "reasons": ["scam-associated"]})),
    # leaked-secret guard: a credential in a free-text payment field would be published
    # on-chain the moment it's signed -> hard STOP (secret_scan.py). Reason is redacted.
    ("leaked secret in payload (API key)", "secret-scan", "block", False,
     dict(amount="0.09", record=GOOD, price_history=STABLE, counterparty=LEGIT,
          secret_findings=[{"type": "aws_access_key_id", "severity": "high",
                            "field": "memo", "hint": "AKIA***"}])),
    # Sybil ring: enough distinct payers to clear the naive gate, yet NOT ONE pays a
    # trusted anchor -- a closed, unvouched cluster. Now GATES (HOLD) after the coverage-
    # convergence eval (coverage_eval.py) proved the false-flag rate has stabilized.
    ("Sybil ring (no payer pays an anchor)", "sybil-graph", "block", False,
     dict(amount="0.09", record=GOOD, price_history=STABLE, counterparty=LEGIT,
          payer_graph_signal={"captive_sybil": False, "sybil_ring": True,
                              "distinct_payers": 6, "established_payers": 5,
                              "reputable_payers": 0})),

    # --- documented GAPS (attack gets GO) ---
    ("large captive farm (>ceiling)", "sybil-graph", "block", True,
     dict(amount="0.09", record=dict(GOOD, distinct_payers=13), price_history=STABLE,
          counterparty=LEGIT,
          # graph does NOT set captive_sybil above the ceiling -> no gate fires
          payer_graph_signal={"captive_sybil": False, "distinct_payers": 13,
                              "established_payers": 0})),
    ("burst-acquired Sybil (diagnostic)", "temporal", "block", True,
     dict(amount="0.09", record=GOOD, price_history=STABLE, counterparty=LEGIT,
          temporal_signal={"stale": False, "burst_sybil": True, "peak_day_share": 0.95})),

    # --- legit controls: must NOT be blocked ---
    ("established, fair price", "control", "allow", False,
     dict(amount="0.09", record=GOOD, price_history=STABLE, counterparty=LEGIT)),
    # BOUNDARY control: a payee in the ring band (only 6 distinct payers) that is NOT a
    # ring -- one of its payers is reputable (pays a trusted anchor), so sybil_ring is
    # False. Proves the gate keys on reputable_payers==0, NOT on a low distinct count:
    # an established payee with few payers still GOes.
    ("established payee, few but reputable payers", "control", "allow", False,
     dict(amount="0.09", record=GOOD, price_history=STABLE, counterparty=LEGIT,
          payer_graph_signal={"captive_sybil": False, "sybil_ring": False,
                              "distinct_payers": 6, "established_payers": 5,
                              "reputable_payers": 2, "avg_payer_reputation": 0.6})),
    # premium (15x the category median) is legit, not a gouge -> the category gate
    # (50x) must NOT false-positive on it. Guards the eval-calibrated threshold.
    # NOTE: the restraint property for the wash-forged-tier attack above -- that a
    # premium price backed by INDEPENDENT payers is downgraded to HOLD rather than
    # STOP -- is NOT expressible here. This harness is binary (allow=GO,
    # block=not-GO) and a corroborated anomaly is still an anomaly, so it HOLDs and
    # would register as a FALSE POSITIVE. It is pinned exactly where the
    # distinction lives instead: test_price_corroboration.EndToEndVerdict.
    # test_legit_premium_tier_is_HOLD_not_STOP.
    ("premium price (15x, under category bar)", "control", "allow", False,
     dict(amount="0.30", record=GOOD, price_history=[], counterparty=LEGIT,
          category="finance", category_median="0.02")),   # 0.30 / 0.02 = 15x
]


# ===========================================================================
# Simulation-gate scenarios (folded in `forecast`, not `decide_payment`)
# ===========================================================================

PAYER = "0x" + "3" * 40
NONCE = "0x" + "7f" * 32
SIG65 = "0x" + ("11" * 32) + ("22" * 32) + "1b"
BLACKLIST_REVERT = "Blacklistable: account is blacklisted"   # real USDC string
KYC_REVERT = "STBT: NO_RECEIVE_PERMISSION"                   # real Matrixdock string


class _StubRep:
    """A reputation source good enough that the BASELINE verdict is GO -- otherwise a
    cold-start HOLD would mask whether the simulation gate actually fired."""

    def __init__(self, record=None):
        self.record = record or dict(GOOD, price_history=STABLE)

    def lookup(self, addr):
        return dict(self.record)


def _revert(msg):
    """An eth_call response carrying a real Error(string) revert payload."""
    b = msg.encode()
    return {"error": {"message": "execution reverted",
                      "data": "0x08c379a0" + "%064x" % 32 + "%064x" % len(b)
                              + b.hex().ljust(64, "0")}}


_OK = {"result": "0x1"}


def _settlement_source(target, control=_OK):
    """SettlementSimSource whose first call (payer->payee) returns `target` and whose
    second (payer->control address) returns `control`."""
    from settlement_sim import SettlementSimSource
    from transfer_sim import TransferSimulator
    seen = []

    def transport(params):
        seen.append(params)
        return target if len(seen) == 1 else control
    return SettlementSimSource(simulator=TransferSimulator(transport=transport))


def _auth_source(used=False, execution=_OK, now=1000):
    """AuthorizationSimSource: `used` drives the authorizationState replay read."""
    from auth_sim import AuthorizationSimSource

    class _Sim:
        def _call(self, params):
            if len(params.get("data", "")) < 200:          # authorizationState
                return {"result": "0x" + "0" * 63 + ("1" if used else "0")}
            return execution
    return AuthorizationSimSource(simulator=_Sim(), now=now)


def _rwa_source(target, control=_OK):
    from transfer_sim import SimulationReadinessSource, TransferSimulator
    seen = []

    def transport(params):
        seen.append(params)
        return target if len(seen) == 1 else control
    return SimulationReadinessSource(simulator=TransferSimulator(transport=transport))


def _payload(**kw):
    p = {"counterparty": LEGIT, "amount": "0.09", "asset": "USDC", "chain": "base",
         "payer": PAYER, "price_history": STABLE}
    p.update(kw)
    return p


def _signed(auth_over=None):
    """A base64 X-PAYMENT carrying a signed EIP-3009 authorization."""
    import base64
    import json
    auth = {"from": PAYER, "to": LEGIT, "value": "90000", "validAfter": 0,
            "validBefore": 9999999999, "nonce": NONCE}
    auth.update(auth_over or {})
    return base64.b64encode(json.dumps(
        {"x402Version": 1, "scheme": "exact", "network": "base",
         "payload": {"authorization": auth, "signature": SIG65}}).encode()).decode()


# (name, category, expect, known_gap, payload, forecast-source kwargs)
SIM_SCENARIOS = [
    # --- attacks the simulation gates MUST catch ---
    # `upto` settles via Permit2 `transferFrom`, so it needs an ERC-20 allowance.
    # AWS AgentCore's own docs offer granting an UNLIMITED one as a normal option,
    # and no spending cap can restrain it -- an allowance is not a spend. Same
    # exposure calldata.py hard-STOPs as raw calldata, arriving as a payment intent.
    ("upto grants unlimited Permit2 allowance", "upto-scheme", "block", False,
     _payload(scheme="upto",
              permit2AllowanceLimit=str((1 << 256) - 1),
              accepts=[{"scheme": "upto", "maxAmountRequired": "1000"}]),
     lambda: {}),
    ("blacklisted PAYEE (USDC)", "settlement-sim", "block", False, _payload(),
     lambda: {"settlement_sim_source": _settlement_source(_revert(BLACKLIST_REVERT))}),
    ("blacklisted PAYER (USDC)", "settlement-sim", "block", False, _payload(),
     lambda: {"settlement_sim_source": _settlement_source(
         _revert(BLACKLIST_REVERT), control=_revert(BLACKLIST_REVERT))}),
    ("replayed EIP-3009 nonce", "auth-sim", "block", False,
     _payload(payment_authorization=_signed()),
     lambda: {"auth_sim_source": _auth_source(used=True)}),
    ("expired authorization", "auth-sim", "block", False,
     _payload(payment_authorization=_signed({"validBefore": 1000})),
     lambda: {"auth_sim_source": _auth_source(now=10 ** 9)}),
    ("authorization reverts (bad sig)", "auth-sim", "block", False,
     _payload(payment_authorization=_signed()),
     lambda: {"auth_sim_source": _auth_source(
         execution=_revert("ECRecover: invalid signature"))}),
    ("RWA receiver not permissioned", "rwa-sim", "block", False,
     _payload(acquires={"token": "0x" + "9" * 40, "chain": "ethereum",
                        "holder": "0x" + "8" * 40}),
     lambda: {"rwa_source": _rwa_source(_revert(KYC_REVERT))}),

    # --- controls: these must NOT be blocked (over-blocking is the real risk here) ---
    ("upto with a sane allowance", "control", "allow", True,
     _payload(scheme="upto", permit2AllowanceLimit="1000",
              accepts=[{"scheme": "upto", "maxAmountRequired": "1000"}]),
     lambda: {}),
    ("clean payment, sim ready", "control", "allow", False, _payload(),
     lambda: {"settlement_sim_source": _settlement_source(_OK)}),
    ("underfunded payer (must not gate)", "control", "allow", False, _payload(),
     lambda: {"settlement_sim_source": _settlement_source(
         _revert("ERC20: transfer amount exceeds balance"))}),
    ("SENDER-side revert (attribution)", "control", "allow", False,
     _payload(acquires={"token": "0x" + "9" * 40, "chain": "ethereum",
                        "holder": "0x" + "8" * 40}),
     # target AND control both fail -> the SENDER is at fault; blaming the receiver
     # here would be a false positive. This guards the control-attribution property.
     lambda: {"rwa_source": _rwa_source(_revert(KYC_REVERT),
                                        control=_revert(KYC_REVERT))}),
    ("RPC unreachable (fail-open)", "control", "allow", False, _payload(),
     lambda: {"settlement_sim_source": _settlement_source({}, control={})}),
    ("valid fresh authorization", "control", "allow", False,
     _payload(payment_authorization=_signed()),
     lambda: {"auth_sim_source": _auth_source(used=False)}),
]


def run_sim():
    """Drive the simulation-gate scenarios through `forecast`."""
    results = []
    for name, cat, expect, known_gap, payload, sources in SIM_SCENARIOS:
        v, err = bw.forecast(payload, _StubRep(), verify_signer=False, **sources())
        verdict = v["verdict"] if err is None else "ERROR"
        results.append({"name": name, "category": cat, "expect": expect,
                        "known_gap": known_gap, "verdict": verdict,
                        "disposition": _disposition(expect, known_gap, verdict)})
    return results


def _disposition(expect, known_gap, verdict):
    blocked = verdict in ("HOLD", "STOP")
    if expect == "allow":
        return "CLEAN" if not blocked else "FALSE POSITIVE"
    if expect == "stop":
        # SEVERITY-AWARE. "block" cannot express every defense: some gates decide
        # STOP vs HOLD, and both count as blocked, so a scenario written as "block"
        # passes even with the gate removed -- coverage that cannot fail. Where the
        # severity IS the property under test, assert it directly.
        if verdict == "STOP":
            return "CAUGHT"
        return "KNOWN GAP" if known_gap else "MISS (BUG)"
    # expect == "block"
    if blocked:
        return "CAUGHT"
    return "KNOWN GAP" if known_gap else "MISS (BUG)"


def run():
    results = []
    for name, cat, expect, known_gap, kw in SCENARIOS:
        v = bw.decide_payment(**kw)
        results.append({"name": name, "category": cat, "expect": expect,
                        "known_gap": known_gap, "verdict": v["verdict"],
                        "disposition": _disposition(expect, known_gap, v["verdict"])})
    results.extend(run_sim())
    return results


def main(argv=None):
    import argparse
    import collections
    import json
    p = argparse.ArgumentParser(description="Adversarial coverage scorecard.")
    p.add_argument("--json", help="write results JSON here")
    args = p.parse_args(argv)
    results = run()
    counts = collections.Counter(r["disposition"] for r in results)
    w = 34
    print("%-34s %-13s %-6s %s" % ("SCENARIO", "CATEGORY", "VERD", "DISPOSITION"))
    print("-" * 72)
    for r in results:
        print("%-34s %-13s %-6s %s" % (r["name"][:w], r["category"], r["verdict"],
                                       r["disposition"]))
    print("-" * 72)
    print("caught=%d  known-gap=%d  clean=%d  false-positive=%d  miss=%d"
          % (counts["CAUGHT"], counts["KNOWN GAP"], counts["CLEAN"],
             counts["FALSE POSITIVE"], counts["MISS (BUG)"]))
    if args.json:
        json.dump(results, open(args.json, "w"), indent=2)
    return 1 if (counts["FALSE POSITIVE"] or counts["MISS (BUG)"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
