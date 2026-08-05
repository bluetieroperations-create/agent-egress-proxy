#!/usr/bin/env python3
"""Generate an ML-DSA-65 keypair for RECEIPTS_PQ_KEY — standalone, no repo needed.

Use this when you can't run `python -m traceipt.service --gen-pq-key` on the
server (e.g. Render's free tier has no Shell). Runs anywhere with dilithium-py:

    pip install dilithium-py
    python gen_pq_key.py

It prints the packed secret to set as RECEIPTS_PQ_KEY and the public JWK. The
private key stays on this machine — paste ONLY the RECEIPTS_PQ_KEY value into
your host's secret store; never into a chat. Back it up (it's a signing key).

The packed format is byte-identical to traceipt.pqsign.pack_keypair, so the
service unpacks it exactly.
"""
import base64
import hashlib
import json
import struct


def main() -> int:
    try:
        from dilithium_py.ml_dsa import ML_DSA_65
    except ImportError:
        print("Missing dependency. Run:  pip install dilithium-py")
        return 1

    pk, sk = ML_DSA_65.keygen()
    # length-prefixed (>I pk_len || pk || sk), base64 — matches pqsign.pack_keypair
    packed = base64.b64encode(struct.pack(">I", len(pk)) + pk + sk).decode("ascii")
    kid = hashlib.sha256(pk).hexdigest()[:16]
    pub = base64.urlsafe_b64encode(pk).rstrip(b"=").decode("ascii")

    print("# Set this as the RECEIPTS_PQ_KEY secret (single line):\n")
    print(packed)
    print("\n# Public JWK (the service publishes this at /jwks.json):")
    print(json.dumps({"kty": "MLDSA", "alg": "ML-DSA-65", "kid": kid,
                      "pub": pub, "use": "sig"}, indent=2))
    print("\n# Back this up. Paste ONLY the RECEIPTS_PQ_KEY value into your host "
          "secret store — never into a chat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
