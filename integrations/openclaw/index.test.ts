// SPDX-FileCopyrightText: Copyright (c) 2026 BlueTier Operations. All rights reserved.
// SPDX-License-Identifier: MIT

// Tests for the OpenClaw x402 payment gate (index.ts).
//
// MUTATION NOTES appear per-describe: each names the regression that block of
// tests pins, so a mutant that survives points at the exact contract broken.

import { describe, expect, it, vi } from "vitest";

// The plugin entry import is provided by the OpenClaw host at runtime; stub it
// so the module loads under test (same pattern as the blackwall-guard example).
vi.mock("openclaw/plugin-sdk/plugin-entry", () => ({
  definePluginEntry: (entry: unknown) => entry,
}));

import {
  atomicToDecimal,
  createBlackwallX402Gate,
  decide,
  extractClaim,
  handleBeforeToolCall,
  postJson,
  resolveConfig,
  toolLooksLikePayment,
} from "./index.ts";

const silent = { info() {}, warn() {}, error() {}, debug() {} };

const PAYEE = "0x02c2FCAFce36b4AADb39625866Bc6b1699D83043"; // mixed case on purpose
const PAYER = "0x1111111111111111111111111111111111111111";

// resolveConfig accepts an injected forecast fn, so the whole gate runs against
// a mock backend — no network in unit tests.
const cfg = (overrides: Record<string, unknown> = {}) =>
  resolveConfig({ forecast: vi.fn(async () => ({ verdict: "GO" })), ...overrides } as any);

const verdict = (v: string, extra: Record<string, unknown> = {}) => ({
  verdict: v,
  hard_stop: v === "STOP",
  score: 0.5,
  reasons: ["reason-one", "reason-two"],
  receipt_id: "bw_test",
  ...extra,
});

function b64(obj: unknown): string {
  return Buffer.from(JSON.stringify(obj), "utf-8").toString("base64");
}

const X_PAYMENT = b64({
  x402Version: 1,
  scheme: "exact",
  network: "base",
  payload: {
    signature: "0xsig",
    authorization: {
      from: PAYER,
      to: PAYEE,
      value: "14000", // atomic, 6dp => 0.014
      validAfter: "0",
      validBefore: "9999999999",
      nonce: "0xabc",
    },
  },
});

describe("atomicToDecimal (string-safe atomic-unit conversion)", () => {
  // MUTATION NOTES: float math here would corrupt large values; a mutant that
  // divides via Number fails the 18-digit case.
  it("converts 6-decimal USDC units", () => {
    expect(atomicToDecimal("14000", 6)).toBe("0.014");
    expect(atomicToDecimal("1000000", 6)).toBe("1");
    expect(atomicToDecimal("1", 6)).toBe("0.000001");
    expect(atomicToDecimal("1234567", 6)).toBe("1.234567");
  });
  it("is exact for values beyond float precision", () => {
    expect(atomicToDecimal("123456789012345678901", 6)).toBe("123456789012345.678901");
  });
  it("rejects non-digit input", () => {
    expect(atomicToDecimal("14.5", 6)).toBeNull();
    expect(atomicToDecimal("-3", 6)).toBeNull();
    expect(atomicToDecimal("0x10", 6)).toBeNull();
    expect(atomicToDecimal("", 6)).toBeNull();
  });
});

describe("toolLooksLikePayment (token-boundary matching, no substring bleed)", () => {
  // MUTATION NOTES: substring matching would flag "playback"/"paypal_search";
  // that false positive turns into a BLOCKED non-payment tool under enforce.
  const tokens = resolveConfig({} as any).paymentTools;
  it("matches payment-shaped names across naming styles", () => {
    for (const name of ["payInvoice", "x402_fetch", "make_payment", "pay", "usdc.send"]) {
      expect(toolLooksLikePayment(name, tokens), name).toBe(true);
    }
  });
  it("does not match lookalikes or generic tools", () => {
    for (const name of ["playback", "paypal_search", "display", "transfer_file", "search"]) {
      expect(toolLooksLikePayment(name, tokens), name).toBe(false);
    }
  });
});

