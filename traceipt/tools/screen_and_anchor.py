#!/usr/bin/env python3
"""
End-to-end demo of the wedge: SCREEN an address against real sanctions providers,
build ONE verdict, ANCHOR it through the neutral Traceipt layer, and bind it into
a compliance-bound receipt -- all with real Ed25519 + a real Merkle proof.

    python tools/screen_and_anchor.py [ADDRESS ...]

With no CHAINALYSIS_API_KEY / TRM_API_KEY in the environment it uses the offline
fixture provider (the Chainalysis documentation sample flags as sanctioned), so
the whole chain runs with no key and no network. Set either key to fold in a real
feed -- the verdict shape and everything downstream is identical.

What it proves, concretely:
  * a risk engine's sanctions answer becomes independently verifiable (the
    verdict digest is recomputable and the decision is re-derivable from the
    cited screens -- verify_verdict);
  * the verdict is anchored (Merkle root) so its existence has a trustless
    timestamp -- the "screened BEFORE paid" ordering primitive;
  * the receipt is BOUND to that exact verdict (verify_screening) -- the bound
    verdict cannot be swapped for another.

This is "consume to prove", not "ingest to compete": Traceipt never analyzes the
chain, it just makes whatever a risk engine decided provable.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from traceipt.canonical import GENESIS_HASH
from traceipt.ledger import Ledger
from traceipt.merkle import verify_inclusion
from traceipt.schema import build_receipt
from traceipt.screening import build_screening, verdict_digest, verify_screening
from traceipt.screening_providers import (
    CHAINALYSIS_DEMO_SANCTIONED, build_verdict, providers_from_env, screen,
    verify_verdict,
)
from traceipt.service import App, utc_now
from traceipt.settlement import make_verifier
from traceipt.signing import Signer, verify_envelope

# A clean address (this project's own configured pay_to) alongside the
# documentation sample, so the demo shows both a GO and a STOP path.
CLEAN = "0x3ec5e0ec1e1cb8e2afa36f3b40eed9057d9004e1"


def _line(ch="-"):
    print(ch * 68)


def run(addresses):
    providers = providers_from_env(os.environ)
    provider_names = ", ".join(p.name for p in providers)
    print(f"screening providers: {provider_names}")
    print("(set CHAINALYSIS_API_KEY / TRM_API_KEY to add real feeds)\n")

    tmp = tempfile.mkdtemp()
    signer = Signer.generate()
    app = App(signer=signer, ledger=Ledger(os.path.join(tmp, "demo.db")),
              verifier=make_verifier("mock", "base-sepolia", ""), gate="off",
              price_base_units="2000", pay_to=CLEAN, seller_id="demo.traceipt",
              chain="base-sepolia", base_url="http://demo")
    jwks = {"keys": [signer.jwk()]}
    seq = 0

    for address in addresses:
        _line("=")
        print(f"ADDRESS  {address}")
        _line("=")

        # 1. SCREEN ---------------------------------------------------------
        results = screen(address, providers)
        for r in results:
            state = ("LISTED" if r.listed else "clear") if r.checked else "NOT-RUN"
            extra = f"  {list(r.matches)}" if r.matches else ""
            extra += f"  ({r.error})" if r.error else ""
            print(f"  screen[{r.provider}] -> {state}{extra}")

        # 2. VERDICT (one canonical object -> one digest) -------------------
        verdict = build_verdict(address, results, decided_at=utc_now())
        digest = verdict_digest(verdict)
        ok, problems = verify_verdict(verdict)
        print(f"\n  verdict decision : {verdict['decision']}")
        print(f"  verdict digest   : {digest}")
        print(f"  decision re-derivable from evidence: {ok}"
              + ("" if ok else f"  {problems}"))

        # 3. ANCHOR the verdict through the neutral layer -------------------
        code, out = app.issue_attestation(
            {"hash": digest, "type": "sanctions-verdict",
             "ref": f"policy:{verdict['policy']}"})
        assert code in (200, 201), out
        att_id = out["attestation"]["attestation_id"]
        anchor = app.ledger.create_anchor(utc_now())
        proof = app.ledger.attestation_inclusion(att_id)
        proof_ok = verify_inclusion(
            proof["leaf_index"], proof["tree_size"], proof["leaf_data"].encode(),
            [bytes.fromhex(h) for h in proof["audit_path"]],
            bytes.fromhex(proof["root"]))
        print(f"\n  anchored as      : {att_id}")
        print(f"  merkle root      : sha256:{anchor['root'][:32]}...")
        print(f"  inclusion proof verifies OFFLINE: {proof_ok}")

        # 4. BIND the verdict into a compliance-bound receipt ---------------
        seq += 1
        settlement = {"chain": "base-sepolia", "tx_hash": "0x" + f"{seq:02x}" * 32,
                      "asset": "USDC",
                      "asset_contract": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
                      "amount_base_units": "2000", "payer": CLEAN, "payee": CLEAN,
                      "verification_method": "mock", "verified": True}
        commerce = {
            "resource": "https://api.traceipt.xyz/v1/demo",
            "description": "compliance-bound machine payment",
            "quoted_amount_base_units": "2000",
            "screening": build_screening(
                verdict, decision=verdict["decision"], screener=provider_names,
                decided_at=verdict["decided_at"],
                attestation_ref=f"http://demo/attest/{att_id}"),
        }
        payload = build_receipt(
            seller_id="demo.traceipt", sequence=seq, prev_receipt_hash=GENESIS_HASH,
            issued_at=utc_now(), settlement=settlement, commerce=commerce)
        envelope = signer.sign_envelope(payload)
        verify_envelope(envelope, jwks)  # signature is real; raises if not
        bind_ok, bind_problems = verify_screening(payload, verdict)
        print(f"\n  receipt          : {payload['receipt_id']}")
        print(f"  bound to verdict : {bind_ok}"
              + ("" if bind_ok else f"  {bind_problems}"))

        # tamper check: ANY change to the verdict must break the binding.
        # (Mutate the subject so the digest always differs, whatever the
        # decision was.)
        tampered = {**verdict, "subject": {"address": "0x" + "de" * 20}}
        swapped_ok, _ = verify_screening(payload, tampered)
        print(f"  swap-a-verdict rejected: {not swapped_ok}")
        print()


if __name__ == "__main__":
    args = sys.argv[1:] or [CHAINALYSIS_DEMO_SANCTIONED, CLEAN]
    run(args)
