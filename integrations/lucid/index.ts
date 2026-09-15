/**
 * blackwall-lucid-policy -- a Blackwall verdict gate for the Lucid Agents
 * Commerce SDK (daydreamsai/lucid-agents).
 *
 * WHAT LUCID ALREADY DOES, and why this is an ADAPTER and not a clone. Lucid's
 * buyer path composes fetch wrappers:
 *
 *     const policyFetch = wrapBaseFetchWithPolicy(fetch, { ...budget });
 *     const paidFetch   = wrapFetchWithPayment(policyFetch, client);
 *
 * Their `wrapBaseFetchWithPolicy` inspects the unpaid x402 v2 requirement and
 * reserves budget BEFORE a signature is created, rejecting a disallowed
 * challenge with `403 policy_violation`. That is exactly the right place to
 * stand, and it is why this integration is a fetch wrapper too rather than a
 * reimplementation of anything.
 *
 * THE GAP, which is the same one `integrations/agentcore/` documents about AWS:
 * their budget tier constrains
 *
 *     allowedRecipients   a STATIC allowlist
 *     maxPaymentUsd       a per-request ceiling
 *     maxTotalUsd/windowMs a time-bounded total
 *
 * So it enforces HOW MUCH, and WHO only from a list a human typed. It never
 * asks who the counterparty actually IS. An allowlist cannot tell you that a
 * payee you are about to add is a wash-trading Sybil ring, is OFAC-sanctioned,
 * quotes 50x its category's settled median, has been silent for 90 days, or
 * advertises a `payTo` that is not a possible address on any chain. Those are
 * the questions Blackwall answers, and none of them are a spend limit.
 *
 * TWO DESIGN POINTS THE WRAPPER POSITION FORCES, both of which would be bugs if
 * handled the obvious way:
 *
 * 1. SCORE EVERY `accepts[]` ENTRY, NOT THE FIRST. At policy time the client
 *    has not yet chosen which requirement to satisfy -- that happens inside
 *    `wrapFetchWithPayment` when the scheme is selected. Scoring `accepts[0]`
 *    and letting the client pay `accepts[1]` means the gate scored a payment
 *    that never happened, which is a check aimed slightly to the left of the
 *    thing it verifies. Since the choice is unknowable here, the only sound
 *    rule is "safe for whichever it picks": the combined decision is the MOST
 *    CONSERVATIVE across all entries. (`integrations/openclaw` reads
 *    `accepts[0]`; it sits at a tool-call boundary where the payload is already
 *    chosen, so the assumption holds there and not here.)
 *
 * 2. READ ALL THREE CHALLENGE CARRIERS. Requirements arrive in the JSON body,
 *    in `WWW-Authenticate: X402 requirements="<b64>"`, or in a bare-base64
 *    `payment-required:` header. MEASURED on 195 live x402 hosts: the body
 *    alone leaves 86 of them unreadable, and 80 of those 86 serve a complete v2
 *    challenge in `payment-required` with `{}` as the body. A body-only gate
 *    therefore fails OPEN on 41% of the live ecosystem while looking healthy --
 *    it sees no challenge and passes the request through. Same finding as
 *    `x402_challenge.py`, and the reason that module exists.
 *
 * WHAT THIS NEVER DOES: it does not sign, hold a key, or move money. It turns a
 * 402 into a 403 when the verdict is not GO, which is the same currency Lucid's
 * own policy layer already speaks, so `wrapFetchWithPayment` sees a refusal
 * rather than a payable challenge and no signature is ever created.
 *
 * Decision logic is IMPORTED from ../openclaw, not copied: `decide` is the one
 * place GO/HOLD/STOP becomes allow/confirm/block, and two copies would drift.
 */
import {
  atomicToDecimal,
  decide,
  postJson,
  resolveConfig,
  type GateConfig,
  type PaymentClaim,
  type ResolvedConfig,
} from "../openclaw/core.js";

export { type GateConfig, type PaymentClaim, type ResolvedConfig };

/** Lucid's own refusal code, reused so callers can handle one shape. */
export const POLICY_VIOLATION = "policy_violation";
/** Ours, so an operator can tell a budget refusal from a verdict refusal. */
export const BLACKWALL_VERDICT = "blackwall_verdict";

const USDC_DECIMALS = 6;

// ---------------------------------------------------------------------------
// Pure: parse the challenge (all three carriers)
// ---------------------------------------------------------------------------

