"""
W3C Verifiable Credential output -- ride the standard instead of competing with it.

Google's AP2 (Agent Payments Protocol) and its A2A x402 crypto extension carry
signed W3C Verifiable Credentials (Intent / Cart / Payment Mandates -- the
payment INSTRUCTIONS). AP2 does not define the post-settlement, on-chain-VERIFIED
receipt. That is exactly what Traceipt issues, so this module re-expresses a
Traceipt receipt as a standard W3C VC 2.0 `PaymentReceiptCredential` that drops
into any AP2 / VC verifier alongside those Mandates.

Cryptosuite: `eddsa-jcs-2022` (W3C VC Data Integrity EdDSA). It canonicalizes
with JCS (RFC 8785) -- which is exactly what canonical.py already produces -- and
signs with Ed25519, so the VC proof reuses Traceipt's existing key and
canonicalization with no JSON-LD/RDF machinery. The issuer key is expressed as a
`did:key` so the credential verifies fully offline (no JWKS fetch), true to
Traceipt's offline-verifiable ethos.

Pure + stdlib except the Ed25519 primitive (cryptography, already a dependency).
"""
from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .canonical import canonical_json

VC_CONTEXT = "https://www.w3.org/ns/credentials/v2"
TRACEIPT_CONTEXT = "https://traceipt.xyz/credentials/v1"
RECEIPT_TYPE = "X402PaymentReceiptCredential"
VERDICT_TYPE = "RiskVerdictCredential"
ATTESTATION_TYPE = "AnchoredAttestationCredential"
CRYPTOSUITE = "eddsa-jcs-2022"
# multicodec prefix for an Ed25519 public key (varint 0xed 0x01).
_ED25519_MULTICODEC = b"\xed\x01"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# --- multibase base58-btc (the "z" multibase used by did:key + proofValue) ---
def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + out


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


def multibase58(data: bytes) -> str:
    return "z" + b58encode(data)


def unmultibase58(s: str) -> bytes:
    if not s.startswith("z"):
        raise ValueError("not base58-btc multibase (missing 'z' prefix)")
    return b58decode(s[1:])


# --- did:key for an Ed25519 public key ---------------------------------------
def did_key(pubkey: bytes) -> tuple[str, str]:
    """(did, verificationMethod) for a raw 32-byte Ed25519 public key."""
    mb = multibase58(_ED25519_MULTICODEC + pubkey)
    did = "did:key:" + mb
    return did, f"{did}#{mb}"


def pubkey_from_verification_method(vm: str) -> bytes:
    """Extract the 32-byte Ed25519 public key from a did:key verificationMethod."""
    frag = vm.split("#", 1)[-1]
    raw = unmultibase58(frag)
    if raw[:2] != _ED25519_MULTICODEC:
        raise ValueError("verificationMethod is not an Ed25519 did:key")
    key = raw[2:]
    if len(key) != 32:
        raise ValueError("bad Ed25519 key length in did:key")
    return key


def did_web_document(host: str, active_pubkey: bytes, retired_pubkeys=None) -> dict:
    """The DID document served at https://<host>/.well-known/did.json, giving
    Traceipt a resolvable, human-meaningful `did:web:<host>` identity (for
    enterprise / AP2 verifiers that prefer it over an opaque did:key).

    `alsoKnownAs` links it to the SAME key's did:key, so a verifier holding a
    did:key-issued VC can resolve this document to confirm the key really
    belongs to <host> -- while offline verification against did:key still works
    unchanged (verify_vc stays did:key-only, which is spoof-proof because a
    did:key IS its key; a did:web needs resolution to be trustworthy)."""
    did = "did:web:" + host
    seen, vms = set(), []
    for pub in [active_pubkey] + list(retired_pubkeys or []):
        if pub in seen:
            continue
        seen.add(pub)
        mb = multibase58(_ED25519_MULTICODEC + pub)
        vms.append({"id": f"{did}#{mb}", "type": "Multikey",
                    "controller": did, "publicKeyMultibase": mb})
    return {
        "@context": ["https://www.w3.org/ns/did/v1",
                     "https://w3id.org/security/multikey/v1"],
        "id": did,
        "alsoKnownAs": [did_key(active_pubkey)[0]],
        "verificationMethod": vms,
        "assertionMethod": [vm["id"] for vm in vms],
        "authentication": [vms[0]["id"]],
    }


