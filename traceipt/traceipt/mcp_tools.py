"""
Traceipt as an MCP tool surface -- a DISTRIBUTION play.

Agents discover capabilities through MCP. This module exposes Traceipt's
NEUTRAL, no-payment value -- independent verification of receipts, credentials,
compliance bindings, and on-chain anchors -- as plain functions that the FastMCP
server (mcp_server.py) wraps as tools. Getting listed in the MCP registry puts
"verify a payment receipt" where agents already look for it, in a category that
is nearly empty today.

Why verification (and not paid issuance) is the tool surface: verification is
the read-only public good, it needs no keys or wallet, and it is exactly the
job an agent has when it receives a receipt it did not create. It also shows off
the differentiator -- an agent can verify a Traceipt receipt WITHOUT trusting
Traceipt's server, because W3C credentials carry their own did:key signature and
Merkle proofs verify offline against a published root.

Pure logic + stdlib (only urllib for fetching public material: JWKS, DID docs,
Merkle proofs). No `mcp` dependency, so every tool is unit-testable without the
SDK. Inputs accept either a JSON string (how an agent passes them) or a dict
(how tests pass them).
"""
from __future__ import annotations

import json
import urllib.request

from .bundle import verify_lifecycle, verify_presentation
from .merkle import verify_inclusion
from .screening import verify_screening
from .screening_providers import verify_verdict
from .signing import verify_envelope
from .vc import verify_jwt, verify_vc

_UA = {"User-Agent": "Traceipt-MCP/0.1", "Accept": "application/json"}


def _as_obj(x):
    """Accept a dict as-is, or parse a JSON string into one. Returns None if it
    is neither (e.g. a bare JWT string)."""
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        try:
            v = json.loads(x)
            return v if isinstance(v, dict) else None
        except Exception:  # noqa: BLE001
            return None
    return None


def _get_json(url: str, timeout: float = 20.0):
    with urllib.request.urlopen(
            urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _origin(url: str) -> str:
    # scheme://host[:port] from a URL, without a path
    rest = url.split("://", 1)
    if len(rest) != 2:
        return url.rstrip("/")
    scheme, tail = rest
    return f"{scheme}://{tail.split('/', 1)[0]}"


def verify_credential(document) -> dict:
    """Verify any W3C Verifiable Credential (Data Integrity or VC-JWT), a
    VerifiablePresentation, or a Traceipt lifecycle bundle
    {receipt_vc, verdict_vc, attestation_vc}. Self-certifying: checks each
    credential's own did:key/did:web signature, so it verifies credentials from
    ANY issuer, not just Traceipt. Returns {ok, kind, ...}."""
    # VC-JWT passed as a bare token string?
    if isinstance(document, str) and _as_obj(document) is None:
        tok = document.strip()
        if tok.count(".") == 2:
            payload = verify_jwt(tok)
            return {"ok": payload is not None, "kind": "credential-jwt",
                    "issuer": (payload or {}).get("issuer"),
                    "types": (payload or {}).get("type")}
        return {"ok": False, "error": "not a JSON document or a VC-JWT"}

    body = _as_obj(document)
    if body is None:
        return {"ok": False, "error": "expected a JSON VC/VP/bundle or a VC-JWT"}

    # Enveloped VC-JWT: {"jwt": "..."} or an EnvelopedVerifiableCredential.
    tok = None
    if isinstance(body.get("jwt"), str):
        tok = body["jwt"]
    elif "EnvelopedVerifiableCredential" in (body.get("type") or []):
        vid = body.get("id", "")
        if isinstance(vid, str) and vid.startswith("data:") and "," in vid:
            tok = vid.split(",", 1)[1]
    if tok is not None:
        payload = verify_jwt(tok)
        return {"ok": payload is not None, "kind": "credential-jwt",
                "issuer": (payload or {}).get("issuer"),
                "types": (payload or {}).get("type")}

    if any(k in body for k in ("receipt_vc", "verdict_vc", "attestation_vc")):
        ok, report = verify_lifecycle(
            receipt_vc=body.get("receipt_vc"), verdict_vc=body.get("verdict_vc"),
            attestation_vc=body.get("attestation_vc"),
            settlement_time=body.get("settlement_time"),
            expected_issuers=body.get("expected_issuers"))
        return {"ok": ok, "kind": "lifecycle", **report}

    types = body.get("type") or []
    if "VerifiablePresentation" in types:
        ok, report = verify_presentation(body)
        return {"ok": ok, "kind": "presentation", **report}
    if "proof" in body:
        return {"ok": verify_vc(body), "kind": "credential",
                "issuer": body.get("issuer"), "types": types}
    return {"ok": False, "error": "unrecognized document: expected a VC (with a "
            "proof), a VerifiablePresentation, a bundle, or a VC-JWT"}


def verify_receipt_envelope(envelope, jwks_url: str) -> dict:
    """Verify a Traceipt signed-receipt ENVELOPE (protected/payload/signature)
    against the issuer's published JWKS (fetched from jwks_url). Returns
    {ok, receipt_id, seller_id, ...} or {ok: False, error}."""
    env = _as_obj(envelope)
    if env is None:
        return {"ok": False, "error": "envelope must be a JSON object"}
    try:
        jwks = _get_json(jwks_url)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not fetch JWKS: {e}"}
    try:
        payload = verify_envelope(env, jwks)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"signature/verify failed: {e}"}
    out = {"ok": True, "kind": "receipt-envelope",
           "receipt_id": payload.get("receipt_id"),
           "seller_id": payload.get("seller_id"),
           "sequence": payload.get("sequence")}
    sc = (payload.get("commerce") or {}).get("screening")
    if isinstance(sc, dict):
        out["compliance_bound"] = True
        out["verdict_hash"] = sc.get("verdict_hash")
    return out


