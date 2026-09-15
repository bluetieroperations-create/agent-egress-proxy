/**
 * Each test names the mutation it kills.
 *
 * The properties that matter here are the two the wrapper POSITION forces --
 * score every accepts[] entry, and read all three challenge carriers -- because
 * getting either wrong produces a gate that looks installed and scores the
 * wrong payment or no payment at all.
 */
import { describe, expect, it, vi } from "vitest";
import {
  BLACKWALL_VERDICT,
  b64decode,
  claimsFromChallenge,
  combine,
  parseChallenge,
  requirementsFromAuthenticate,
  wrapFetchWithBlackwall,
} from "./index.js";

const PAYEE_A = "0x" + "a".repeat(40);
const PAYEE_B = "0x" + "b".repeat(40);

function challenge(entries: any[]) {
  return { x402Version: 2, accepts: entries };
}

function entry(payTo: string, atomic = "1000", extra: any = {}) {
  return { scheme: "exact", network: "eip155:8453", maxAmountRequired: atomic,
           asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", payTo, ...extra };
}

function headers(h: Record<string, string> = {}) {
  return new Headers(h);
}

function resp402(body: unknown, h: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status: 402,
    headers: { "content-type": "application/json", ...h },
  });
}

describe("challenge carriers (design point 2)", () => {
  it("reads the JSON body", () => {
    expect(parseChallenge(challenge([entry(PAYEE_A)]), headers())).toHaveLength(1);
  });

  it("reads the bare-base64 payment-required header when the body is {}", () => {
    // MUTATION: body-only parsing. MEASURED: 80 of 195 live x402 hosts serve a
    // complete v2 challenge in this header with `{}` as the body, so a
    // body-only gate passes 41% of the live ecosystem through UNSCORED while
    // reporting no error. That is the failure this test exists for.
    const b64 = Buffer.from(JSON.stringify(challenge([entry(PAYEE_A)]))).toString("base64");
    expect(parseChallenge({}, headers({ "payment-required": b64 }))).toHaveLength(1);
  });

  it("reads the WWW-Authenticate X402 requirements form", () => {
    // MUTATION: ignoring this carrier. Only 2 of 195 hosts, but it is the
    // spec's v2 style and costs one regex.
    const b64 = Buffer.from(JSON.stringify(challenge([entry(PAYEE_B)]))).toString("base64");
    expect(
      parseChallenge({}, headers({ "www-authenticate": `X402 requirements="${b64}"` })),
    ).toHaveLength(1);
  });

  it("BODY WINS over a disagreeing header", () => {
    // MUTATION: preferring the header. The body is what other x402 clients pay,
    // so a gate scoring the header would score a payment nobody settles.
    const b64 = Buffer.from(JSON.stringify(challenge([entry(PAYEE_B)]))).toString("base64");
    const got = parseChallenge(challenge([entry(PAYEE_A)]), headers({ "payment-required": b64 }));
    expect(got).toHaveLength(1);
    expect((got[0] as any).payTo).toBe(PAYEE_A);
  });

  it("never throws on third-party junk", () => {
    // MUTATION: letting a parse error escape. This runs inside the caller's
    // request path; a throw here takes down the agent, not the payment.
    for (const junk of [null, undefined, 7, "x", [], {}, { accepts: "no" }, { accepts: [1, null] }]) {
      expect(() => parseChallenge(junk, headers({ "payment-required": "!!!not-b64!!!" }))).not.toThrow();
      expect(parseChallenge(junk, headers())).toEqual([]);
    }
  });

  it("b64decode and the requirements regex handle absent input", () => {
    expect(requirementsFromAuthenticate(null)).toBeNull();
    expect(requirementsFromAuthenticate("Basic realm=x")).toBeNull();
    expect(b64decode("////not base64 at all ~~~")).not.toBe(undefined);
  });
});

