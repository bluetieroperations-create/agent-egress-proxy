/**
 * Blackwall x402 payment gate — OpenClaw/NemoClaw plugin.
 *
 * "Call Blackwall before you sign", enforced at the RUNTIME layer: a
 * `before_tool_call` hook that recognizes payment-shaped tool calls, extracts
 * the x402 claim {counterparty, amount, asset, chain}, asks a Blackwall
 * deployment's POST /v1/forecast-payment for a GO / HOLD / STOP verdict, and
 * blocks anything that isn't a GO — even if the agent (compromised, mistaken,
 * or prompt-injected) never asked. This is the mandatory backstop behind the
 * advisory skill layer; a skill an agent can skip is guidance, a hook it cannot
 * skip is a gate.
 *
 * Design notes (mirrors the langchain + wallet guard family):
 *   - decision core is dependency-free and pure (extractClaim / decide);
 *     the OpenClaw plugin-entry import is the only host coupling.
 *   - verdict mapping: GO -> allow, HOLD -> confirm, STOP -> block. This
 *     runtime has no interactive approval surface, so `confirm` degrades to
 *     block-with-detail (the verdict's reasons ride in blockReason).
 *   - observe mode never blocks (trial/rollout), enforce is the DEFAULT: this
 *     hook fires only on payment-shaped calls, so enforcement is the product.
 *   - failClosed defaults true: an unreachable verdict service blocks the
 *     payment, not the conversation — non-payment tool calls are never touched.
 *   - no API key: the default endpoint is the free public Blackwall instance;
 *     only the payment CLAIM leaves the sandbox (data that is about to be
 *     public on-chain anyway), never tool payloads.
 *
 * Canonical source: integrations/openclaw/ in the Blackwall repo. The
 * nemoclaw-community example `blackwall-x402-payment-gate` vendors a copy.
 */

import net from "node:net";
import tls from "node:tls";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

type Mode = "observe" | "enforce";

export interface PaymentClaim {
  counterparty: string;
  amount: string;
  asset: string;
  chain: string;
  resource?: string;
  payer?: string;
  payment_authorization?: string;
}

export interface GateConfig {
  baseUrl?: string;
  mode?: Mode;
  failClosed?: boolean;
  payer?: string;
  paymentTools?: string[];
  forecastTimeoutMs?: number;
  forecast?: (claim: PaymentClaim, cfg: ResolvedConfig) => Promise<any>;
}

