#!/usr/bin/env python3
"""
demo_flywheel.py -- the verdict -> outcome -> reputation -> verdict loop, end to end.

SIMULATION (labeled): this proves the PLUMBING of the whole loop turns, with a REAL
EIP-3009 signature on the payment leg (via eth-account, if installed) and settlement
mocked. It is NOT a real mainnet payment -- a funded round-trip needs a funded key and
real money, which is the operator's to run (see docs/LIVE_PROOF.md).

What it shows, all through the real code (blackwall.forecast + ledger + the
recency-weighted dispute signal):

  1. cold start        -> HOLD (Blackwall knows nothing yet)
  2. 25 payments settle -> the loop feeds itself -> GO (real history, distinct payers)
  3. the merchant goes bad (recent disputes) -> HOLD again (going_bad gate)

Run:  python3 clients/demo_flywheel.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blackwall as bw          # noqa: E402
from ledger import EventLedger, LedgerReputationSource   # noqa: E402

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
MERCHANT = "0x" + "3a" * 20
PAYERS = ["0x%040x" % (0xA11CE + i) for i in range(5)]   # 5 distinct agent wallets


def _sign_leg(payer_priv, to, value):
    """REAL EIP-3009 transferWithAuthorization signature; returns the recovered
    signer so we can prove the payment leg is genuine crypto. None if eth-account
    isn't installed (the flywheel still runs; only the signature proof is skipped)."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
    except Exception:
        return None
    acct = Account.from_key(payer_priv)
    typed = {
        "types": {
            "EIP712Domain": [{"name": "name", "type": "string"},
                             {"name": "version", "type": "string"},
                             {"name": "chainId", "type": "uint256"},
                             {"name": "verifyingContract", "type": "address"}],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"}],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {"name": "USD Coin", "version": "2", "chainId": 8453,
                   "verifyingContract": USDC},
        "message": {"from": acct.address, "to": to, "value": int(value),
                    "validAfter": 0, "validBefore": 2**48 - 1,
                    "nonce": b"\x11" * 32},
    }
    signed = acct.sign_message(encode_typed_data(full_message=typed))
    recovered = Account.recover_message(encode_typed_data(full_message=typed),
                                        signature=signed.signature)
    return acct.address, recovered


def _forecast(source, ledger, payer=None):
    payload = {"counterparty": MERCHANT, "amount": "0.09", "asset": USDC,
               "chain": "base"}
    if payer:
        payload["payer"] = payer
    return bw.forecast(payload, source, ledger)


def run_flywheel():
    """Drive the loop and return {signature_ok, stages:[(name, verdict, record)]}.
    Pure of stdout -- the harness for both main() and the test."""
    ledger = EventLedger(tempfile.mkdtemp() + "/ledger.jsonl")
    source = LedgerReputationSource(ledger)   # reputation built ONLY from the loop
    stages = []

    def snap(name):
        rec = source.lookup(MERCHANT)
        v, _ = bw.forecast({"counterparty": MERCHANT, "amount": "0.09", "asset": USDC,
                            "chain": "base"}, source, None)
        stages.append((name, v["verdict"], rec))

    proof = _sign_leg("0x" + "42" * 32, MERCHANT, 90000)
    signature_ok = bool(proof) and proof[0].lower() == proof[1].lower()

    snap("cold start")                                     # 1: HOLD
    receipts = []
    for i in range(25):
        resp, _ = _forecast(source, ledger, payer=PAYERS[i % len(PAYERS)])
        rid = resp["receipt_id"]
        _sign_leg("0x" + ("%064x" % (0xBEEF + i)), MERCHANT, 90000)   # real signature
        ledger.record_outcome(rid, "settled", observed_amount="0.09",
                              settlement_tx="0x" + ("%064x" % (i + 1)),
                              source="chain-watch", ts="2026-07-%02dT00:00:00Z" % (i + 1))
        receipts.append(rid)
    snap("after 25 settled payments")                      # 2: GO
    for j, rid in enumerate(receipts[-6:]):
        ledger.record_outcome(rid, "disputed", ts="2026-08-0%dT00:00:00Z" % (j + 1))
    snap("after recent disputes")                          # 3: HOLD
    return {"signature_ok": signature_ok, "signed": bool(proof), "stages": stages}


def main():
    r = run_flywheel()
    if r["signed"]:
        print("payment leg: real EIP-3009 signed; signer recovers == payer: %s\n"
              % r["signature_ok"])
    else:
        print("payment leg: eth-account not installed -> signature proof skipped "
              "(flywheel still runs)\n")
    labels = ["STAGE 1 -- cold start (Blackwall has never seen this merchant):",
              "STAGE 2 -- 25 payments flow through the loop and settle:",
              "STAGE 3 -- the merchant goes bad (last 6 payments disputed):"]
    for lab, (name, verdict, rec) in zip(labels, r["stages"]):
        rdr = rec.get("recent_dispute_rate")
        print(lab)
        print("  %-30s verdict=%-4s  [settlements=%s distinct=%s recent_dispute=%s]\n"
              % (name, verdict, rec["settlement_count"], rec["distinct_payers"],
                 "%.0f%%" % (rdr * 100) if rdr is not None else "n/a"))
    print("The loop turned: a merchant Blackwall knew nothing about earned GO purely "
          "from its own\nsettled verdicts, then lost it the moment recent outcomes went "
          "bad -- no manual input.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