describe("claims from the challenge (design point 1)", () => {
  it("produces ONE claim PER accepts entry", () => {
    // MUTATION: accepts[0] only. The client picks a requirement later, inside
    // wrapFetchWithPayment -- so scoring the first and letting it pay the
    // second means the gate scored a payment that never happened.
    const { claims } = claimsFromChallenge([entry(PAYEE_A), entry(PAYEE_B)]);
    expect(claims.map((c) => c.counterparty)).toEqual([PAYEE_A, PAYEE_B]);
  });

  it("converts atomic units to a decimal amount", () => {
    const { claims } = claimsFromChallenge([entry(PAYEE_A, "5000")]);
    expect(claims[0].amount).toBe("0.005");
  });

  it("DROPS an unreadable entry instead of scoring a placeholder", () => {
    // MUTATION: emitting a claim with an empty counterparty. That scores as an
    // unknown address and returns a cold-start HOLD -- which reads as a real
    // risk finding about a seller when it is a parse failure on OUR side.
    const { claims, silentEntries } = claimsFromChallenge([
      entry(PAYEE_A),
      { scheme: "exact", network: "base" },            // no payTo, no amount
      { payTo: PAYEE_B },                              // no amount
    ]);
    expect(claims).toHaveLength(1);
    expect(silentEntries).toBe(2);
  });

  it("carries the resource and payer through", () => {
    const { claims } = claimsFromChallenge([entry(PAYEE_A)], "https://x.example/api", PAYEE_B);
    expect(claims[0].resource).toBe("https://x.example/api");
    expect(claims[0].payer).toBe(PAYEE_B);
  });
});

describe("combine: most conservative wins", () => {
  const A = { action: "allow" as const, verdict: "GO", reasons: ["ok"] };
  const C = { action: "confirm" as const, verdict: "HOLD", reasons: ["thin"] };
  const B = { action: "block" as const, verdict: "STOP", reasons: ["sanctioned"] };

  it("one STOP among GOs blocks", () => {
    // MUTATION: taking the first, or the majority. Either lets the client pay
    // the entry that was refused.
    expect(combine([A, A, B]).action).toBe("block");
    expect(combine([B, A]).action).toBe("block");
  });

  it("one HOLD among GOs confirms", () => {
    expect(combine([A, C, A]).action).toBe("confirm");
  });

  it("all GO allows", () => {
    expect(combine([A, A]).action).toBe("allow");
  });

  it("an EMPTY list allows, and says it was UNSCORED", () => {
    // MUTATION: blocking on empty. Refusing every unparseable 402 makes the
    // gate a denial-of-service on the agent it protects. Deliberate fail-open,
    // and the reason the header carriers above are load-bearing.
    expect(combine([])).toEqual({ action: "allow", verdict: "UNSCORED", reasons: [] });
  });

  it("deduplicates reasons across entries", () => {
    expect(combine([C, C]).reasons).toEqual(["thin"]);
  });
});

