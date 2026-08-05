// node test.mjs — verifies the real mainnet run and a tampered copy.
import assert from "node:assert";
import { verifyReceipt, verifyInclusion, canon, asciiEscape } from "./index.mjs";

const REAL = {
  verdict_digest: "sha256:728c4733c730091d606cfc22368e7787249392fec898ad730f8d59a5396dcace",
  response: {
    proof: {
      leaf_index: 0, tree_size: 1,
      leaf_data: "sha256:728c4733c730091d606cfc22368e7787249392fec898ad730f8d59a5396dcace",
      audit_path: [],
      root: "6d2b25d4f9701dba7d9b6bbe5cfc6a706d7b436a3db09e8c3b8c9cebfbce2985",
      onchain_network: "base",
      onchain_tx: "0xab1c79b60a3ca3386eabc654bf163711140ac17a969e1fa526be8314da38821f",
    },
  },
};

let failures = 0;
const ok = (cond, msg) => { if (!cond) { console.error("FAIL:", msg); failures++; } else console.log("pass:", msg); };

// canonical JSON matches Python (sanity)
ok(canon({ b: 1, a: [2, 3] }) === '{"a":[2,3],"b":1}', "canon sorts keys + compact");

// inclusion proof over the real run (offline, no network)
ok(await verifyInclusion(REAL.response.proof) === true, "real inclusion proof verifies");

// tampered root fails inclusion
const bad = structuredClone(REAL);
bad.response.proof.root = "00" + bad.response.proof.root.slice(2);
ok(await verifyInclusion(bad.response.proof) === false, "tampered root fails inclusion");

// full verify, offline (skips the on-chain RPC)
const r = await verifyReceipt(REAL, { offline: true });
const byName = Object.fromEntries(r.checks.map((c) => [c.name, c.ok]));
ok(byName.inclusion_proof === true, "verifyReceipt: inclusion passes");
ok(byName.onchain_anchor === null, "verifyReceipt: anchor skipped offline");
ok(r.ok === true, "verifyReceipt ok (offline)");

// full verify WITH the chain (needs network; tolerate offline CI)
try {
  const online = await verifyReceipt(REAL, {});
  const on = Object.fromEntries(online.checks.map((c) => [c.name, c.ok]));
  ok(on.onchain_anchor === true, "verifyReceipt: on-chain anchor confirmed on Base mainnet");
  ok(online.ok === true, "verifyReceipt ok (online)");
} catch (e) {
  console.log("skip: on-chain check (no network):", e.message);
}


// Regression: the verdict digest must match Python screening.verdict_digest
// (ensure_ascii=True) even for a non-ASCII entity name — else verdict_binding
// falsely fails. Authoritative value computed by the Python impl.
{
  const enc = new TextEncoder();
  const sha = async b => new Uint8Array(await crypto.subtle.digest("SHA-256", b));
  const hx = u => [...u].map(b=>b.toString(16).padStart(2,"0")).join("");
  const v = {policy:"ofac-sanctions-v1",subject:{address:"0xabc"},decided_at:"2026-08-05T00:00:00Z",decision:"STOP",
    screens:[{provider:"trm",listed:true,checked:true,source:"trm",matches:["Soci\u00e9t\u00e9 G\u00e9n\u00e9rale (SDN)"]}]};
  const got = "sha256:"+hx(await sha(enc.encode(asciiEscape(canon(v)))));
  ok(got === "sha256:84f58c9258500c2b61a7d8a802c1eeeccb215700e5b3224b524d745f9a584771",
     "non-ASCII verdict digest matches Python ensure_ascii=True authoritative");
}

assert.strictEqual(failures, 0, `${failures} failure(s)`);
console.log("\nALL PASS");
