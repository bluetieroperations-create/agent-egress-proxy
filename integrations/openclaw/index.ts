/**
 * blackwall-openclaw-gate -- the OpenClaw host adapter.
 *
 * THIN BY DESIGN: everything that does not need the host lives in `core.ts`
 * (see its header for why it was split out). This file is the one place that
 * imports the OpenClaw plugin SDK, and it re-exports the core so every
 * existing `from "./index.js"` importer is unaffected.
 */
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

import {
  handleBeforeToolCall,
  resolveConfig,
  type GateConfig,
} from "./core.js";

export * from "./core.js";


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