describe("extractClaim (payment shapes -> Blackwall claim)", () => {
  // MUTATION NOTES: the address gate is the anti-garbage filter — a mutant that
  // accepts a non-EVM counterparty would forecast nonsense and (fail-open on
  // HOLD-only signals) weaken the gate; the lowercase step pins the reputation
  // key canonicalization; the atomic conversions pin 6dp math.
  it("extracts a flat decimal-amount claim and lowercases the address", () => {
    const claim = extractClaim({ payTo: PAYEE, amount: "0.014", asset: "USDC", network: "base" });
    expect(claim).toMatchObject({
      counterparty: PAYEE.toLowerCase(),
      amount: "0.014",
      asset: "USDC",
      chain: "base",
    });
  });
  it("accepts numeric amounts and alternate field names", () => {
    const claim = extractClaim({ recipient: PAYEE, price: 0.5 });
    expect(claim).toMatchObject({ counterparty: PAYEE.toLowerCase(), amount: "0.5" });
    expect(claim!.asset).toBe("USDC"); // default
    expect(claim!.chain).toBe("base"); // default
  });
  it("extracts from an x402 402-challenge accepts[] entry (atomic maxAmountRequired)", () => {
    const claim = extractClaim({
      accepts: [
        {
          scheme: "exact",
          network: "base",
          maxAmountRequired: "14000",
          payTo: PAYEE,
          asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
          resource: "https://api.example.com/v1/data",
        },
      ],
    });
    expect(claim).toMatchObject({
      counterparty: PAYEE.toLowerCase(),
      amount: "0.014",
      chain: "base",
      resource: "https://api.example.com/v1/data",
    });
  });
  it("extracts from a signed X-PAYMENT header and carries it for payload-sim", () => {
    const claim = extractClaim({ paymentHeader: X_PAYMENT });
    expect(claim).toMatchObject({
      counterparty: PAYEE.toLowerCase(),
      amount: "0.014",
      chain: "base",
      payer: PAYER.toLowerCase(),
      payment_authorization: X_PAYMENT,
    });
  });
  it("explicit flat fields win over the X-PAYMENT decode, but the header still rides along", () => {
    const claim = extractClaim({ payTo: PAYEE, amount: "0.02", payment: X_PAYMENT });
    expect(claim!.amount).toBe("0.02");
    expect(claim!.payment_authorization).toBe(X_PAYMENT);
  });
  it("returns null when there is no valid EVM counterparty or no positive amount", () => {
    expect(extractClaim({ payTo: "bob", amount: "0.01" })).toBeNull();
    expect(extractClaim({ payTo: PAYEE })).toBeNull();
    expect(extractClaim({ payTo: PAYEE, amount: "0" })).toBeNull();
    expect(extractClaim({ payTo: PAYEE, amount: "-1" })).toBeNull();
    expect(extractClaim({ query: "weather in SF" })).toBeNull();
    expect(extractClaim(null)).toBeNull();
  });
  it("survives a malformed X-PAYMENT header (garbage b64 -> null, no throw)", () => {
    expect(extractClaim({ payment: "!!!not-base64!!!" })).toBeNull();
    expect(extractClaim({ payment: b64({ nope: 1 }) })).toBeNull();
  });
});

describe("decide (family verdict mapping: allow / confirm / block)", () => {
  // MUTATION NOTES: same contract as the langchain + wallet guards — STOP
  // blocks under enforce, HOLD escalates (confirm), observe never blocks. A
  // mutant that lets STOP through under enforce is the whole gate failing.
  it("GO -> allow", () => {
    expect(decide(verdict("GO"), "enforce").action).toBe("allow");
  });
  it("HOLD -> confirm under enforce, allow under observe", () => {
    expect(decide(verdict("HOLD"), "enforce").action).toBe("confirm");
    expect(decide(verdict("HOLD"), "observe").action).toBe("allow");
  });
  it("STOP -> block under enforce, allow under observe", () => {
    expect(decide(verdict("STOP"), "enforce").action).toBe("block");
    expect(decide(verdict("STOP"), "observe").action).toBe("allow");
  });
  it("hard_stop blocks even if verdict text is unrecognized", () => {
    expect(decide(verdict("WEIRD", { hard_stop: true }), "enforce").action).toBe("block");
  });
  it("unknown verdicts escalate (confirm), never allow", () => {
    expect(decide({ verdict: "???" }, "enforce").action).toBe("confirm");
    expect(decide(null, "enforce").action).toBe("confirm");
  });
  it("carries the verdict's reasons", () => {
    expect(decide(verdict("HOLD"), "enforce").reasons).toContain("reason-one");
  });
});