/** Decode base64 without assuming a browser or Node global. */
export function b64decode(raw: string): string | null {
  try {
    if (typeof atob === "function") return atob(raw.trim());
  } catch {
    /* fall through */
  }
  try {
    return Buffer.from(raw.trim(), "base64").toString("utf-8");
  } catch {
    return null;
  }
}

/** Pull the base64 payload out of `WWW-Authenticate: X402 requirements="..."`. */
export function requirementsFromAuthenticate(value: string | null): string | null {
  if (!value) return null;
  const m = /requirements\s*=\s*"?([A-Za-z0-9+/=_-]+)"?/i.exec(value);
  return m ? m[1] : null;
}

/**
 * The parsed challenge, from whichever carrier supplied it.
 *
 * BODY WINS on disagreement, matching `x402_challenge.py`: the body is what
 * other x402 clients pay, so a gate that preferred a header would score
 * something nobody settles. TOLERANT BY CONSTRUCTION -- third-party junk yields
 * an empty list, never a throw, because a parse error here would take down the
 * caller's request path.
 */
export function parseChallenge(
  body: unknown,
  headers: { get(name: string): string | null } | null,
): Record<string, unknown>[] {
  const fromBody = acceptsOf(body);
  if (fromBody.length) return fromBody;
  if (!headers) return [];
  const direct = headers.get("payment-required");
  if (direct) {
    const decoded = b64decode(direct);
    if (decoded) {
      const parsed = safeJson(decoded);
      const a = acceptsOf(parsed);
      if (a.length) return a;
    }
  }
  const auth = requirementsFromAuthenticate(headers.get("www-authenticate"));
  if (auth) {
    const decoded = b64decode(auth);
    if (decoded) {
      const a = acceptsOf(safeJson(decoded));
      if (a.length) return a;
    }
  }
  return [];
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function acceptsOf(doc: unknown): Record<string, unknown>[] {
  if (!doc || typeof doc !== "object") return [];
  const accepts = (doc as Record<string, unknown>).accepts;
  if (!Array.isArray(accepts)) return [];
  return accepts.filter((e) => e && typeof e === "object") as Record<string, unknown>[];
}

// ---------------------------------------------------------------------------
// Pure: challenge -> claims
// ---------------------------------------------------------------------------

function str(v: unknown): string | undefined {
  return typeof v === "string" && v.trim() ? v.trim() : undefined;
}

/**
 * One claim per `accepts[]` entry. See design point 1: the client has not
 * chosen yet, so every entry is a payment that might happen.
 *
 * An entry missing a payee or an amount is DROPPED rather than scored with a
 * placeholder: a claim with an empty counterparty would be scored as an unknown
 * address and draw a cold-start HOLD, which reads as a real risk finding about
 * a seller when it is actually a parse failure on our side. `silentEntries`
 * reports how many were dropped so the caller is not told "no payment found"
 * when the truth is "we could not read it".
 */
export function claimsFromChallenge(
  accepts: Record<string, unknown>[],
  resourceUrl?: string,
  payer?: string,
): { claims: PaymentClaim[]; silentEntries: number } {
  const claims: PaymentClaim[] = [];
  let silent = 0;
  for (const entry of accepts) {
    const counterparty = str(entry.payTo) ?? str(entry.pay_to) ?? str(entry.recipient);
    const amount =
      atomicToDecimal(entry.maxAmountRequired ?? entry.amount, USDC_DECIMALS) ??
      str(entry.maxAmountRequired) ??
      str(entry.amount);
    if (!counterparty || !amount) {
      silent += 1;
      continue;
    }
    const claim: PaymentClaim = {
      counterparty,
      amount,
      asset: str(entry.asset) ?? "USDC",
      chain: str(entry.network) ?? str(entry.chain) ?? "base",
    };
    const resource = str(entry.resource) ?? resourceUrl;
    if (resource) claim.resource = resource;
    if (payer) claim.payer = payer;
    claims.push(claim);
  }
  return { claims, silentEntries: silent };
}

// ---------------------------------------------------------------------------
// Pure: combine
// ---------------------------------------------------------------------------

export type Action = "allow" | "confirm" | "block";

const RANK: Record<Action, number> = { allow: 0, confirm: 1, block: 2 };

/**
 * MOST CONSERVATIVE WINS. See design point 1: "safe for whichever entry the
 * client picks" is the only sound rule when the choice is not yet made.
 *
 * An EMPTY list is `allow`, not `block`: no readable requirement means this was
 * not a payment we can score, and refusing every unparseable 402 would make the
 * gate a denial-of-service on the agent it is protecting. That is a deliberate
 * fail-open, and it is exactly why design point 2 matters -- the header carriers
 * must be read, or 86 of 195 live hosts land in this branch.
 */
export function combine(
  decisions: { action: Action; verdict: string; reasons: string[] }[],
): { action: Action; verdict: string; reasons: string[] } {
  if (!decisions.length) return { action: "allow", verdict: "UNSCORED", reasons: [] };
  let worst = decisions[0];
  for (const d of decisions) if (RANK[d.action] > RANK[worst.action]) worst = d;
  const reasons: string[] = [];
  for (const d of decisions) for (const r of d.reasons) if (!reasons.includes(r)) reasons.push(r);
  return { action: worst.action, verdict: worst.verdict, reasons };
}

// ---------------------------------------------------------------------------
// The adapter
// ---------------------------------------------------------------------------

export interface LucidGateConfig extends GateConfig {
  /** Called instead of returning a 403, for `confirm`. Return true to proceed. */
  onConfirm?: (d: { verdict: string; reasons: string[]; claims: PaymentClaim[] }) => Promise<boolean> | boolean;
}

function refusal(code: string, verdict: string, reasons: string[], status = 403): Response {
  return new Response(
    JSON.stringify({ error: code, verdict, reasons }),
    { status, headers: { "content-type": "application/json" } },
  );
}

/**
 * Wrap a base fetch so a 402 is scored before anything signs it.
 *
 * Place it where Lucid's own policy wrapper goes -- INSIDE
 * `wrapFetchWithPayment`, so the 402 passes through here on its way back and a
 * refusal reaches the payment wrapper as a 403 it cannot pay against:
 *
 *     const gated = wrapFetchWithBlackwall(fetch, { baseUrl, payer });
 *     const policy = wrapBaseFetchWithPolicy(gated, { ...budget });
 *     const paid   = wrapFetchWithPayment(policy, client);
 *
 * FAIL-CLOSED BY DEFAULT (`failClosed`, inherited from the openclaw config), and
 * the tradeoff is real in both directions: closed means a Blackwall outage stops
 * the agent paying, open means an outage silently removes the gate. Closed is
 * the default because an unscored payment is irreversible and a stopped agent is
 * not, but an operator running unattended may legitimately choose otherwise.
 */
export function wrapFetchWithBlackwall(
  baseFetch: typeof fetch,
  config: LucidGateConfig = {},
): typeof fetch {
  const cfg: ResolvedConfig = resolveConfig(config);
  const onConfirm = config.onConfirm;

  return async function blackwallFetch(input: any, init?: any): Promise<Response> {
    const response: Response = await baseFetch(input, init);
    if (response.status !== 402) return response;

    // Read the body ONCE and keep it: a Response body is a stream, and a second
    // read silently downgrades a real challenge to "unreadable" -- the defect
    // `x402_challenge.accepts_from_http_error` exists to avoid. Everything
    // downstream gets a fresh Response built from the text we captured.
    let text = "";
    try {
      text = await response.clone().text();
    } catch {
      text = "";
    }
    const accepts = parseChallenge(safeJson(text), response.headers);
    const url = typeof input === "string" ? input : (input?.url as string | undefined);
    const { claims } = claimsFromChallenge(accepts, url, cfg.payer);

    if (!claims.length) return response; // nothing readable to score -- see `combine`

    let decisions: { action: Action; verdict: string; reasons: string[] }[];
    try {
      decisions = await Promise.all(
        claims.map(async (claim) => {
          const verdictObj = await cfg.forecast(claim, cfg);
          return decide(verdictObj, cfg.mode) as { action: Action; verdict: string; reasons: string[] };
        }),
      );
    } catch (err: any) {
      if (cfg.failClosed && cfg.mode !== "observe") {
        return refusal(
          BLACKWALL_VERDICT,
          "UNAVAILABLE",
          [`blackwall unreachable (${err?.message ?? err}) -- refusing to pay unscored`],
          503,
        );
      }
      return response;
    }

    const combined = combine(decisions);
    if (combined.action === "allow") return response;
    if (combined.action === "confirm" && onConfirm) {
      let ok = false;
      try {
        ok = !!(await onConfirm({ ...combined, claims }));
      } catch {
        ok = false; // a confirm handler that threw is NOT an approval
      }
      if (ok) return response;
    }
    return refusal(BLACKWALL_VERDICT, combined.verdict, combined.reasons);
  } as typeof fetch;
}

export { decide, postJson, resolveConfig, atomicToDecimal };