def verify_compliance_binding(receipt, verdict) -> dict:
    """Verify a compliance-bound receipt is bound to the ACTUAL verdict object
    (the receipt's verdict_hash == digest of this verdict). If the verdict is in
    Traceipt's screening-provider shape, also re-derive its GO/STOP/REVIEW
    decision from the cited screens. Returns {ok, ...}."""
    rec = _as_obj(receipt)
    v = _as_obj(verdict)
    if rec is None or v is None:
        return {"ok": False, "error": "receipt and verdict must be JSON objects"}
    payload = rec.get("payload") if "payload" in rec else rec
    ok_b, problems = verify_screening(payload, v)
    out = {"ok": ok_b, "binding_ok": ok_b, "binding_problems": problems}
    if "screens" in v:  # our screening-provider verdict shape
        ok_v, prob_v = verify_verdict(v)
        out["ok"] = ok_b and ok_v
        out["decision_recomputable"] = ok_v
        out["decision_problems"] = prob_v
    return out


def verify_onchain_anchor(base_url: str, attestation_id: str) -> dict:
    """Fetch a Traceipt attestation's Merkle inclusion proof, verify it OFFLINE
    against the anchor root, and report the on-chain anchor transaction (if the
    root was published). Returns {ok, leaf, root, onchain_tx, ...}."""
    base = base_url.rstrip("/")
    try:
        proof = _get_json(f"{base}/attest/{attestation_id}/proof")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not fetch proof: {e}"}
    if not isinstance(proof, dict) or "audit_path" not in proof:
        return {"ok": False, "error": proof.get("error", "no proof (not anchored yet?)")
                if isinstance(proof, dict) else "bad proof response"}
    ok = verify_inclusion(
        proof["leaf_index"], proof["tree_size"], proof["leaf_data"].encode(),
        [bytes.fromhex(h) for h in proof["audit_path"]],
        bytes.fromhex(proof["root"]))
    out = {"ok": ok, "leaf": proof["leaf_data"], "root": proof["root"],
           "tree_size": proof["tree_size"]}
    try:  # attach the on-chain tx for this root, if published
        anchors = _get_json(f"{base}/anchors")
        lst = anchors if isinstance(anchors, list) else anchors.get("anchors", [])
        match = [a for a in lst if a.get("root") == proof["root"]]
        if match:
            out["onchain_tx"] = match[0].get("onchain_tx")
            out["onchain_network"] = match[0].get("onchain_network")
    except Exception:  # noqa: BLE001
        pass
    return out


def fetch_and_verify(url: str) -> dict:
    """Fetch a receipt/credential from a URL (e.g. a Traceipt /receipts/{id}/vc
    or a stored envelope) and verify it. Auto-detects the format."""
    try:
        doc = _get_json(url)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not fetch {url}: {e}"}
    if isinstance(doc, dict) and {"protected", "payload", "signature"} <= set(doc):
        return verify_receipt_envelope(doc, f"{_origin(url)}/jwks.json")
    return verify_credential(doc)
