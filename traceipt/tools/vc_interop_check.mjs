// Independent interop check for Traceipt's W3C Verifiable Credentials.
//
// Verifies a Traceipt-issued VC's `eddsa-jcs-2022` data-integrity proof using a
// SEPARATE toolchain from the Python code that produced it, so a shared bug
// can't pass unnoticed:
//   * canonicalize  -- the RFC 8785 (JCS) reference implementation (by the JCS
//                      author), independent of traceipt/canonical.py;
//   * bs58          -- base58-btc, independent of traceipt/vc.py;
//   * node WebCrypto -- Ed25519 verify, independent of `cryptography`.
//
// Usage:
//   cd traceipt && npm i canonicalize bs58
//   # write a VC to vc.json, e.g.:
//   python3 -c "from traceipt.service import App; ..."   # GET /receipts/{id}/vc
//   node tools/vc_interop_check.mjs vc.json
//
// eddsa-jcs-2022 hashData = SHA256(JCS(proofConfig)) || SHA256(JCS(doc-no-proof)),
// where proofConfig is the proof minus proofValue, with the document's @context
// folded in. A `true` here means Traceipt's JCS + hashData + did:key + signature
// all match the spec as implemented by independent tools.
import canonicalize from 'canonicalize';
import bs58 from 'bs58';
import { webcrypto as c } from 'crypto';
import { readFileSync } from 'fs';

const path = process.argv[2];
if (!path) { console.error('usage: node tools/vc_interop_check.mjs <vc.json>'); process.exit(2); }
const vc = JSON.parse(readFileSync(path, 'utf8'));
const sha256 = async (b) => new Uint8Array(await c.subtle.digest('SHA-256', b));
const enc = (s) => new TextEncoder().encode(s);

const proofConfig = { ...vc.proof };
delete proofConfig.proofValue;
proofConfig['@context'] = vc['@context'];
const doc = { ...vc }; delete doc.proof;

const hashData = new Uint8Array([
  ...(await sha256(enc(canonicalize(proofConfig)))),
  ...(await sha256(enc(canonicalize(doc)))),
]);
const sig = bs58.decode(vc.proof.proofValue.slice(1));
const keyBytes = bs58.decode(vc.proof.verificationMethod.split('#').pop().slice(1));
if (keyBytes[0] !== 0xed || keyBytes[1] !== 0x01) throw new Error('verificationMethod is not an Ed25519 did:key');
const key = await c.subtle.importKey('raw', keyBytes.slice(2), { name: 'Ed25519' }, false, ['verify']);

const ok = await c.subtle.verify({ name: 'Ed25519' }, key, sig, hashData);
console.log(`${vc.type?.[1] ?? 'VerifiableCredential'}: independent eddsa-jcs-2022 verify -> ${ok}`);
process.exit(ok ? 0 : 1);