export interface ResolvedConfig {
  baseUrl: string;
  mode: Mode;
  failClosed: boolean;
  payer?: string;
  paymentTools: string[];
  forecastTimeoutMs: number;
  forecast: (claim: PaymentClaim, cfg: ResolvedConfig) => Promise<any>;
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

const EVM_ADDRESS = /^0x[0-9a-fA-F]{40}$/;

function normalizeAddress(value: unknown): string | null {
  return typeof value === "string" && EVM_ADDRESS.test(value.trim())
    ? value.trim().toLowerCase()
    : null;
}

/** Positive PLAIN-decimal amount as a canonical string, or null. Numbers whose
 * string form is scientific notation (1e21, 1e-7) are rejected rather than
 * forwarded as a malformed claim — such magnitudes are not legitimate x402
 * payments, and the claim format is plain decimal. */
function parseAmount(value: unknown): string | null {
  if (typeof value === "number") {
    if (!Number.isFinite(value) || value <= 0) return null;
    value = String(value);
  }
  if (typeof value !== "string") return null;
  const s = (value as string).trim();
  if (!/^\d+(\.\d+)?$/.test(s)) return null;
  return Number(s) > 0 ? s : null;
}

/**
 * Atomic token units -> decimal string, exactly (no float math — an 18-digit
 * USDC balance would lose precision through Number). "14000" @6dp -> "0.014".
 */
export function atomicToDecimal(units: unknown, decimals: number): string | null {
  if (typeof units === "number") units = String(units);
  if (typeof units !== "string" || !/^\d+$/.test(units)) return null;
  const padded = units.padStart(decimals + 1, "0");
  const whole = padded.slice(0, padded.length - decimals).replace(/^0+(?=\d)/, "");
  const frac = padded.slice(padded.length - decimals).replace(/0+$/, "");
  return frac ? `${whole}.${frac}` : whole;
}

/** Lowercase word tokens of a tool name: "payInvoice" -> ["pay","invoice"]. */
function tokensOf(name: string): string[] {
  return name
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
}

// Conservative on purpose: a false positive here BLOCKS a non-payment tool
// under enforce when its payload is unscorable. Generic verbs (send, transfer,
// sign) stay out; deployments add their wallet tool names via `paymentTools`.
const DEFAULT_PAYMENT_TOKENS = ["x402", "pay", "payment", "payto", "usdc"];

export function toolLooksLikePayment(toolName: string, paymentTokens: string[]): boolean {
  const tokens = tokensOf(toolName);
  return paymentTokens.some((t) => tokens.includes(t));
}

// Field aliases seen across x402 clients / wallet tools.
const COUNTERPARTY_KEYS = ["payTo", "pay_to", "payee", "recipient", "counterparty", "to"];
const AMOUNT_KEYS = ["amount", "price"]; // decimal strings/numbers at the flat level
const ASSET_KEYS = ["asset", "currency", "token"];
const CHAIN_KEYS = ["chain", "network"];
const RESOURCE_KEYS = ["resource", "url"];
const XPAYMENT_KEYS = [
  "payment_authorization",
  "paymentHeader",
  "payment_header",
  "x_payment",
  "xPayment",
  "payment",
];
const USDC_DECIMALS = 6;

/** First non-empty value under any alias, matching keys CASE-INSENSITIVELY
 * (payto / PayTo / PAY_TO all count) — exact-case hits win over variants so a
 * conventional payload is never ambiguous. Underscores are ignored in the
 * fallback so snake/camel variants collapse. */
function firstOf(obj: Record<string, unknown>, keys: string[]): unknown {
  for (const k of keys) if (obj[k] != null && obj[k] !== "") return obj[k];
  const fold = (s: string) => s.toLowerCase().replace(/_/g, "");
  const folded = new Map<string, unknown>();
  for (const [k, v] of Object.entries(obj)) {
    if (v != null && v !== "" && !folded.has(fold(k))) folded.set(fold(k), v);
  }
  for (const k of keys) {
    const v = folded.get(fold(k));
    if (v !== undefined) return v;
  }
  return undefined;
}

/** Decode a base64 X-PAYMENT header into its EIP-3009 authorization, or null. */
function decodeXPayment(header: unknown): Record<string, any> | null {
  if (typeof header !== "string" || header.length < 8) return null;
  try {
    const parsed = JSON.parse(Buffer.from(header, "base64").toString("utf-8"));
    const auth = parsed?.payload?.authorization;
    return auth && typeof auth === "object" ? { ...parsed, authorization: auth } : null;
  } catch {
    return null;
  }
}

/**
 * Recognize a payment in a tool call's params and normalize it into a
 * Blackwall claim. Three shapes, most-explicit first:
 *   1. flat fields (payTo/amount/... as an agent's payment tool would take)
 *   2. an x402 402-challenge `accepts[]` entry (atomic maxAmountRequired)
 *   3. a signed base64 X-PAYMENT header (EIP-3009 authorization)
 * A found X-PAYMENT header always rides along as `payment_authorization` so the
 * service's payload-sim can cross-check the signature against the claim.
 * Returns null unless a valid EVM counterparty AND a positive amount emerge —
 * the gate scores payments, it does not guess.
 */
export function extractClaim(params: unknown): PaymentClaim | null {
  if (params === null || typeof params !== "object") return null;
  const p = params as Record<string, unknown>;

  let counterparty = normalizeAddress(firstOf(p, COUNTERPARTY_KEYS));
  let amount = parseAmount(firstOf(p, AMOUNT_KEYS));
  let asset = firstOf(p, ASSET_KEYS);
  let chain = firstOf(p, CHAIN_KEYS);
  let resource = firstOf(p, RESOURCE_KEYS);
  let payer: string | null = null;

  // x402 402-challenge: { accepts: [{ payTo, maxAmountRequired, network, ... }] }
  const accept = Array.isArray(p.accepts) ? (p.accepts[0] as Record<string, unknown>) : null;
  if (accept && typeof accept === "object") {
    counterparty = counterparty ?? normalizeAddress(firstOf(accept, COUNTERPARTY_KEYS));
    amount = amount ?? atomicToDecimal(accept.maxAmountRequired, USDC_DECIMALS);
    asset = asset ?? firstOf(accept, ASSET_KEYS);
    chain = chain ?? firstOf(accept, CHAIN_KEYS);
    resource = resource ?? firstOf(accept, RESOURCE_KEYS);
  }

  // Signed X-PAYMENT header: decode for claim fields, and ALWAYS pass the raw
  // header through for the server-side payload-sim cross-check.
  const rawHeader = firstOf(p, XPAYMENT_KEYS);
  const decoded = decodeXPayment(rawHeader);
  if (decoded) {
    counterparty = counterparty ?? normalizeAddress(decoded.authorization.to);
    amount = amount ?? atomicToDecimal(decoded.authorization.value, USDC_DECIMALS);
    chain = chain ?? decoded.network;
    payer = normalizeAddress(decoded.authorization.from);
  }

  if (!counterparty || !amount) return null;
  const claim: PaymentClaim = {
    counterparty,
    amount,
    asset: typeof asset === "string" && asset ? asset : "USDC",
    chain: typeof chain === "string" && chain ? chain : "base",
  };
  if (typeof resource === "string" && resource) claim.resource = resource;
  if (payer) claim.payer = payer;
  if (decoded && typeof rawHeader === "string") claim.payment_authorization = rawHeader;
  return claim;
}

/**
 * Family verdict mapping (see integrations/langchain + integrations/wallets):
 * GO -> allow; HOLD -> confirm; STOP/hard_stop -> block; anything unrecognized
 * -> confirm (escalate, never silently allow). Observe mode always allows.
 */
export function decide(
  verdictObj: any,
  mode: Mode,
): { action: "allow" | "confirm" | "block"; verdict: string; reasons: string[] } {
  const v = typeof verdictObj?.verdict === "string" ? verdictObj.verdict : "UNKNOWN";
  const reasons = Array.isArray(verdictObj?.reasons) ? verdictObj.reasons : [];
  let action: "allow" | "confirm" | "block";
  if (verdictObj?.hard_stop || v === "STOP") action = "block";
  else if (v === "GO") action = "allow";
  else action = "confirm"; // HOLD and anything unrecognized escalate
  if (mode === "observe") action = "allow";
  return { action, verdict: v, reasons };
}

// ---------------------------------------------------------------------------
// Proxy-aware HTTPS POST (zero-dependency CONNECT tunnel)
//
// Node's global fetch (undici) ignores HTTP(S)_PROXY. In a proxy-only-egress
// network — a NemoClaw sandbox, most corporate networks — a direct fetch is
// dropped at the firewall, so tunnel through the proxy with node:net/node:tls.
// ---------------------------------------------------------------------------

function proxyFor(host: string): URL | null {
  if (host === "localhost" || host === "127.0.0.1" || host === "::1") return null;
  const raw = process.env.HTTPS_PROXY || process.env.https_proxy || "";
  try {
    return raw ? new URL(raw) : null;
  } catch {
    return null;
  }
}

const MAX_RESPONSE_BYTES = 1024 * 1024;

export async function postJson(
  urlStr: string,
  body: unknown,
  timeoutMs: number,
): Promise<{ status: number; json: any }> {
  const url = new URL(urlStr);
  const secure = url.protocol === "https:";
  const port = Number(url.port) || (secure ? 443 : 80);
  const proxy = proxyFor(url.hostname);
  const payload = Buffer.from(JSON.stringify(body), "utf-8");

  const raw = await new Promise<Buffer>((resolve, reject) => {
    const chunks: Buffer[] = [];
    let received = 0;
    const timer = setTimeout(() => {
      reject(new Error(`blackwall-x402-gate: request timed out after ${timeoutMs}ms`));
      sock?.destroy();
    }, timeoutMs);
    let sock: net.Socket | null = null;

    const sendRequest = (stream: net.Socket) => {
      stream.on("data", (c: Buffer) => {
        // A verdict is a few KB; cap the read so a compromised or misconfigured
        // endpoint cannot stream unbounded data into the hook's memory.
        received += c.length;
        if (received > MAX_RESPONSE_BYTES) {
          clearTimeout(timer);
          stream.destroy();
          reject(new Error("blackwall-x402-gate: response exceeded size cap"));
          return;
        }
        chunks.push(c);
      });
      stream.on("end", () => {
        clearTimeout(timer);
        resolve(Buffer.concat(chunks));
      });
      stream.on("error", (e: Error) => {
        clearTimeout(timer);
        reject(e);
      });
      stream.write(
        `POST ${url.pathname}${url.search} HTTP/1.1\r\n` +
          `Host: ${url.hostname}\r\n` +
          `Content-Type: application/json\r\n` +
          `Content-Length: ${payload.length}\r\n` +
          `Connection: close\r\n\r\n`,
      );
      stream.write(payload);
    };
    const wrapTls = (socket: net.Socket) => {
      const t = tls.connect({ socket, servername: url.hostname }, () => sendRequest(t));
      t.on("error", (e: Error) => {
        clearTimeout(timer);
        reject(e);
      });
    };

    if (proxy) {
      sock = net.connect(Number(proxy.port) || 80, proxy.hostname, () => {
        sock!.write(
          `CONNECT ${url.hostname}:${port} HTTP/1.1\r\nHost: ${url.hostname}:${port}\r\n\r\n`,
        );
      });
      let connectBuf = Buffer.alloc(0);
      const onConnectData = (c: Buffer) => {
        connectBuf = Buffer.concat([connectBuf, c]);
        const headerEnd = connectBuf.indexOf("\r\n\r\n");
        if (headerEnd === -1) {
          if (connectBuf.length > 16384) {
            clearTimeout(timer);
            sock!.destroy();
            reject(new Error("blackwall-x402-gate: oversized proxy CONNECT response"));
          }
          return;
        }
        sock!.off("data", onConnectData);
        const status = connectBuf.subarray(0, headerEnd).toString("latin1").split(" ")[1];
        if (status !== "200") {
          clearTimeout(timer);
          sock!.destroy();
          reject(new Error(`blackwall-x402-gate: proxy CONNECT failed (${status})`));
          return;
        }
        secure ? wrapTls(sock!) : sendRequest(sock!);
      };
      sock.on("data", onConnectData);
      sock.on("error", (e) => {
        clearTimeout(timer);
        reject(e);
      });
    } else {
      sock = net.connect(port, url.hostname, () => {
        secure ? wrapTls(sock!) : sendRequest(sock!);
      });
      sock.on("error", (e) => {
        clearTimeout(timer);
        reject(e);
      });
    }
  });

  const headerEnd = raw.indexOf("\r\n\r\n");
  if (headerEnd === -1) throw new Error("blackwall-x402-gate: malformed HTTP response");
  const head = raw.subarray(0, headerEnd).toString("latin1");
  const status = Number(head.split(" ")[1]) || 0;
  let bodyBuf = raw.subarray(headerEnd + 4);
  if (/transfer-encoding:\s*chunked/i.test(head)) {
    const parts: Buffer[] = [];
    let off = 0;
    while (off < bodyBuf.length) {
      const lineEnd = bodyBuf.indexOf("\r\n", off);
      if (lineEnd === -1) break;
      const size = parseInt(bodyBuf.subarray(off, lineEnd).toString("latin1"), 16);
      if (!size) break;
      parts.push(bodyBuf.subarray(lineEnd + 2, lineEnd + 2 + size));
      off = lineEnd + 2 + size + 2;
    }
    bodyBuf = Buffer.concat(parts);
  }
  let json: any = null;
  try {
    json = JSON.parse(bodyBuf.toString("utf-8"));
  } catch {
    json = null;
  }
  return { status, json };
}

/** POST the claim to /v1/forecast-payment; non-2xx (or non-JSON) throws. */
async function forecastPayment(claim: PaymentClaim, cfg: ResolvedConfig): Promise<any> {
  const { status, json } = await postJson(
    cfg.baseUrl.replace(/\/$/, "") + "/v1/forecast-payment",
    claim,
    cfg.forecastTimeoutMs,
  );
  if (status < 200 || status >= 300 || json === null) {
    const detail = json?.error ? `: ${json.error}` : "";
    throw new Error(`blackwall-x402-gate: forecast-payment HTTP ${status}${detail}`);
  }
  return json;
}

// ---------------------------------------------------------------------------
// Hook + plugin entry
// ---------------------------------------------------------------------------

const DEFAULT_BASE_URL = "https://blackwall-free.onrender.com";
// Generous: the free instance cold-starts (up to ~60s) after idle.
const DEFAULT_TIMEOUT_MS = 60000;

export function resolveConfig(config: GateConfig = {}): ResolvedConfig {
  const mode = (config.mode ?? process.env.BLACKWALL_X402_MODE ?? "enforce").toLowerCase();
  const envFailClosed = process.env.BLACKWALL_X402_FAIL_CLOSED;
  return {
    baseUrl: config.baseUrl ?? process.env.BLACKWALL_X402_URL ?? DEFAULT_BASE_URL,
    // Enforce by default: this hook fires only on payment-shaped calls, and a
    // payment backstop that defaults to advisory is not a backstop.
    mode: mode === "observe" ? "observe" : "enforce",
    failClosed:
      typeof config.failClosed === "boolean"
        ? config.failClosed
        : envFailClosed != null
          ? ["1", "true", "yes"].includes(envFailClosed.toLowerCase())
          : true,
    payer: normalizeAddress(config.payer ?? process.env.BLACKWALL_PAYER) ?? undefined,
    // Lowercased: tool-name tokens compare lowercase, so an uppercase config
    // entry ("Treasury") must not silently fail to match.
    paymentTools: [
      ...DEFAULT_PAYMENT_TOKENS,
      ...(Array.isArray(config.paymentTools)
        ? config.paymentTools.map((t) => String(t).toLowerCase())
        : []),
    ],
    forecastTimeoutMs:
      typeof config.forecastTimeoutMs === "number" ? config.forecastTimeoutMs : DEFAULT_TIMEOUT_MS,
    forecast: typeof config.forecast === "function" ? config.forecast : forecastPayment,
  };
}

/**
 * `before_tool_call` handler. Returns undefined (pass through) for everything
 * except a payment-shaped call that must not proceed, which returns
 * { block: true, blockReason } per the hook contract.
 */
export async function handleBeforeToolCall(
  event: any,
  cfg: ResolvedConfig,
  logger: any = console,
): Promise<any> {
  const toolName: string | undefined = event?.toolName;
  if (!toolName) return undefined;

  const claim = extractClaim(event?.params ?? {});
  if (!claim) {
    // No claim: only a payment-NAMED tool is suspicious — a payment we cannot
    // score must not be signed on a guess (enforce blocks; observe reports).
    if (!toolLooksLikePayment(toolName, cfg.paymentTools)) return undefined;
    const msg =
      `blackwall-x402-gate: "${toolName}" looks like a payment tool but no claim ` +
      "could be extracted from its params (unscorable). " +
      "Pass payTo/amount (or the 402 challenge / X-PAYMENT header) so the payment can be scored.";
    if (cfg.mode === "enforce") return { block: true, blockReason: msg };
    logger.warn?.(`[blackwall-x402] ${msg} (observe: allowing)`);
    return undefined;
  }
  if (cfg.payer && !claim.payer) claim.payer = cfg.payer;

  let verdictObj: any;
  try {
    verdictObj = await cfg.forecast(claim, cfg);
  } catch (err: any) {
    if (cfg.mode === "enforce" && cfg.failClosed) {
      return {
        block: true,
        blockReason:
          `blackwall-x402-gate: verdict service unreachable (${err?.message ?? err}); ` +
          `payment of ${claim.amount} ${claim.asset} to ${claim.counterparty} blocked (fail-closed). ` +
          "Retry, or set failClosed:false to prefer availability over enforcement.",
      };
    }
    logger.warn?.(
      `[blackwall-x402] forecast failed (${err?.message ?? err}) — allowing unscored payment (fail-open)`,
    );
    return undefined;
  }

  const d = decide(verdictObj, cfg.mode);
  logger.info?.(
    `[blackwall-x402] ${cfg.mode} · ${toolName} → ${d.verdict} for ${claim.amount} ${claim.asset} ` +
      `to ${claim.counterparty}${verdictObj?.receipt_id ? ` (receipt ${verdictObj.receipt_id})` : ""}`,
  );
  if (d.action === "allow") return undefined;

  // No interactive approval surface in this runtime: `confirm` (HOLD) degrades
  // to block-with-detail so a human can re-decide out of band.
  const reasons = d.reasons.length ? `\n- ${d.reasons.join("\n- ")}` : "";
  const escalation =
    d.action === "confirm"
      ? "\nThis is a HOLD (escalate), not a hard refusal: a human can approve out of band and re-run."
      : "";
  return {
    block: true,
    blockReason:
      `blackwall-x402-gate: ${d.verdict} verdict on paying ${claim.amount} ${claim.asset} ` +
      `to ${claim.counterparty} (chain ${claim.chain}).${reasons}${escalation}`,
  };
}

export function createBlackwallX402Gate(config: GateConfig = {}) {
  return definePluginEntry({
    id: "blackwall-x402-gate",
    name: "Blackwall x402 Payment Gate",
    description:
      "Pre-signature payment verdicts for x402: hooks before_tool_call, recognizes " +
      "payment-shaped tool calls, and blocks anything Blackwall does not score GO " +
      "(reputation, price-anomaly, OFAC sanctions, Sybil signals). Keyless: only the " +
      "payment claim leaves the sandbox, never tool payloads.",
    register(api: any) {
      const cfg = resolveConfig(config);
      const logger = api?.logger ?? console;
      logger.info?.(
        `[blackwall-x402] registered (mode ${cfg.mode}, failClosed ${cfg.failClosed}, ${cfg.baseUrl})`,
      );
      api.on("before_tool_call", (event: any) => handleBeforeToolCall(event, cfg, logger), {
        priority: 90,
        timeoutMs: cfg.forecastTimeoutMs + 5000,
      });
    },
  });
}

export default createBlackwallX402Gate;
