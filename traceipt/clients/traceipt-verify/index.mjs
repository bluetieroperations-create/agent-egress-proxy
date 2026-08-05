// traceipt-verify — verify a Traceipt receipt independently, in Node or the browser.
//
// Zero runtime dependencies: uses the platform WebCrypto (globalThis.crypto) and
// fetch. A receipt is verifiable against the public chain + the issuer's JWKS
// with no call to the Traceipt server — that independence is the whole point.
//
//   import { verifyReceipt } from "traceipt-verify";
//   const r = await verifyReceipt(doc, { rpcUrl: "https://mainnet.base.org", jwks });
//   // r.ok, r.checks: [{ name, ok, detail }]
//
// `doc` may be a raw inclusion proof, a /attest 201 body, the payer client's
// saved run, or a signed receipt envelope ({ protected, payload, signature }).

const MARKER = "54524143454950542d414e43484f5201"; // "TRACEIPT-ANCHOR\x01"
const RPC_DEFAULTS = {
  base: "https://mainnet.base.org",
  "base-sepolia": "https://sepolia.base.org",
};
const enc = new TextEncoder();
const subtle = () => (globalThis.crypto && globalThis.crypto.subtle);

// ---- primitives ------------------------------------------------------------

async function sha256(bytes) {
  return new Uint8Array(await subtle().digest("SHA-256", bytes));
}
function hex(u8) {
  return [...u8].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function fromHex(h) {
  h = h.startsWith("0x") ? h.slice(2) : h;
  const a = new Uint8Array(h.length / 2);
  for (let i = 0; i < a.length; i++) a[i] = parseInt(h.substr(i * 2, 2), 16);
  return a;
}
function cat(...arrs) {
  let n = 0;
  for (const a of arrs) n += a.length;
  const o = new Uint8Array(n);
  let k = 0;
  for (const a of arrs) { o.set(a, k); k += a.length; }
  return o;
}
function b64urlToBytes(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  const bin = atob(s);
  const a = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i);
  return a;
}

// RFC 6962: leaf = SHA256(0x00 || data); node = SHA256(0x01 || left || right)
async function leafHash(data) { return sha256(cat(new Uint8Array([0]), data)); }
async function nodeHash(l, r) { return sha256(cat(new Uint8Array([1]), l, r)); }

// canonical JSON == Python json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False)
export function canon(o) {
  if (o === null || typeof o !== "object") return JSON.stringify(o);
  if (Array.isArray(o)) return "[" + o.map(canon).join(",") + "]";
  return "{" + Object.keys(o).sort().map((k) => JSON.stringify(k) + ":" + canon(o[k])).join(",") + "}";
}

// ---- Merkle inclusion (RFC 6962 §2.1.1) ------------------------------------

export async function recomputeRoot(m, n, leafData, proof) {
  let fn = m, sn = n - 1, r = await leafHash(leafData);
  for (const sibHex of proof) {
    const sib = fromHex(sibHex);
    if (fn === sn || (fn & 1)) {
      r = await nodeHash(sib, r);
      if (!(fn & 1)) { while (fn !== 0 && !(fn & 1)) { fn >>= 1; sn >>= 1; } }
    } else {
      r = await nodeHash(r, sib);
    }
    fn >>= 1; sn >>= 1;
  }
  return hex(r);
}

export async function verifyInclusion(proof) {
  const root = (proof.root || "").toLowerCase();
  const got = await recomputeRoot(
    proof.leaf_index | 0, proof.tree_size || 1,
    enc.encode(proof.leaf_data), proof.audit_path || []);
  return got === root;
}

// ---- on-chain anchor -------------------------------------------------------