# --- eddsa-jcs-2022 proof (W3C VC Data Integrity) ----------------------------
def _proof_config(proof: dict, doc_context) -> dict:
    """The proof options used for hashing: everything but proofValue, with the
    document's @context folded in (defeats context substitution)."""
    pc = {k: v for k, v in proof.items() if k != "proofValue"}
    pc["@context"] = doc_context
    return pc


def _hash_data(document_no_proof: dict, proof_no_value: dict) -> bytes:
    """eddsa-jcs-2022 hashData = SHA256(JCS(proofConfig)) || SHA256(JCS(doc))."""
    pc = _proof_config(proof_no_value, document_no_proof["@context"])
    proof_hash = hashlib.sha256(canonical_json(pc)).digest()
    doc_hash = hashlib.sha256(canonical_json(document_no_proof)).digest()
    return proof_hash + doc_hash


def add_proof(credential: dict, signer, created: str) -> dict:
    """Attach an eddsa-jcs-2022 data-integrity proof, returning the secured VC.
    `signer` must expose sign_raw(bytes) and public_bytes()."""
    _did, vm = did_key(signer.public_bytes())
    proof = {
        "type": "DataIntegrityProof",
        "cryptosuite": CRYPTOSUITE,
        "created": created,
        "verificationMethod": vm,
        "proofPurpose": "assertionMethod",
    }
    signature = signer.sign_raw(_hash_data(credential, proof))
    proof["proofValue"] = multibase58(signature)
    return {**credential, "proof": proof}


def _issuer_id(secured: dict):
    """The issuer DID/URI, whether `issuer` is a string or an object with id."""
    iss = secured.get("issuer")
    if isinstance(iss, str):
        return iss
    if isinstance(iss, dict):
        return iss.get("id")
    return None


def verify_vc(secured: dict, *, expected_issuer: str | None = None) -> bool:
    """Verify a secured VC's eddsa-jcs-2022 proof against the did:key in its
    verificationMethod. Fully offline. Never raises -- returns False on any
    malformed input or bad signature.

    Also binds AUTHENTICITY: the proof must be an assertionMethod proof, and the
    verificationMethod's controller (the DID before '#') MUST equal the credential
    issuer. Without that, anyone could CLAIM any issuer and sign with their own
    key -- the signature would still 'verify' against their key. This check works
    for did:key (issuer == the key's own did) and did:web (issuer == the DID the
    key lives under). `expected_issuer`, when given, additionally pins the issuer
    the caller trusts."""
    try:
        proof = secured["proof"]
        if proof.get("type") != "DataIntegrityProof" or \
                proof.get("cryptosuite") != CRYPTOSUITE:
            return False
        if proof.get("proofPurpose") != "assertionMethod":
            return False
        vm = proof["verificationMethod"]
        issuer = _issuer_id(secured)
        # Controller binding: the signing key's DID must be the issuer.
        if not issuer or issuer != vm.split("#", 1)[0]:
            return False
        if expected_issuer is not None and issuer != expected_issuer:
            return False
        document = {k: v for k, v in secured.items() if k != "proof"}
        sig = unmultibase58(proof["proofValue"])
        pub = pubkey_from_verification_method(vm)
        Ed25519PublicKey.from_public_bytes(pub).verify(
            sig, _hash_data(document, proof))
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False


# --- Traceipt receipt -> W3C VC ----------------------------------------------
def receipt_to_credential(payload: dict, issuer_did: str, base_url: str) -> dict:
    """Map a Traceipt receipt payload to an UNSIGNED W3C VC 2.0 credential."""
    s = payload["settlement"]
    subject = {
        "type": "X402Payment",
        "receiptId": payload["receipt_id"],
        "seller": payload["seller_id"],
        "sequence": payload["sequence"],
        "settlement": {
            "chain": s["chain"],
            "txHash": s["tx_hash"],
            "asset": s.get("asset"),
            "assetContract": s.get("asset_contract"),
            "amountBaseUnits": s["amount_base_units"],
            "payer": s["payer"],
            "payee": s["payee"],
            "verified": s["verified"],
            "verificationMethod": s["verification_method"],
        },
    }
    if payload.get("commerce"):
        subject["commerce"] = payload["commerce"]
    if payload.get("kind"):
        subject["kind"] = payload["kind"]
    disclosure = payload.get("disclosure")
    if isinstance(disclosure, dict) and disclosure.get("fields_root"):
        # link to the selective-disclosure field commitment
        subject["fieldsRoot"] = disclosure["fields_root"]
    return {
        "@context": [VC_CONTEXT, TRACEIPT_CONTEXT],
        "type": ["VerifiableCredential", RECEIPT_TYPE],
        "id": f"{base_url.rstrip('/')}/receipts/{payload['receipt_id']}",
        "issuer": issuer_did,
        "validFrom": payload["issued_at"],
        "credentialSubject": subject,
    }


