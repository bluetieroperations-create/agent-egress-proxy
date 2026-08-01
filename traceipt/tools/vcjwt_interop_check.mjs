// Independent interop check for Traceipt's VC-JWT (W3C VC-JOSE-COSE) output.
//
// Verifies a Traceipt-issued compact VC-JWT with `jose` -- the de-facto JOSE
// reference library for JS -- independent of the Python code that produced it.
//
// Usage:
//   cd traceipt && npm i jose
//   # write {"jwt": "...", "jwk": {kty,crv,x}} to vcjwt.json, then:
//   node tools/vcjwt_interop_check.mjs vcjwt.json
//
// A success line means Traceipt's JWS header, payload, and EdDSA signature are
// all valid VC-JOSE-COSE as read by an independent JOSE implementation.
import { compactVerify, importJWK } from 'jose';
import { readFileSync } from 'fs';

const path = process.argv[2];
if (!path) { console.error('usage: node tools/vcjwt_interop_check.mjs <vcjwt.json>'); process.exit(2); }
const { jwt, jwk } = JSON.parse(readFileSync(path, 'utf8'));
const key = await importJWK(jwk, 'EdDSA');
try {
  const { payload, protectedHeader } = await compactVerify(jwt, key);
  const cred = JSON.parse(new TextDecoder().decode(payload));
  console.log(`${cred.type?.[1] ?? 'VC'}: independent jose verify -> OK ` +
              `(typ=${protectedHeader.typ}, alg=${protectedHeader.alg})`);
  process.exit(0);
} catch (e) {
  console.log('jose verify -> FAILED:', e.message);
  process.exit(1);
}
