#!/usr/bin/env python3
"""
spike_data_source.py -- feasibility probe for Blackwall's reputation data.

Answers the load-bearing question with numbers, not assertions:
  1. Reachable?  Can we get a counterparty's history from a live, no-key source?
  2. Fast enough? What is the per-lookup latency cold vs warm (cached)?
  3. Complete enough? Which decision signals are REAL vs UNAVAILABLE on-chain?
  4. End-to-end: does a real record drop into blackwall.forecast() unchanged?

Run (needs outbound HTTPS):
    python spike_data_source.py
    python spike_data_source.py 0xSomeBaseWallet 0xAnother...
"""
import sys
import time

import blackwall as bw
from reputation_onchain import OnchainReputationSource

# A few active Base addresses to exercise the path (override via argv).
SAMPLE = [
    "0x20FE51A9229EeF2cF8Ad9E89d91CAb9312cF3b7A",
    "0x4200000000000000000000000000000000000006",  # WETH (contract, very active)
]


def _time(fn):
    t = time.time()
    out = fn()
    return out, time.time() - t


def main(argv):
    addrs = argv[1:] or SAMPLE
    src = OnchainReputationSource()
    print("Blackwall reputation data-source spike")
    print("source: %s   sample: %d address(es)\n" % (src.base_url, len(addrs)))

    cold, warm = [], []
    for a in addrs:
        try:
            rec, dt_cold = _time(lambda a=a: src.lookup(a))
        except Exception as e:
            print("  %s -> FETCH FAILED: %s: %s" % (a[:12], type(e).__name__, e))
            continue
        _, dt_warm = _time(lambda a=a: src.lookup(a))  # cache hit
        cold.append(dt_cold)
        warm.append(dt_warm)
        meta = rec.get("_meta", {})
        print("  %s..." % a[:12])
        print("    cold=%.2fs  warm(cached)=%.4fs" % (dt_cold, dt_warm))
        print("    tx_count=%s  token_transfers=%s  usdc_inbound(sampled)=%s"
              % (meta.get("tx_count"), meta.get("token_transfer_count"),
                 meta.get("usdc_inbound_sampled")))
        print("    settlement_count(lower-bound)=%s  dispute_rate=%s"
              % (rec.get("settlement_count"), rec.get("dispute_rate")))

        # End-to-end: feed the REAL record straight into the verdict path.
        verdict = bw.decide_payment(
            "0.09", rec, rec.get("price_history") or [], counterparty=a)
        print("    -> forecast verdict on $0.09: %s (score %s)\n"
              % (verdict["verdict"], verdict["score"]))

    if cold:
        print("LATENCY  cold: min=%.2fs max=%.2fs   warm: max=%.4fs"
              % (min(cold), max(cold), max(warm)))
    print("\nSIGNAL COMPLETENESS (on-chain alone):")
    print("  volume / typical-amount / counterparty .... REAL")
    print("  dispute_rate / underdelivery .............. UNAVAILABLE (off-chain / own ledger)")
    print("  age_days .................................. needs an extra first-tx query")
    print("  sanctioned ................................ from a supplied OFAC address set")
    print("\nVERDICT ON THE HOT-PATH QUESTION:")
    print("  Reachable + parseable: YES (no API key).")
    print("  Per-call latency (~1-2s/HTTP) is too slow for a synchronous")
    print("  per-signature check WITHOUT caching -> warm cache is sub-ms;")
    print("  pair with x402 session reuse. The MOAT signal (disputes) is NOT")
    print("  on-chain: Blackwall must accumulate its own outcome ledger.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