def receipt_vc(payload: dict, signer, base_url: str, created: str) -> dict:
    """Full path: Traceipt receipt payload -> signed W3C VC (eddsa-jcs-2022)."""
    issuer_did, _vm = did_key(signer.public_bytes())
    credential = receipt_to_credential(payload, issuer_did, base_url)
    return add_proof(credential, signer, created)


# --- verdict -> W3C VC (the pre-payment risk credential AP2 lacks) -----------
def verdict_to_credential(verdict_obj, issuer_did: str, created: str, *,
                          decision=None, screener=None, subject_id=None) -> dict:
    """Map a pre-payment risk verdict to an UNSIGNED W3C VC. Hash-first: the
    subject carries the verdict's digest (byte-identical to Black_Wall's
    traceipt_attest.verdict_digest and to a receipt's screening.verdict_hash),
    plus the short decision, so the verdict's contents can stay off-credential
    and floats (e.g. a score) never reach the JCS canonicalizer. This is the
    risk-credential slot that sits BEFORE AP2's Payment Mandate; whoever screens
    (Black_Wall) signs it with their own key."""
    from .screening import verdict_digest
    subject = {"type": "PaymentRiskVerdict", "verdictHash": verdict_digest(verdict_obj)}
    if subject_id is not None:
        subject["id"] = subject_id
    if decision is not None:
        subject["decision"] = decision
    if screener is not None:
        subject["screener"] = screener
    return {
        "@context": [VC_CONTEXT, TRACEIPT_CONTEXT],
        "type": ["VerifiableCredential", VERDICT_TYPE],
        "issuer": issuer_did,
        "validFrom": created,
        "credentialSubject": subject,
    }


def verdict_vc(verdict_obj, signer, created: str, *, decision=None,
               screener=None, subject_id=None) -> dict:
    """Full path: a risk verdict -> signed W3C RiskVerdictCredential. Reusable
    machinery -- Black_Wall calls this with its OWN signer so the issuer did:key
    is Black_Wall's, and the verdictHash matches what it anchors via /attest and
    what a Traceipt receipt binds via commerce.screening."""
    issuer_did, _vm = did_key(signer.public_bytes())
    cred = verdict_to_credential(verdict_obj, issuer_did, created,
                                 decision=decision, screener=screener,
                                 subject_id=subject_id)
    return add_proof(cred, signer, created)


# --- Traceipt's anchoring attestation -> W3C VC ------------------------------
def attestation_to_credential(rec: dict, inclusion, issuer_did: str,
                              base_url: str) -> dict:
    """Traceipt's own notary assertion, as a VC: 'I anchored digest D (type T)
    at time T0, provable against Merkle root R'. This is what Traceipt can
    honestly attest about a verdict Black_Wall submitted -- it never sees the
    verdict, only its digest -- so it gives the trustless TIMESTAMP that turns
    'screened before paid' from self-asserted into provable."""
    att_id = rec["attestation_id"]
    subject = {
        "type": "AnchoredAttestation",
        "attestationId": att_id,
        "digest": rec["hash"],
        "attestationType": rec.get("type"),
        "submittedAt": rec.get("submitted_at"),
        "anchored": inclusion is not None,
        "proofUrl": f"{base_url.rstrip('/')}/attest/{att_id}/proof",
    }
    if inclusion is not None:
        subject["anchor"] = {
            "root": inclusion.get("root"),
            "onchainTx": inclusion.get("onchain_tx"),
            "anchoredAt": inclusion.get("anchored_at"),
        }
    return {
        "@context": [VC_CONTEXT, TRACEIPT_CONTEXT],
        "type": ["VerifiableCredential", ATTESTATION_TYPE],
        "id": f"{base_url.rstrip('/')}/attest/{att_id}",
        "issuer": issuer_did,
        "validFrom": rec.get("submitted_at"),
        "credentialSubject": subject,
    }


def attestation_vc(rec: dict, inclusion, signer, base_url: str,
                   created: str) -> dict:
    """Full path: a stored attestation -> signed W3C AnchoredAttestationCredential."""
    issuer_did, _vm = did_key(signer.public_bytes())
    cred = attestation_to_credential(rec, inclusion, issuer_did, base_url)
    return add_proof(cred, signer, created)