describe("the fetch wrapper", () => {
  const ok = () => new Response("paid", { status: 200 });

  function gate(verdicts: any[], cfg: any = {}) {
    let i = 0;
    return wrapFetchWithBlackwall(
      (async () => resp402(challenge([entry(PAYEE_A), entry(PAYEE_B)]))) as any,
      { forecast: async () => verdicts[Math.min(i++, verdicts.length - 1)], ...cfg },
    );
  }

  it("passes a NON-402 straight through and never forecasts", async () => {
    // MUTATION: scoring every response. A 200 is not a payment.
    const forecast = vi.fn();
    const f = wrapFetchWithBlackwall(ok as any, { forecast: forecast as any });
    expect((await f("https://x.example" as any)).status).toBe(200);
    expect(forecast).not.toHaveBeenCalled();
  });

  it("lets a 402 through UNCHANGED when every entry is GO", async () => {
    // The 402 must survive so wrapFetchWithPayment can pay it.
    const r = await gate([{ verdict: "GO" }])("https://x.example" as any);
    expect(r.status).toBe(402);
  });

  it("turns a 402 into a 403 when any entry is STOP", async () => {
    // MUTATION: returning the 402 anyway. Then the payment wrapper signs and
    // the gate is decorative -- the wired-and-inert shape in a fetch chain.
    const r = await gate([{ verdict: "GO" }, { verdict: "STOP" }])("https://x.example" as any);
    expect(r.status).toBe(403);
    const body = await r.json();
    expect(body.error).toBe(BLACKWALL_VERDICT);
    expect(body.verdict).toBe("STOP");
  });

  it("a hard_stop blocks even when the verdict string is not STOP", async () => {
    const r = await gate([{ verdict: "HOLD", hard_stop: true }])("https://x.example" as any);
    expect(r.status).toBe(403);
  });

  it("scores EVERY entry on the wire, not just the first", async () => {
    // MUTATION: one forecast call. Two accepts entries must mean two calls.
    const forecast = vi.fn(async () => ({ verdict: "GO" }));
    const f = wrapFetchWithBlackwall(
      (async () => resp402(challenge([entry(PAYEE_A), entry(PAYEE_B)]))) as any,
      { forecast: forecast as any },
    );
    await f("https://x.example" as any);
    expect(forecast).toHaveBeenCalledTimes(2);
  });

  it("HOLD blocks with no confirm handler, and proceeds when one approves", async () => {
    const blocked = await gate([{ verdict: "HOLD" }])("https://x.example" as any);
    expect(blocked.status).toBe(403);
    const allowed = await gate([{ verdict: "HOLD" }], { onConfirm: () => true })(
      "https://x.example" as any,
    );
    expect(allowed.status).toBe(402);
  });

  it("a confirm handler that THROWS is not an approval", async () => {
    // MUTATION: treating a thrown handler as consent. Fail-safe to refusing.
    const r = await gate([{ verdict: "HOLD" }], {
      onConfirm: () => {
        throw new Error("ui down");
      },
    })("https://x.example" as any);
    expect(r.status).toBe(403);
  });

  it("FAIL-CLOSED by default when Blackwall is unreachable", async () => {
    const r = await gate([], {
      forecast: async () => {
        throw new Error("ECONNREFUSED");
      },
    })("https://x.example" as any);
    expect(r.status).toBe(503);
    expect((await r.json()).verdict).toBe("UNAVAILABLE");
  });

  it("fail-OPEN returns the original 402 when configured", async () => {
    // RESTRAINT CONTROL: an operator may choose availability over coverage.
    const r = await gate([], {
      failClosed: false,
      forecast: async () => {
        throw new Error("ECONNREFUSED");
      },
    })("https://x.example" as any);
    expect(r.status).toBe(402);
  });

  it("OBSERVE mode never blocks, even on STOP", async () => {
    // MUTATION: gating in observe mode. The whole point of observe is measuring
    // the false-block rate before enforcing.
    const r = await gate([{ verdict: "STOP" }], { mode: "observe" })("https://x.example" as any);
    expect(r.status).toBe(402);
  });

  it("an unparseable 402 passes through rather than blocking", async () => {
    const f = wrapFetchWithBlackwall(
      (async () => new Response("<html>gateway</html>", { status: 402 })) as any,
      { forecast: async () => ({ verdict: "STOP" }) },
    );
    expect((await f("https://x.example" as any)).status).toBe(402);
  });

  it("reads the 402 body ONCE so the response stays consumable", async () => {
    // MUTATION: awaiting response.text() instead of response.clone().text().
    // A Response body is a stream: reading it here would leave the caller --
    // and wrapFetchWithPayment -- with an already-consumed body, silently
    // downgrading a real challenge to unreadable. Same defect
    // x402_challenge.accepts_from_http_error exists to avoid.
    const f = wrapFetchWithBlackwall(
      (async () => resp402(challenge([entry(PAYEE_A)]))) as any,
      { forecast: async () => ({ verdict: "GO" }) },
    );
    const r = await f("https://x.example" as any);
    expect(r.status).toBe(402);
    const body = await r.json();          // must still be readable
    expect(body.accepts[0].payTo).toBe(PAYEE_A);
  });
});