describe("resolveConfig (defaults: enforce, fail-closed, free endpoint)", () => {
  // MUTATION NOTES: this plugin fires only on payment-shaped calls, so enforce
  // + fail-closed ARE the product; a mutant reverting either default turns the
  // backstop advisory.
  it("defaults to enforce, failClosed=true, and the public free endpoint", () => {
    const c = cfg({});
    expect(c.mode).toBe("enforce");
    expect(c.failClosed).toBe(true);
    expect(c.baseUrl).toBe("https://blackwall-free.onrender.com");
  });
  it("honors BLACKWALL_X402_* env overrides", () => {
    process.env.BLACKWALL_X402_URL = "https://self-hosted.example.com";
    process.env.BLACKWALL_X402_MODE = "observe";
    process.env.BLACKWALL_X402_FAIL_CLOSED = "false";
    try {
      const c = cfg({});
      expect(c.baseUrl).toBe("https://self-hosted.example.com");
      expect(c.mode).toBe("observe");
      expect(c.failClosed).toBe(false);
    } finally {
      delete process.env.BLACKWALL_X402_URL;
      delete process.env.BLACKWALL_X402_MODE;
      delete process.env.BLACKWALL_X402_FAIL_CLOSED;
    }
  });
  it("extra paymentTools extend, not replace, the defaults", () => {
    const c = cfg({ paymentTools: ["treasury"] });
    expect(toolLooksLikePayment("treasury_move", c.paymentTools)).toBe(true);
    expect(toolLooksLikePayment("x402_fetch", c.paymentTools)).toBe(true);
  });
});

describe("handleBeforeToolCall (the enforcement backstop)", () => {
  // MUTATION NOTES: the first test is the plugin's cost model — non-payment
  // calls MUST pass through with zero forecasts; the GO/HOLD/STOP trio pins the
  // hook contract; the failClosed pair pins "unreachable gate blocks payments".
  const ev = (params: Record<string, unknown>, toolName = "fetch_data") => ({
    toolName,
    toolCallId: "t1",
    params,
  });
  const payParams = { payTo: PAYEE, amount: "0.014", network: "base" };

  it("ignores non-payment tool calls without calling forecast", async () => {
    const forecast = vi.fn(async () => verdict("STOP"));
    const c = cfg({ forecast });
    expect(await handleBeforeToolCall(ev({ query: "hello" }), c, silent)).toBeUndefined();
    expect(forecast).not.toHaveBeenCalled();
  });

  it("GO -> allows and sends the claim (plus configured payer)", async () => {
    const forecast = vi.fn(async () => verdict("GO"));
    const c = cfg({ forecast, payer: PAYER });
    expect(await handleBeforeToolCall(ev(payParams), c, silent)).toBeUndefined();
    const sent = (forecast as any).mock.calls[0][0];
    expect(sent).toMatchObject({
      counterparty: PAYEE.toLowerCase(),
      amount: "0.014",
      chain: "base",
      payer: PAYER.toLowerCase(),
    });
  });

  it("HOLD -> blocks with the verdict's reasons in the blockReason", async () => {
    const c = cfg({ forecast: vi.fn(async () => verdict("HOLD")) });
    const r = await handleBeforeToolCall(ev(payParams), c, silent);
    expect(r).toMatchObject({ block: true });
    expect(r.blockReason).toContain("reason-one");
    expect(r.blockReason).toMatch(/HOLD/);
  });

  it("STOP -> blocks", async () => {
    const c = cfg({ forecast: vi.fn(async () => verdict("STOP")) });
    expect(await handleBeforeToolCall(ev(payParams), c, silent)).toMatchObject({ block: true });
  });

  it("observe mode never blocks, but still forecasts", async () => {
    const forecast = vi.fn(async () => verdict("STOP"));
    const c = cfg({ mode: "observe", forecast });
    expect(await handleBeforeToolCall(ev(payParams), c, silent)).toBeUndefined();
    expect(forecast).toHaveBeenCalled();
  });

  it("gate unreachable + failClosed (default) -> payment blocked", async () => {
    const c = cfg({ forecast: vi.fn(async () => { throw new Error("down"); }) });
    const r = await handleBeforeToolCall(ev(payParams), c, silent);
    expect(r).toMatchObject({ block: true });
    expect(r.blockReason).toMatch(/unreachable|failed/i);
  });

  it("gate unreachable + failClosed:false -> payment allowed (explicit opt-out)", async () => {
    const c = cfg({ failClosed: false, forecast: vi.fn(async () => { throw new Error("down"); }) });
    expect(await handleBeforeToolCall(ev(payParams), c, silent)).toBeUndefined();
  });

  it("payment-named tool with an unscorable payload -> blocked under enforce", async () => {
    const forecast = vi.fn(async () => verdict("GO"));
    const c = cfg({ forecast });
    const r = await handleBeforeToolCall(ev({ blob: "???" }, "x402_pay"), c, silent);
    expect(r).toMatchObject({ block: true });
    expect(r.blockReason).toMatch(/unscorable|could not extract/i);
    expect(forecast).not.toHaveBeenCalled();
  });

  it("payment-named tool with an unscorable payload -> allowed under observe", async () => {
    const c = cfg({ mode: "observe" });
    expect(await handleBeforeToolCall(ev({ blob: "???" }, "x402_pay"), c, silent)).toBeUndefined();
  });

  it("a missing toolName never gates", async () => {
    const c = cfg({});
    expect(await handleBeforeToolCall({ params: payParams }, c, silent)).toBeUndefined();
  });
});

