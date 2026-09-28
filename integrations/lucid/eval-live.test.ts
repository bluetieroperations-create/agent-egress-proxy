import { describe, expect, it } from "vitest";
import { wrapFetchWithBlackwall } from "./index.js";

const BASE = "https://blackwall-free.onrender.com";
const SANCTIONED = "0x7F367cC41522cE07553e823bf3be79A889DEbe1B"; // OFAC-listed
const UNKNOWN = "0x" + "e".repeat(40);

function resp402(entries: any[]) {
  return new Response(JSON.stringify({ x402Version: 2, accepts: entries }), {
    status: 402, headers: { "content-type": "application/json" },
  });
}
const e = (payTo: string, atomic = "1000") => ({
  scheme: "exact", network: "eip155:8453", maxAmountRequired: atomic,
  asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", payTo,
});

describe("LIVE against the real Blackwall endpoint", () => {
  it("an OFAC-sanctioned payee is BLOCKED before any signature", async () => {
    const f = wrapFetchWithBlackwall((async () => resp402([e(SANCTIONED)])) as any,
                                     { baseUrl: BASE });
    const r = await f("https://seller.example/api" as any);
    console.log("sanctioned ->", r.status, JSON.stringify(await r.clone().json()));
    expect(r.status).toBe(403);
  }, 90000);

  it("a cold-start unknown payee CONFIRMS (HOLD), not allow", async () => {
    const f = wrapFetchWithBlackwall((async () => resp402([e(UNKNOWN)])) as any,
                                     { baseUrl: BASE });
    const r = await f("https://seller.example/api" as any);
    console.log("unknown ->", r.status, JSON.stringify(await r.clone().json()));
    expect(r.status).toBe(403);
  }, 90000);

  it("MULTI-ENTRY: one clean + one sanctioned blocks the whole challenge", async () => {
    const f = wrapFetchWithBlackwall(
      (async () => resp402([e(UNKNOWN), e(SANCTIONED)])) as any, { baseUrl: BASE });
    const r = await f("https://seller.example/api" as any);
    const body = await r.clone().json();
    console.log("multi ->", r.status, JSON.stringify(body));
    expect(r.status).toBe(403);
    expect(body.verdict).toBe("STOP");
  }, 120000);
});
