import { describe, expect, it, vi } from "vitest";
vi.mock("openclaw/plugin-sdk/plugin-entry", () => ({ definePluginEntry: (e: unknown) => e }));
import { handleBeforeToolCall, resolveConfig } from "./index.ts";

const WARM = "0x02c2fcafce36b4aadb39625866bc6b1699d83043";
const ATTACKER = "0x9999999999999999999999999999999999999999";
const SANCTIONED = "0x0330070fd38ec3bb94f58fa55d40368271e9e54a";
const UNKNOWN = "0x0000000000000000000000000000000000000001";
const silent = { info() {}, warn() {}, error() {}, debug() {} };
const cfg = resolveConfig({});
const call = (params: any, toolName = "x402_pay") =>
  handleBeforeToolCall({ toolName, toolCallId: "t", params }, cfg, silent);
const b64 = (o: any) => Buffer.from(JSON.stringify(o)).toString("base64");
// "Verdict says pay WARM, signature actually pays ATTACKER"
const SWAP_XPAYMENT = b64({
  x402Version: 1, scheme: "exact", network: "base",
  payload: { signature: "0x" + "ab".repeat(65),
    authorization: { from: "0x1111111111111111111111111111111111111111",
      to: ATTACKER, value: "14000", validAfter: "0", validBefore: "9999999999", nonce: "0x01" } },
});

describe("LIVE eval scorecard", () => {
  it("E1 warm payee at fair price -> GO (allow)", async () => {
    expect(await call({ payTo: WARM, amount: "0.014" })).toBeUndefined();
  }, 180000);
  it("E2 warm payee gouged 3.5x -> HOLD (block, price reason)", async () => {
    const r = await call({ payTo: WARM, amount: "0.05" });
    expect(r).toMatchObject({ block: true });
    expect(r.blockReason).toMatch(/HOLD/);
    expect(r.blockReason).toMatch(/median|price/i);
  }, 60000);
  it("E3 unknown cold-start payee -> HOLD (block)", async () => {
    const r = await call({ payTo: UNKNOWN, amount: "0.014" });
    expect(r).toMatchObject({ block: true });
    expect(r.blockReason).toMatch(/HOLD/);
  }, 60000);
  it("E4 sanctioned payee -> STOP (block)", async () => {
    const r = await call({ payTo: SANCTIONED, amount: "0.014" });
    expect(r).toMatchObject({ block: true });
    expect(r.blockReason).toMatch(/STOP/);
    expect(r.blockReason).toMatch(/sanctions/i);
  }, 60000);
  it("E5 SWAP ATTACK: claim pays warm payee, signature pays attacker -> STOP (block)", async () => {
    const r = await call({ payTo: WARM, amount: "0.014", payment: SWAP_XPAYMENT });
    expect(r).toMatchObject({ block: true });
    expect(r.blockReason).toMatch(/STOP/);
    console.log("E5 reason:", r.blockReason.split("\n").slice(0, 4).join("\n"));
  }, 60000);
  it("E6 case-variant key (payto) still gated live -> blocked for unknown payee", async () => {
    const r = await call({ payto: UNKNOWN, amount: "0.014" }, "fetch_paid_api");
    expect(r).toMatchObject({ block: true });
  }, 60000);
});