async function rpc(url, method, params, fetchImpl) {
  const f = fetchImpl || globalThis.fetch;
  const res = await f(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  const j = await res.json();
  if (j.error) throw new Error(JSON.stringify(j.error));
  return j.result;
}

export async function verifyAnchor(proof, opts = {}) {
  const net = proof.onchain_network || "base";
  const url = opts.rpcUrl || RPC_DEFAULTS[net] || RPC_DEFAULTS.base;
  const tx = proof.onchain_tx;
  const root = (proof.root || "").toLowerCase();
  const txo = await rpc(url, "eth_getTransactionByHash", [tx], opts.fetch);
  if (!txo) return { ok: false, detail: `tx not found on ${net}` };
  const inp = (txo.input || "").slice(2);
  const carries = inp.startsWith(MARKER) && inp.slice(MARKER.length) === root;
  const block = txo.blockNumber ? parseInt(txo.blockNumber, 16) : null;
  return {
    ok: carries,
    detail: carries ? `${net} block ${block} — calldata carries the exact root`
                    : "tx calldata does NOT carry this root",
  };
}

// ---- Ed25519 envelope signature (+ note any post-quantum signature) --------

export async function verifyEnvelopeSignature(envelope, jwks) {
  const kid = envelope.protected && envelope.protected.kid;
  const jwk = (jwks.keys || []).find((k) => k.kid === kid && k.crv === "Ed25519");
  if (!jwk) return { ok: null, detail: `no Ed25519 key kid=${kid} in JWKS` };
  const pub = b64urlToBytes(jwk.x);
  const key = await subtle().importKey("raw", pub, { name: "Ed25519" }, false, ["verify"]);
  const input = enc.encode(canon({ payload: envelope.payload, protected: envelope.protected }));
  const ok = await subtle().verify({ name: "Ed25519" }, key, b64urlToBytes(envelope.signature), input);
  return { ok, detail: ok ? `Ed25519 verified against issuer key kid=${kid}` : "signature FAILED" };
}

// ---- locate the proof in whatever shape the caller passes ------------------

function findProof(d) {
  if (!d || typeof d !== "object") return null;
  if ("root" in d && "leaf_data" in d) return d;
  if (d.proof && typeof d.proof === "object" && "root" in d.proof) return d.proof;
  if (d.response && d.response.proof && "root" in d.response.proof) return d.response.proof;
  return null;
}

// ---- orchestrator ----------------------------------------------------------

export async function verifyReceipt(doc, opts = {}) {
  const checks = [];
  const add = (name, ok, detail) => checks.push({ name, ok, detail: detail || "" });

  const proof = findProof(doc);
  if (proof && proof.leaf_data && proof.root) {
    // verdict binding (optional)
    const verdict = doc && typeof doc.verdict === "object" ? doc.verdict : null;
    if (verdict) {
      const want = "sha256:" + hex(await sha256(enc.encode(canon(verdict))));
      add("verdict_binding", want === proof.leaf_data,
          want === proof.leaf_data ? "digest matches anchored leaf" : `verdict hashes to ${want}`);
    }
    try {
      add("inclusion_proof", await verifyInclusion(proof),
          `root ${proof.root.slice(0, 16)}… recomputed`);
    } catch (e) { add("inclusion_proof", false, "error: " + e.message); }

    if (opts.offline) add("onchain_anchor", null, "skipped (offline)");
    else if (!proof.onchain_tx) add("onchain_anchor", null, "no onchain_tx (not published)");
    else {
      try { const a = await verifyAnchor(proof, opts); add("onchain_anchor", a.ok, a.detail); }
      catch (e) { add("onchain_anchor", false, "RPC error: " + e.message); }
    }
  }

  const isEnv = doc && doc.protected && doc.payload && doc.signature;
  if (isEnv) {
    if (opts.jwks) {
      try { const s = await verifyEnvelopeSignature(doc, opts.jwks); add("signature", s.ok, s.detail); }
      catch (e) { add("signature", false, e.message); }
    } else {
      add("signature", null, "skipped — pass opts.jwks to verify");
    }
    // A hybrid post-quantum signature (ML-DSA) may accompany the Ed25519 one.
    // This lib verifies the classical path; the PQ signature is a forward hedge
    // and is reported as present-but-unverified here (verify it with the Python
    // verifier / an ML-DSA lib).
    if (doc.protected.pq_alg && doc.pq_signature) {
      add("pq_signature", null,
          `post-quantum ${doc.protected.pq_alg} signature present (not checked by this lib)`);
    }
  }

  if (!proof && !isEnv) {
    return { ok: false, checks: [{ name: "input", ok: false, detail: "unrecognized document" }] };
  }
  const ok = checks.every((c) => c.ok !== false);
  return { ok, checks };
}

export default { verifyReceipt, verifyInclusion, verifyAnchor, verifyEnvelopeSignature, recomputeRoot, canon };