describe("plugin entry", () => {
  it("registers a before_tool_call hook", () => {
    const plugin = createBlackwallX402Gate({ forecast: vi.fn() } as any) as any;
    const on = vi.fn();
    plugin.register({ on, logger: silent });
    expect(on).toHaveBeenCalledWith("before_tool_call", expect.any(Function), expect.anything());
  });
});

describe("audit regressions (session audit of the initial implementation)", () => {
  // MUTATION NOTES: each of these pins a hole found in adversarial audit —
  // A1: uppercase config tokens silently never matched (operator believes
  //     their wallet tool is gated; it is not). A2: case-variant param keys
  //     (payto/PayTo) escaped recognition entirely. A3: scientific-notation
  //     numeric amounts produced malformed claims ("1e+21").
  it("A1: config paymentTools match case-insensitively", () => {
    const c = cfg({ paymentTools: ["Treasury"] });
    expect(toolLooksLikePayment("treasury_move", c.paymentTools)).toBe(true);
  });
  it("A2: param keys match case-insensitively (payto, PayTo, PAY_TO)", () => {
    for (const key of ["payto", "PayTo", "PAY_TO", "Recipient"]) {
      const claim = extractClaim({ [key]: PAYEE, amount: "0.01" });
      expect(claim, key).not.toBeNull();
      expect(claim!.counterparty).toBe(PAYEE.toLowerCase());
    }
  });
  it("A2: exact-case keys still win over case-variants", () => {
    const other = "0x9999999999999999999999999999999999999999";
    const claim = extractClaim({ payTo: PAYEE, PAYTO: other, amount: "0.01" });
    expect(claim!.counterparty).toBe(PAYEE.toLowerCase());
  });
  it("A3: scientific-notation amounts are rejected, not forwarded", () => {
    expect(extractClaim({ payTo: PAYEE, amount: 1e21 })).toBeNull();
    expect(extractClaim({ payTo: PAYEE, amount: 1e-7 })).toBeNull();
    expect(extractClaim({ payTo: PAYEE, amount: "1e3" })).toBeNull();
    // plain decimals still fine
    expect(extractClaim({ payTo: PAYEE, amount: 1000 })!.amount).toBe("1000");
  });
});

describe("postJson (loopback: real sockets, no proxy)", () => {
  // MUTATION NOTES: A4 — the size cap pins "a hostile endpoint cannot stream
  // unbounded data into the hook"; the happy-path test pins status+JSON parsing
  // over a real socket.
  it("round-trips JSON over loopback HTTP", async () => {
    const http = await import("node:http");
    const srv = http.createServer((req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: true, path: req.url }));
    });
    await new Promise<void>((r) => srv.listen(0, "127.0.0.1", () => r()));
    const port = (srv.address() as any).port;
    try {
      const { status, json } = await postJson(`http://127.0.0.1:${port}/v1/x`, { a: 1 }, 5000);
      expect(status).toBe(200);
      expect(json).toMatchObject({ ok: true, path: "/v1/x" });
    } finally {
      srv.close();
    }
  });
  it("A4: rejects a response that exceeds the size cap instead of buffering it", async () => {
    const http = await import("node:http");
    const big = "x".repeat(2 * 1024 * 1024);
    const srv = http.createServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(big);
    });
    await new Promise<void>((r) => srv.listen(0, "127.0.0.1", () => r()));
    const port = (srv.address() as any).port;
    try {
      await expect(postJson(`http://127.0.0.1:${port}/`, {}, 10000)).rejects.toThrow(/size cap/);
    } finally {
      srv.close();
    }
  });
});
